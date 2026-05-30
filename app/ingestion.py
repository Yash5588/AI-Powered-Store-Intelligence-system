"""Event ingestion: per-event validation, deduplication, and persistence.

Design note (partial success):
We deliberately do NOT let FastAPI validate the whole batch as one model. If we
did, a single malformed event would 422 the entire batch. Instead we validate
each event independently here so the caller gets a structured per-event verdict
(accepted / duplicate / rejected) and one bad event never blocks 499 good ones.

Idempotency:
`event_id` is the primary key. Re-ingesting the same payload yields duplicates,
not new rows and not errors — so POST /events/ingest is safe to call twice.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Event, EventRecord, IngestResult, RejectedEvent

MAX_BATCH_SIZE = 500


def _summarise_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic error into a short, safe, human-readable string.

    No stack traces, no internal paths — just field + problem, suitable to
    return to an external caller.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts[:5])  # cap to keep the response compact


def normalise_batch(payload: Any) -> list[dict]:
    """Accept either a bare list of events or {"events": [...]}.

    Both shapes appear in the wild (raw JSONL replay vs. wrapped request body),
    so we tolerate both rather than forcing the caller to guess.
    """
    if isinstance(payload, dict) and "events" in payload:
        events = payload["events"]
    else:
        events = payload
    if not isinstance(events, list):
        raise ValueError("Request body must be a JSON array of events or an object with 'events'.")
    return events


def ingest_events(db: Session, raw_events: list[dict]) -> IngestResult:
    """Validate, deduplicate, and persist a batch of raw event dicts."""
    received = len(raw_events)
    rejected: list[RejectedEvent] = []
    valid: list[tuple[int, Event]] = []

    # 1) Per-event validation.
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            rejected.append(
                RejectedEvent(index=index, reason="malformed", detail="Event must be a JSON object.")
            )
            continue
        try:
            event = Event.model_validate(raw)
            valid.append((index, event))
        except ValidationError as exc:
            rejected.append(
                RejectedEvent(
                    index=index,
                    event_id=raw.get("event_id") if isinstance(raw.get("event_id"), str) else None,
                    reason="validation_error",
                    detail=_summarise_validation_error(exc),
                )
            )

    # 2) Intra-batch deduplication (first occurrence wins).
    duplicate_ids: list[str] = []
    seen_in_batch: set[str] = set()
    unique_in_batch: list[tuple[int, Event]] = []
    for index, event in valid:
        if event.event_id in seen_in_batch:
            duplicate_ids.append(event.event_id)
        else:
            seen_in_batch.add(event.event_id)
            unique_in_batch.append((index, event))

    # 3) Cross-batch deduplication against what's already persisted.
    candidate_ids = [e.event_id for _, e in unique_in_batch]
    existing_ids: set[str] = set()
    if candidate_ids:
        existing_ids = set(
            db.scalars(
                select(EventRecord.event_id).where(EventRecord.event_id.in_(candidate_ids))
            ).all()
        )

    to_insert: list[Event] = []
    accepted_ids: list[str] = []
    for _, event in unique_in_batch:
        if event.event_id in existing_ids:
            duplicate_ids.append(event.event_id)
        else:
            to_insert.append(event)
            accepted_ids.append(event.event_id)

    # 4) Persist. Tolerate a concurrent insert of the same id (race -> duplicate).
    if to_insert:
        db.add_all([EventRecord.from_event(e) for e in to_insert])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            accepted_ids, duplicate_ids = _persist_one_by_one(
                db, to_insert, accepted_ids, duplicate_ids
            )

    return IngestResult(
        received=received,
        accepted=len(accepted_ids),
        duplicates=len(duplicate_ids),
        rejected=len(rejected),
        accepted_ids=accepted_ids,
        duplicate_ids=duplicate_ids,
        rejected_events=rejected,
    )


def _persist_one_by_one(
    db: Session,
    events: list[Event],
    accepted_ids: list[str],
    duplicate_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Fallback path: insert individually so one collision can't fail the batch."""
    accepted_ids = []
    for event in events:
        db.add(EventRecord.from_event(event))
        try:
            db.commit()
            accepted_ids.append(event.event_id)
        except IntegrityError:
            db.rollback()
            duplicate_ids.append(event.event_id)
    return accepted_ids, duplicate_ids


def dominant_store_id(raw_events: list[dict]) -> Optional[str]:
    """Best-effort store id for the request log line (most common in batch)."""
    store_ids = [
        e["store_id"]
        for e in raw_events
        if isinstance(e, dict) and isinstance(e.get("store_id"), str)
    ]
    if not store_ids:
        return None
    return Counter(store_ids).most_common(1)[0][0]

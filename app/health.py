"""Health reporting.

`/health` is the first thing an on-call engineer checks, so it must be accurate
and cheap. It reports DB connectivity, total events, and per-store feed
freshness. A store whose most recent event is older than the staleness
threshold (default 10 minutes) is flagged STALE_FEED.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import check_connection
from app.models import EventRecord, HealthResponse, StoreFeedStatus

# Configurable so tests / ops can tighten or relax the freshness SLA.
STALE_FEED_THRESHOLD_SECONDS = int(os.getenv("STALE_FEED_THRESHOLD_SECONDS", str(10 * 60)))
# Warn when the latest event appears more than this many seconds in the future.
FUTURE_EVENT_THRESHOLD_SECONDS = int(os.getenv("FUTURE_EVENT_THRESHOLD_SECONDS", "60"))


def _as_utc(value: datetime) -> datetime:
    """SQLite may return naive datetimes; treat them as UTC for lag math."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _feed_lag_seconds(
    now: datetime,
    last_ts: datetime,
    *,
    stale_threshold: int = STALE_FEED_THRESHOLD_SECONDS,
    future_threshold: int = FUTURE_EVENT_THRESHOLD_SECONDS,
) -> tuple[float, bool, bool]:
    """Compute lag, stale_feed, and future-timestamp flags for one store feed.

    lag_seconds is always >= 0 (future events must not produce negative lag).
    stale_feed is True only when lag exceeds the staleness threshold.
    future_timestamp is True when last_ts is more than `future_threshold`
    seconds ahead of `now` (clock skew / bad synthetic timestamps).
    """
    now = _as_utc(now)
    last_ts = _as_utc(last_ts)
    lag_raw = (now - last_ts).total_seconds()
    future_timestamp = lag_raw < -future_threshold
    lag = max(0.0, lag_raw)
    stale = lag > stale_threshold
    return lag, stale, future_timestamp


def build_health(db: Session) -> HealthResponse:
    """Assemble the health snapshot."""
    now = datetime.now(timezone.utc)
    db_up = check_connection()

    # If the DB is down we report degraded but never raise — health must answer.
    if not db_up:
        return HealthResponse(
            status="degraded",
            database="down",
            event_count=0,
            server_time=now,
            stale_feed_threshold_seconds=STALE_FEED_THRESHOLD_SECONDS,
            stores=[],
            warnings=["DATABASE_UNAVAILABLE"],
        )

    total_events = db.scalar(select(func.count()).select_from(EventRecord)) or 0

    # Latest event timestamp per store, in one grouped query.
    rows = db.execute(
        select(EventRecord.store_id, func.max(EventRecord.timestamp)).group_by(EventRecord.store_id)
    ).all()

    stores: list[StoreFeedStatus] = []
    warnings: list[str] = []
    for store_id, last_ts in rows:
        if last_ts is None:
            stores.append(StoreFeedStatus(store_id=store_id))
            continue
        last_ts = _as_utc(last_ts)
        lag, stale, future = _feed_lag_seconds(now, last_ts)
        if stale:
            warnings.append(f"STALE_FEED:{store_id}")
        if future:
            warnings.append(f"FUTURE_EVENT_TIMESTAMP:{store_id}")
        stores.append(
            StoreFeedStatus(
                store_id=store_id,
                last_event_timestamp=last_ts,
                lag_seconds=round(lag, 1),
                stale_feed=stale,
            )
        )

    stores.sort(key=lambda s: s.store_id)

    return HealthResponse(
        status="ok",
        database="up",
        event_count=total_events,
        server_time=now,
        stale_feed_threshold_seconds=STALE_FEED_THRESHOLD_SECONDS,
        stores=stores,
        warnings=warnings,
    )

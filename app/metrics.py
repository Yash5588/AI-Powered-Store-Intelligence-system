"""Real-time store metrics  [PHASE 2].

GET /stores/{id}/metrics -> unique visitors, conversion rate, avg dwell per
zone, current queue depth, and abandonment rate, computed live from stored
events. Staff (is_staff=true) are excluded. Zero-traffic and zero-purchase
stores return well-formed zeros rather than nulls or division errors.

Conversion rule (per the spec):
A visitor is "converted" if they were in the billing zone within
CONVERSION_WINDOW_MINUTES (default 5) BEFORE a POS transaction for the same
store. We greedily match each transaction to at most one billing visitor so two
transactions can't both credit the same single visitor.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.loaders import load_pos_transactions
from app.models import MetricsResponse, ZoneDwell
from app.sessions import (
    CONVERSION_WINDOW_MINUTES,
    MIN_SESSIONS_FOR_CONFIDENCE,
    VisitorSession,
    customer_sessions,
    load_store_context,
)


def _safe_rate(numerator: int, denominator: int) -> float:
    """Division that never raises and is rounded for stable JSON output."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def correlate_conversions(
    sessions: list[VisitorSession],
    store_id: str,
    billing_zones: set[str],
) -> tuple[int, int]:
    """Return (converted_visitor_count, matched_transaction_count).

    Greedy time-window matching: each POS transaction is credited to the most
    recent un-credited billing visitor that was in billing within the window.
    """
    transactions = load_pos_transactions(store_id)
    if not transactions or not sessions:
        return 0, 0

    window = timedelta(minutes=CONVERSION_WINDOW_MINUTES)

    # Candidate visitors: customers who were in the billing zone, with a time.
    billing_visitors = [
        s for s in sessions if s.was_in_billing and s.last_billing_time is not None
    ]
    billing_visitors.sort(key=lambda s: s.last_billing_time)  # type: ignore[arg-type]

    converted: set[str] = set()
    matched_txns = 0
    for txn in transactions:
        # Eligible: in billing within [txn - window, txn], not yet credited.
        best: VisitorSession | None = None
        for s in billing_visitors:
            if s.visitor_id in converted:
                continue
            bt = s.last_billing_time
            if bt is None:
                continue
            if txn.timestamp - window <= bt <= txn.timestamp:
                # Prefer the closest-in-time billing visit before the txn.
                if best is None or bt > best.last_billing_time:  # type: ignore[operator]
                    best = s
        if best is not None:
            converted.add(best.visitor_id)
            matched_txns += 1

    return len(converted), matched_txns


def compute_store_metrics(db: Session, store_id: str) -> MetricsResponse:
    """Compute the live metrics payload for a store."""
    window, events, all_sessions, billing_zones = load_store_context(db, store_id)
    sessions = customer_sessions(all_sessions)

    # Empty / zero-traffic store: well-formed zeros, never a crash.
    if not sessions:
        return MetricsResponse(
            store_id=store_id,
            window_start=window.start,
            window_end=window.end,
            unique_visitors=0,
            converted_visitors=0,
            conversion_rate=0.0,
            avg_dwell_per_zone=[],
            current_queue_depth=0,
            abandonment_rate=0.0,
            transactions=len(load_pos_transactions(store_id)),
            data_confidence=False,
        )

    unique_visitors = len(sessions)

    # Average dwell per zone across customer sessions that visited each zone.
    zone_totals: dict[str, list[int]] = {}
    for s in sessions:
        for zone, dwell in s.zone_dwell_ms.items():
            zone_totals.setdefault(zone, []).append(dwell)
    avg_dwell = [
        ZoneDwell(
            zone_id=zone,
            avg_dwell_ms=round(sum(v) / len(v), 2),
            avg_dwell_seconds=round(sum(v) / len(v) / 1000.0, 2),
        )
        for zone, v in sorted(zone_totals.items())
        if v
    ]

    # Current queue depth: the most recent observed queue_depth in billing.
    current_queue_depth = _latest_queue_depth(events, billing_zones)

    # Abandonment rate: of customers who joined the billing queue, how many
    # abandoned (left before a purchase). Guarded against divide-by-zero.
    joined = [s for s in sessions if s.joined_billing_queue]
    abandoned = [s for s in joined if s.abandoned_billing]
    abandonment_rate = _safe_rate(len(abandoned), len(joined))

    converted_visitors, matched_txns = correlate_conversions(sessions, store_id, billing_zones)
    conversion_rate = _safe_rate(converted_visitors, unique_visitors)

    return MetricsResponse(
        store_id=store_id,
        window_start=window.start,
        window_end=window.end,
        unique_visitors=unique_visitors,
        converted_visitors=converted_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell,
        current_queue_depth=current_queue_depth,
        abandonment_rate=abandonment_rate,
        transactions=matched_txns,
        data_confidence=unique_visitors >= MIN_SESSIONS_FOR_CONFIDENCE,
    )


def _latest_queue_depth(events, billing_zones: set[str]) -> int:
    """Most recent non-null queue_depth seen in a billing zone (0 if none)."""
    latest_depth = 0
    latest_time = None
    for ev in events:
        if ev.queue_depth is None:
            continue
        if ev.zone_id and ev.zone_id not in billing_zones and ev.event_type != "BILLING_QUEUE_JOIN":
            continue
        if latest_time is None or ev.timestamp >= latest_time:
            latest_time = ev.timestamp
            latest_depth = ev.queue_depth
    return latest_depth

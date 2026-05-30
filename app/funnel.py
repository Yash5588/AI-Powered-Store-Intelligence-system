"""Conversion funnel + session logic  [PHASE 2].

GET /stores/{id}/funnel -> Entry -> Zone Visit -> Billing Queue -> Purchase,
with per-stage counts and drop-off % between stages.

The unit is the visitor SESSION, not raw events. Stages are strictly nested:
each stage is a subset of the previous one, so counts always satisfy
Entry >= Zone Visit >= Billing Queue >= Purchase.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.metrics import correlate_conversions
from app.models import FunnelResponse, FunnelStage
from app.sessions import customer_sessions, load_store_context


def _drop_off_pct(prev_count: int, current_count: int) -> float:
    """Percentage of sessions lost from the previous stage (0 if no prev)."""
    if prev_count <= 0:
        return 0.0
    lost = prev_count - current_count
    return round(max(lost, 0) / prev_count * 100.0, 2)


def _billing_queue_sessions(sessions: list) -> list:
    """Sessions that reached billing queue or billing zone activity."""
    return [
        s for s in sessions
        if s.joined_billing_queue or s.was_in_billing
    ]


def compute_funnel(db: Session, store_id: str) -> FunnelResponse:
    """Build the session-based, nested conversion funnel for a store."""
    window, _events, all_sessions, billing_zones = load_store_context(db, store_id)
    sessions = customer_sessions(all_sessions)

    if not sessions:
        stages = [
            FunnelStage(stage=name, count=0, drop_off_pct=0.0)
            for name in ("Entry", "Zone Visit", "Billing Queue", "Purchase")
        ]
        return FunnelResponse(
            store_id=store_id,
            window_start=window.start,
            window_end=window.end,
            total_sessions=0,
            stages=stages,
        )

    # Stage 1 — Entry: customer sessions with an ENTRY event.
    entry = [s for s in sessions if s.has_entry]

    # Stage 2 — Zone Visit: entered sessions with ZONE_ENTER or ZONE_DWELL.
    zone_visit = [s for s in entry if s.has_zone_visit]

    # Stage 3 — Billing Queue: zone-visit sessions that joined billing or
    # were observed in the billing zone. Direct billing without a prior zone
    # visit does NOT count here (keeps the funnel nested).
    billing = _billing_queue_sessions(zone_visit)

    # Stage 4 — Purchase: billing-queue sessions matched to POS within window.
    converted_count, _ = correlate_conversions(billing, store_id, billing_zones)
    purchase_count = min(converted_count, len(billing))

    counts = [len(entry), len(zone_visit), len(billing), purchase_count]
    names = ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    stages: list[FunnelStage] = []
    for i, (name, count) in enumerate(zip(names, counts)):
        drop = 0.0 if i == 0 else _drop_off_pct(counts[i - 1], count)
        stages.append(FunnelStage(stage=name, count=count, drop_off_pct=drop))

    return FunnelResponse(
        store_id=store_id,
        window_start=window.start,
        window_end=window.end,
        total_sessions=len(sessions),
        stages=stages,
    )

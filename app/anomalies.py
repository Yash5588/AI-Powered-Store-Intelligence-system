"""Operational anomaly detection  [PHASE 2].

GET /stores/{id}/anomalies -> active operational anomalies:

  * BILLING_QUEUE_SPIKE - current queue depth exceeds a threshold
  * CONVERSION_DROP     - today's conversion rate is materially below a 7-day baseline
  * DEAD_ZONE           - a defined zone has had no visits for 30+ minutes

Each anomaly carries anomaly_type, severity (INFO/WARN/CRITICAL), a human
message, a suggested_action, and a timestamp. The list is empty (not null) when
the store is healthy or has no data.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.loaders import get_store_zones
from app.metrics import _latest_queue_depth, correlate_conversions
from app.models import AnomaliesResponse, Anomaly, EventRecord
from app.sessions import (
    _as_utc,
    customer_sessions,
    load_store_context,
)

QUEUE_SPIKE_WARN = int(os.getenv("QUEUE_SPIKE_WARN", "5"))
QUEUE_SPIKE_CRITICAL = int(os.getenv("QUEUE_SPIKE_CRITICAL", "8"))
DEAD_ZONE_MINUTES = int(os.getenv("DEAD_ZONE_MINUTES", "30"))
CONVERSION_DROP_WARN = float(os.getenv("CONVERSION_DROP_WARN", "0.30"))  # 30% below baseline
CONVERSION_DROP_CRITICAL = float(os.getenv("CONVERSION_DROP_CRITICAL", "0.50"))
BASELINE_DAYS = int(os.getenv("CONVERSION_BASELINE_DAYS", "7"))


def _queue_spike(events, billing_zones, now) -> Anomaly | None:
    depth = _latest_queue_depth(events, billing_zones)
    if depth >= QUEUE_SPIKE_CRITICAL:
        severity = "CRITICAL"
    elif depth >= QUEUE_SPIKE_WARN:
        severity = "WARN"
    else:
        return None
    return Anomaly(
        anomaly_type="BILLING_QUEUE_SPIKE",
        severity=severity,
        message=f"Billing queue depth is {depth} (threshold {QUEUE_SPIKE_WARN}).",
        suggested_action="Open an additional billing counter or redirect staff to checkout.",
        timestamp=now,
        zone_id=next(iter(billing_zones), "BILLING"),
        value=float(depth),
    )


def _dead_zones(db: Session, store_id: str, now: datetime) -> list[Anomaly]:
    """Defined zones with no visit in the last DEAD_ZONE_MINUTES."""
    threshold = now - timedelta(minutes=DEAD_ZONE_MINUTES)
    defined = get_store_zones(store_id)
    if not defined:
        return []

    # Last visit time per zone (customers only — exclude staff at query time).
    rows = db.execute(
        select(EventRecord.zone_id, func.max(EventRecord.timestamp))
        .where(EventRecord.store_id == store_id)
        .where(EventRecord.is_staff.is_(False))
        .where(EventRecord.zone_id.is_not(None))
        .group_by(EventRecord.zone_id)
    ).all()
    last_visit = {z: _as_utc(t) for z, t in rows if t is not None}

    anomalies: list[Anomaly] = []
    for zone in defined:
        if zone == "ENTRY":  # threshold, not a dwell zone
            continue
        lv = last_visit.get(zone)
        if lv is None or lv < threshold:
            mins = None if lv is None else round((now - lv).total_seconds() / 60.0, 1)
            detail = "no visits recorded" if lv is None else f"last visit {mins} min ago"
            anomalies.append(
                Anomaly(
                    anomaly_type="DEAD_ZONE",
                    severity="WARN",
                    message=f"Zone '{zone}' has had no customer visits in {DEAD_ZONE_MINUTES} min ({detail}).",
                    suggested_action=f"Check merchandising/lighting in '{zone}' or verify camera coverage.",
                    timestamp=now,
                    zone_id=zone,
                )
            )
    return anomalies


def _conversion_drop(db: Session, store_id: str, sessions, billing_zones, now) -> Anomaly | None:
    """Compare current-window conversion to a prior multi-day baseline."""
    if not sessions:
        return None
    converted, _ = correlate_conversions(sessions, store_id, billing_zones)
    current_rate = converted / len(sessions) if sessions else 0.0

    # Baseline = conversion over the BASELINE_DAYS before the current window.
    window_end = now
    baseline_start = window_end - timedelta(days=BASELINE_DAYS)
    rows = list(
        db.scalars(
            select(EventRecord)
            .where(EventRecord.store_id == store_id)
            .where(EventRecord.is_staff.is_(False))
            .where(EventRecord.timestamp >= baseline_start)
            .where(EventRecord.timestamp < (now - timedelta(minutes=1)))
        ).all()
    )
    if not rows:
        return None
    baseline_sessions = {r.visitor_id for r in rows}
    baseline_billing = {
        r.visitor_id for r in rows if r.zone_id in billing_zones or r.event_type == "BILLING_QUEUE_JOIN"
    }
    if not baseline_sessions or not baseline_billing:
        return None
    baseline_rate = len(baseline_billing) / len(baseline_sessions)
    if baseline_rate <= 0:
        return None

    drop = (baseline_rate - current_rate) / baseline_rate
    if drop >= CONVERSION_DROP_CRITICAL:
        severity = "CRITICAL"
    elif drop >= CONVERSION_DROP_WARN:
        severity = "WARN"
    else:
        return None

    return Anomaly(
        anomaly_type="CONVERSION_DROP",
        severity=severity,
        message=(
            f"Conversion rate {current_rate:.0%} is {drop:.0%} below the "
            f"{BASELINE_DAYS}-day baseline of {baseline_rate:.0%}."
        ),
        suggested_action="Review staffing, queue length, and stock availability in billing.",
        timestamp=now,
        value=round(drop, 4),
    )


def detect_anomalies(db: Session, store_id: str) -> AnomaliesResponse:
    """Evaluate all anomaly detectors for a store and return active ones."""
    window, events, all_sessions, billing_zones = load_store_context(db, store_id)
    sessions = customer_sessions(all_sessions)
    now = window.end or datetime.now(timezone.utc)

    # No data in the window -> nothing is "active". A zero-traffic store should
    # not raise DEAD_ZONE for every zone; that's noise, not an active anomaly.
    if not events:
        return AnomaliesResponse(
            store_id=store_id, evaluated_at=now, anomaly_count=0, anomalies=[]
        )

    found: list[Anomaly] = []

    spike = _queue_spike(events, billing_zones, now)
    if spike:
        found.append(spike)

    found.extend(_dead_zones(db, store_id, now))

    drop = _conversion_drop(db, store_id, sessions, billing_zones, now)
    if drop:
        found.append(drop)

    # Order by severity for at-a-glance triage.
    severity_rank = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    found.sort(key=lambda a: severity_rank.get(a.severity, 3))

    return AnomaliesResponse(
        store_id=store_id,
        evaluated_at=now,
        anomaly_count=len(found),
        anomalies=found,
    )

"""Session reconstruction — the shared unit of analysis for Phase 2.

The funnel, metrics, and heatmap endpoints all operate on *visitor sessions*,
not raw events. This module turns a store's stored events into sessions and
provides the analytics window helpers they share.

Session model
-------------
A session is keyed by ``(store_id, visitor_id)``. The detection pipeline assigns
a ``visitor_id`` that is "unique per visit session", and a REENTRY event reuses
the SAME visitor_id (that's how re-entry is detected). Therefore grouping by
visitor_id automatically collapses a re-entry into the original session — the
visitor is counted ONCE. This is exactly the "re-entries must not double-count a
visitor" requirement, handled at the data-model level rather than with special
cases.

Staff exclusion
---------------
A session is staff if ANY of its events is flagged ``is_staff=true``. Staff
sessions are dropped from all customer-facing metrics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.loaders import get_billing_zone_ids
from app.models import EventRecord

# Analytics "now" is anchored to the store's most recent event so that fixed
# historical test/replay data still produces a populated window. The window is
# [latest - ANALYTICS_WINDOW, latest]. Set ANALYTICS_WINDOW_MINUTES=0 to use all
# stored events for the store (no lower bound).
ANALYTICS_WINDOW_MINUTES = int(os.getenv("ANALYTICS_WINDOW_MINUTES", "1440"))  # 24h
# Conversion: a visitor in the billing zone within this many minutes BEFORE a POS
# transaction (same store) is counted as converted.
CONVERSION_WINDOW_MINUTES = int(os.getenv("CONVERSION_WINDOW_MINUTES", "5"))
# Heatmap reports low confidence below this many sessions in the window.
MIN_SESSIONS_FOR_CONFIDENCE = int(os.getenv("MIN_SESSIONS_FOR_CONFIDENCE", "20"))


@dataclass
class VisitorSession:
    """One customer (or staff) visit, reconstructed from events."""

    store_id: str
    visitor_id: str
    is_staff: bool = False
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    has_entry: bool = False
    has_zone_visit: bool = False  # ZONE_ENTER or ZONE_DWELL observed
    visited_zones: set[str] = field(default_factory=set)
    zone_dwell_ms: dict[str, int] = field(default_factory=dict)  # zone -> max dwell seen
    joined_billing_queue: bool = False
    abandoned_billing: bool = False
    was_in_billing: bool = False
    last_billing_time: Optional[datetime] = None


@dataclass
class AnalyticsWindow:
    store_id: str
    start: Optional[datetime]
    end: Optional[datetime]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_window(db: Session, store_id: str) -> AnalyticsWindow:
    """Compute the analytics window anchored on the store's latest event."""
    latest = db.scalar(
        select(func.max(EventRecord.timestamp)).where(EventRecord.store_id == store_id)
    )
    if latest is None:
        return AnalyticsWindow(store_id=store_id, start=None, end=None)
    latest = _as_utc(latest)
    if ANALYTICS_WINDOW_MINUTES <= 0:
        return AnalyticsWindow(store_id=store_id, start=None, end=latest)
    start = latest - timedelta(minutes=ANALYTICS_WINDOW_MINUTES)
    return AnalyticsWindow(store_id=store_id, start=start, end=latest)


def fetch_events(db: Session, store_id: str, window: AnalyticsWindow) -> list[EventRecord]:
    """Fetch a store's events within the window, oldest first."""
    stmt = select(EventRecord).where(EventRecord.store_id == store_id)
    if window.start is not None:
        stmt = stmt.where(EventRecord.timestamp >= window.start)
    if window.end is not None:
        stmt = stmt.where(EventRecord.timestamp <= window.end)
    stmt = stmt.order_by(EventRecord.timestamp.asc())
    return list(db.scalars(stmt).all())


def build_sessions(
    events: list[EventRecord], billing_zones: set[str]
) -> dict[str, VisitorSession]:
    """Group events into per-visitor sessions (re-entry collapses into one)."""
    sessions: dict[str, VisitorSession] = {}
    for ev in events:
        s = sessions.get(ev.visitor_id)
        if s is None:
            s = VisitorSession(store_id=ev.store_id, visitor_id=ev.visitor_id)
            sessions[ev.visitor_id] = s

        ts = _as_utc(ev.timestamp)
        if s.first_seen is None or ts < s.first_seen:
            s.first_seen = ts
        if s.last_seen is None or ts > s.last_seen:
            s.last_seen = ts

        # Any staff-flagged event marks the whole session as staff.
        if ev.is_staff:
            s.is_staff = True

        etype = ev.event_type
        if etype == "ENTRY":
            s.has_entry = True
        if etype in ("ZONE_ENTER", "ZONE_DWELL"):
            s.has_zone_visit = True

        if ev.zone_id:
            s.visited_zones.add(ev.zone_id)
            # Track the longest dwell observed per zone (ZONE_DWELL accumulates).
            if ev.dwell_ms and ev.dwell_ms > s.zone_dwell_ms.get(ev.zone_id, 0):
                s.zone_dwell_ms[ev.zone_id] = ev.dwell_ms

        in_billing = bool(ev.zone_id and ev.zone_id in billing_zones)
        if in_billing:
            s.was_in_billing = True
            s.last_billing_time = ts

        if etype == "BILLING_QUEUE_JOIN":
            s.joined_billing_queue = True
        if etype == "BILLING_QUEUE_ABANDON":
            s.abandoned_billing = True

    return sessions


def customer_sessions(sessions: dict[str, VisitorSession]) -> list[VisitorSession]:
    """Sessions with staff excluded."""
    return [s for s in sessions.values() if not s.is_staff]


def load_store_context(db: Session, store_id: str):
    """Convenience: window + customer sessions + billing zones for a store."""
    window = resolve_window(db, store_id)
    billing_zones = get_billing_zone_ids(store_id)
    events = fetch_events(db, store_id, window)
    sessions = build_sessions(events, billing_zones)
    return window, events, sessions, billing_zones

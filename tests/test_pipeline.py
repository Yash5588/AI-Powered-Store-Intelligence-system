# PROMPT: "Write pytest tests for a CCTV detection pipeline's pure-Python parts
#          (no video needed): (1) zone mapping from normalized polygon regions
#          with point-in-polygon + default-region fallback, (2) a centroid
#          tracker that keeps a stable visitor_id across frames, creates new ids
#          for new people, and reuses an id (re-entry) when someone reappears
#          nearby soon after, and (3) the SessionStateMachine that turns
#          (visitor, zone, confidence, time) observations into ENTRY / ZONE_ENTER
#          / ZONE_DWELL (every 30s) / BILLING_QUEUE_JOIN / EXIT events that match
#          the API's Pydantic schema. Verify YOLO confidence is preserved and
#          events validate against app.models.Event."
# CHANGES MADE: Asserted emitted events validate against the real Pydantic
#          Event model (schema compliance), tightened the re-entry test to check
#          the SAME visitor_id is reused with is_reentry=True, and added a
#          billing-queue-join + abandon transition case driven purely through the
#          state machine.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Event
from pipeline.detect import SessionStateMachine
from pipeline.tracker import CentroidTracker
from pipeline.zones import ZoneMap, load_zone_map


def _assert_schema(events):
    """Every emitted event must validate against the API's Event schema."""
    for ev in events:
        Event.model_validate(ev)


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #
def test_zone_map_loads_fallback_layout():
    zm = load_zone_map("STORE_BLR_002")
    zone_ids = {z.zone_id for z in zm.zones}
    assert {"ENTRY", "SKINCARE", "MAKEUP", "BILLING"}.issubset(zone_ids)
    assert "BILLING" in zm.billing_zone_ids
    assert "ENTRY" in zm.entry_zone_ids


def test_zone_classify_point_in_region():
    zm = load_zone_map("STORE_BLR_002")
    # Lower-centre maps to ENTRY per the fallback layout regions.
    entry = zm.classify(0.5, 0.9)
    assert entry is not None and entry.zone_id == "ENTRY"
    # Lower-right maps to BILLING.
    billing = zm.classify(0.85, 0.9)
    assert billing is not None and billing.zone_id == "BILLING"


def test_zone_map_unknown_store_uses_defaults():
    zm = load_zone_map("STORE_UNKNOWN_999")
    zone_ids = {z.zone_id for z in zm.zones}
    assert "ENTRY" in zone_ids and "BILLING" in zone_ids  # default regions


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #
def test_tracker_stable_id_across_frames():
    tr = CentroidTracker()
    t0 = tr.update([(0.5, 0.5)], t_s=0.0)
    vid = t0[0].visitor_id
    # Person moves slightly next frame -> same id.
    t1 = tr.update([(0.52, 0.51)], t_s=0.1)
    assert len(t1) == 1
    assert t1[0].visitor_id == vid


def test_tracker_new_person_new_id():
    tr = CentroidTracker()
    tr.update([(0.2, 0.2)], t_s=0.0)
    tracks = tr.update([(0.2, 0.2), (0.8, 0.8)], t_s=0.1)
    ids = {t.visitor_id for t in tracks}
    assert len(ids) == 2


def test_tracker_reentry_reuses_visitor_id():
    tr = CentroidTracker(max_missed=1, reentry_seconds=120, reentry_distance=0.2)
    first = tr.update([(0.5, 0.9)], t_s=0.0)
    vid = first[0].visitor_id
    # Disappear for enough frames to retire the track.
    tr.update([], t_s=1.0)
    tr.update([], t_s=2.0)
    # Reappear nearby shortly after -> reused id, flagged as re-entry.
    again = tr.update([(0.52, 0.88)], t_s=10.0)
    assert again[0].visitor_id == vid
    assert again[0].is_reentry is True


# --------------------------------------------------------------------------- #
# SessionStateMachine -> schema-compliant events
# --------------------------------------------------------------------------- #
def _sm():
    zm = load_zone_map("STORE_BLR_002")
    return SessionStateMachine("STORE_BLR_002", "CAM_FLOOR_01", zm)


def test_entry_then_zone_enter_events():
    sm = _sm()
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    # First sighting in SKINCARE region -> ENTRY then ZONE_ENTER.
    events = sm.observe("VIS_1", "SKINCARE", "product", "MOISTURISER", 0.91, 0.0, base)
    types = [e["event_type"] for e in events]
    assert types[0] == "ENTRY"
    assert "ZONE_ENTER" in types
    _assert_schema(events)
    # Confidence preserved from the detector.
    assert all(e["confidence"] == 0.91 for e in events)


def test_zone_dwell_emitted_every_30s():
    sm = _sm()
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    sm.observe("VIS_1", "SKINCARE", "product", "MOISTURISER", 0.9, 0.0, base)
    # Still in zone 35s later -> a ZONE_DWELL should fire.
    later = sm.observe("VIS_1", "SKINCARE", "product", "MOISTURISER", 0.9, 35.0, base)
    assert any(e["event_type"] == "ZONE_DWELL" for e in later)
    dwell = next(e for e in later if e["event_type"] == "ZONE_DWELL")
    assert dwell["dwell_ms"] >= 30000
    _assert_schema(later)


def test_billing_queue_join_and_abandon():
    sm = _sm()
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    join = sm.observe("VIS_1", "BILLING", "billing", None, 0.88, 0.0, base)
    jtypes = [e["event_type"] for e in join]
    assert "BILLING_QUEUE_JOIN" in jtypes
    qj = next(e for e in join if e["event_type"] == "BILLING_QUEUE_JOIN")
    assert qj["metadata"]["queue_depth"] >= 1
    # Move to a product zone -> abandons the billing queue.
    leave = sm.observe("VIS_1", "MAKEUP", "product", "LIPSTICK", 0.8, 5.0, base)
    assert any(e["event_type"] == "BILLING_QUEUE_ABANDON" for e in leave)
    _assert_schema(join + leave)


def test_finalize_emits_exit():
    sm = _sm()
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    sm.observe("VIS_1", "SKINCARE", "product", "MOISTURISER", 0.9, 0.0, base)
    exits = sm.finalize(60.0, base)
    assert any(e["event_type"] == "EXIT" for e in exits)
    _assert_schema(exits)


def test_low_confidence_event_not_suppressed():
    sm = _sm()
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = sm.observe("VIS_low", "MAKEUP", "product", "LIPSTICK", 0.07, 0.0, base)
    # Low-confidence detection still produces events with the real confidence.
    assert events
    assert any(e["confidence"] == 0.07 for e in events)
    _assert_schema(events)

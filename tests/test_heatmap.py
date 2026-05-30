# PROMPT: "Write pytest tests for GET /stores/{id}/heatmap on a FastAPI + SQLite
#          analytics service. Per zone return visit_frequency, avg dwell, and a
#          normalized_score 0-100; set data_confidence=false when fewer than 20
#          customer sessions are in the window. Cover: empty store (no cells,
#          confidence false), the confidence flag flipping at the 20-session
#          threshold, the busiest/longest zone scoring 100, and staff exclusion."
# CHANGES MADE: Made the threshold test explicit at exactly 20 sessions, added a
#          normalized-score-bounds assertion (0..100), and verified the top zone
#          by combined frequency+dwell maps to 100.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import ingest, make_event


def _ts(base, **kw):
    return (base + timedelta(**kw)).isoformat()


def test_heatmap_empty_store(client):
    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    assert body["session_count"] == 0
    assert body["cells"] == []
    assert body["data_confidence"] is False


def test_heatmap_low_confidence_below_threshold(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    # 5 sessions (< 20) -> data_confidence should be False.
    events = []
    for i in range(5):
        events.append(make_event(visitor_id=f"VIS_{i}", event_type="ZONE_ENTER",
                                  zone_id="SKINCARE", timestamp=_ts(base, minutes=i)))
    ingest(client, events)
    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    assert body["session_count"] == 5
    assert body["data_confidence"] is False


def test_heatmap_confidence_true_at_threshold(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = []
    for i in range(20):  # exactly the MIN_SESSIONS_FOR_CONFIDENCE default
        events.append(make_event(visitor_id=f"VIS_{i}", event_type="ZONE_ENTER",
                                  zone_id="SKINCARE", timestamp=_ts(base, minutes=i)))
    ingest(client, events)
    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    assert body["session_count"] == 20
    assert body["data_confidence"] is True


def test_heatmap_scores_and_top_zone(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = []
    # SKINCARE: visited by 3 visitors with long dwell; MAKEUP: 1 visitor short dwell.
    for i in range(3):
        events.append(make_event(visitor_id=f"VIS_s{i}", event_type="ZONE_DWELL",
                                  zone_id="SKINCARE", dwell_ms=90000,
                                  timestamp=_ts(base, minutes=i)))
    events.append(make_event(visitor_id="VIS_m", event_type="ZONE_DWELL",
                             zone_id="MAKEUP", dwell_ms=20000, timestamp=_ts(base, minutes=10)))
    ingest(client, events)
    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    cells = {c["zone_id"]: c for c in body["cells"]}
    assert cells["SKINCARE"]["visit_frequency"] == 3
    # All scores within 0..100, and SKINCARE (most frequent + longest) is the max.
    for c in body["cells"]:
        assert 0.0 <= c["normalized_score"] <= 100.0
    assert cells["SKINCARE"]["normalized_score"] == 100.0


def test_heatmap_excludes_staff(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_cust", event_type="ZONE_ENTER", zone_id="SKINCARE",
                   timestamp=_ts(base)),
        make_event(visitor_id="VIS_staff", event_type="ZONE_ENTER", zone_id="SKINCARE",
                   is_staff=True, timestamp=_ts(base, seconds=10)),
    ])
    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    assert body["session_count"] == 1  # staff excluded

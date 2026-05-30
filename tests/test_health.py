# PROMPT: "Write pytest tests for a FastAPI GET /health endpoint that reports
#          database status, total event count, and per-store last-event
#          timestamp, raising a STALE_FEED warning when a store's latest event
#          lags more than the configured threshold (default 10 min). Cover the
#          zero-traffic case (no events) and both fresh and stale feeds."
# CHANGES MADE: Added an explicit assertion that the threshold is reported in
#          the payload, switched the stale case to inject an event timestamped
#          well beyond the threshold via the ingest API (end-to-end rather than
#          poking the DB directly), and verified empty-store handling returns a
#          200 with an empty stores list instead of null/500.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.health import _feed_lag_seconds
from tests.conftest import make_event


def test_health_with_no_events(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["event_count"] == 0
    assert body["stores"] == []
    assert body["warnings"] == []
    assert body["stale_feed_threshold_seconds"] == 600


def test_health_reports_fresh_store(client):
    client.post("/events/ingest", json=[make_event(store_id="STORE_BLR_002")])
    body = client.get("/health").json()
    assert body["event_count"] == 1
    stores = {s["store_id"]: s for s in body["stores"]}
    assert "STORE_BLR_002" in stores
    assert stores["STORE_BLR_002"]["stale_feed"] is False
    assert stores["STORE_BLR_002"]["last_event_timestamp"] is not None
    assert stores["STORE_BLR_002"]["lag_seconds"] >= 0
    assert "STALE_FEED:STORE_BLR_002" not in body["warnings"]
    assert "FUTURE_EVENT_TIMESTAMP:STORE_BLR_002" not in body["warnings"]


def test_health_flags_stale_feed(client):
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    client.post(
        "/events/ingest",
        json=[make_event(store_id="STORE_DEL_005", timestamp=old_ts)],
    )
    body = client.get("/health").json()
    stores = {s["store_id"]: s for s in body["stores"]}
    assert stores["STORE_DEL_005"]["stale_feed"] is True
    assert stores["STORE_DEL_005"]["lag_seconds"] > 600
    assert stores["STORE_DEL_005"]["lag_seconds"] >= 0
    assert "STALE_FEED:STORE_DEL_005" in body["warnings"]
    assert "FUTURE_EVENT_TIMESTAMP:STORE_DEL_005" not in body["warnings"]


def test_health_future_event_timestamp_not_negative_lag(client):
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    client.post(
        "/events/ingest",
        json=[make_event(store_id="STORE_BLR_002", timestamp=future_ts)],
    )
    body = client.get("/health").json()
    stores = {s["store_id"]: s for s in body["stores"]}
    assert stores["STORE_BLR_002"]["lag_seconds"] == 0
    assert stores["STORE_BLR_002"]["stale_feed"] is False
    assert "STALE_FEED:STORE_BLR_002" not in body["warnings"]
    assert "FUTURE_EVENT_TIMESTAMP:STORE_BLR_002" in body["warnings"]


def test_health_slightly_future_event_no_warning(client):
    """Events <=60s ahead clamp lag to 0 but do not warn."""
    future_ts = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    client.post(
        "/events/ingest",
        json=[make_event(store_id="STORE_BLR_002", timestamp=future_ts)],
    )
    body = client.get("/health").json()
    stores = {s["store_id"]: s for s in body["stores"]}
    assert stores["STORE_BLR_002"]["lag_seconds"] == 0
    assert stores["STORE_BLR_002"]["stale_feed"] is False
    assert "FUTURE_EVENT_TIMESTAMP:STORE_BLR_002" not in body["warnings"]


def test_feed_lag_seconds_unit_cases():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(seconds=30)
    stale = now - timedelta(minutes=20)
    far_future = now + timedelta(minutes=5)
    near_future = now + timedelta(seconds=30)

    lag, stale_flag, future = _feed_lag_seconds(now, recent)
    assert lag == 30.0
    assert stale_flag is False
    assert future is False

    lag, stale_flag, future = _feed_lag_seconds(now, stale)
    assert lag == 1200.0
    assert stale_flag is True
    assert future is False

    lag, stale_flag, future = _feed_lag_seconds(now, far_future)
    assert lag == 0.0
    assert stale_flag is False
    assert future is True

    lag, stale_flag, future = _feed_lag_seconds(now, near_future)
    assert lag == 0.0
    assert stale_flag is False
    assert future is False


def test_health_tracks_multiple_stores(client):
    client.post(
        "/events/ingest",
        json=[
            make_event(store_id="STORE_BLR_002"),
            make_event(store_id="STORE_DEL_005"),
        ],
    )
    body = client.get("/health").json()
    store_ids = {s["store_id"] for s in body["stores"]}
    assert store_ids == {"STORE_BLR_002", "STORE_DEL_005"}
    assert body["event_count"] == 2

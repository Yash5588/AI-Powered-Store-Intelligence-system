# PROMPT: "Write pytest tests for GET /stores/{id}/metrics on a FastAPI + SQLite
#          store-analytics service. Cover: empty/zero-traffic store returns
#          zeros (no crash, no null), staff events (is_staff=true) excluded from
#          unique_visitors, zero purchases yields conversion_rate 0.0 without a
#          ZeroDivisionError, avg dwell per zone is computed from ZONE_DWELL
#          events, current_queue_depth reflects the latest billing queue_depth,
#          and conversion is credited via POS time-window correlation."
# CHANGES MADE: Wired conversion tests to a temporary POS CSV via the DATA_DIR
#          env override + load_pos_transactions cache reset, asserted the
#          all-staff store reports unique_visitors=0, and added an explicit
#          divide-by-zero guard test (visitors but no transactions).

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import ingest, make_event, set_pos_csv


def _ts(base: datetime, **kw) -> str:
    return (base + timedelta(**kw)).isoformat()


def test_metrics_empty_store_returns_zeros(client):
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["current_queue_depth"] == 0
    assert body["abandonment_rate"] == 0.0
    assert body["avg_dwell_per_zone"] == []
    assert body["data_confidence"] is False


def test_metrics_excludes_staff(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_cust", event_type="ENTRY", timestamp=_ts(base)),
        make_event(visitor_id="VIS_staff", event_type="ZONE_ENTER", zone_id="MAKEUP",
                   is_staff=True, timestamp=_ts(base, seconds=10)),
    ])
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 1  # staff excluded


def test_stockroom_camera_events_do_not_inflate_visitors(client):
    """Events from a staff_only camera (all is_staff=true) never count as visitors.

    Simulates the CAM_STOCKROOM_01 stream (every event flagged is_staff=true)
    arriving alongside one real customer: unique_visitors must stay 1.
    """
    base = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    stockroom = [
        make_event(visitor_id=f"CAM_STOCKROOM_01_VIS_{i:06d}", event_type="ENTRY",
                   is_staff=True, timestamp=_ts(base, seconds=i))
        for i in range(5)
    ]
    customer = [make_event(visitor_id="CAM_ENTRY_01_VIS_000001", event_type="ENTRY",
                           timestamp=_ts(base, seconds=30))]
    ingest(client, stockroom + customer)
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 1  # 5 stockroom staff excluded, 1 customer counted


def test_metrics_zero_purchases_no_division_error(client):
    """Visitors present but no POS transactions -> conversion 0.0, no crash."""
    set_pos_csv(client, [])  # empty POS file
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [make_event(visitor_id=f"VIS_{i}", timestamp=_ts(base, minutes=i)) for i in range(3)])
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 3
    assert body["conversion_rate"] == 0.0
    assert body["converted_visitors"] == 0


def test_metrics_avg_dwell_per_zone(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_a", event_type="ENTRY", timestamp=_ts(base)),
        make_event(visitor_id="VIS_a", event_type="ZONE_DWELL", zone_id="SKINCARE",
                   dwell_ms=40000, timestamp=_ts(base, seconds=40)),
        make_event(visitor_id="VIS_b", event_type="ZONE_DWELL", zone_id="SKINCARE",
                   dwell_ms=60000, timestamp=_ts(base, seconds=80)),
    ])
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    zones = {z["zone_id"]: z for z in body["avg_dwell_per_zone"]}
    assert "SKINCARE" in zones
    assert zones["SKINCARE"]["avg_dwell_ms"] == 50000.0  # (40000 + 60000) / 2


def test_metrics_current_queue_depth(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_a", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 1},
                   timestamp=_ts(base)),
        make_event(visitor_id="VIS_b", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 4, "sku_zone": None, "session_seq": 1},
                   timestamp=_ts(base, seconds=30)),
    ])
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["current_queue_depth"] == 4  # latest wins


def test_metrics_conversion_via_pos_correlation(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    # Visitor in billing at 14:00; transaction at 14:03 (within 5 min) -> converted.
    txn_ts = (base + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_pos_csv(client, [("STORE_BLR_002", "TXN_1", txn_ts, 999.0)])
    ingest(client, [
        make_event(visitor_id="VIS_buyer", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 1},
                   timestamp=_ts(base)),
        make_event(visitor_id="VIS_browser", event_type="ZONE_ENTER", zone_id="SKINCARE",
                   timestamp=_ts(base, minutes=1)),
    ])
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 2
    assert body["converted_visitors"] == 1
    assert body["conversion_rate"] == 0.5

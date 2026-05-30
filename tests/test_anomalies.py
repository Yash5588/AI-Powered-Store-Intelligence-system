# PROMPT: "Write pytest tests for GET /stores/{id}/anomalies on a FastAPI +
#          SQLite analytics service. Detectors: BILLING_QUEUE_SPIKE (current
#          queue depth over threshold), DEAD_ZONE (a defined zone with no visits
#          in 30 min), and CONVERSION_DROP (vs a multi-day baseline). Each
#          anomaly must include anomaly_type, severity (INFO/WARN/CRITICAL),
#          message, suggested_action, and timestamp. Cover empty store (no
#          anomalies, empty list not null) and a clear queue-spike case."
# CHANGES MADE: Asserted the queue spike emits CRITICAL at depth >= 8 and that
#          every returned anomaly carries a non-empty suggested_action, and
#          added a dead-zone case driving traffic to only one zone so the others
#          are reported dead.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import ingest, make_event


def _ts(base, **kw):
    return (base + timedelta(**kw)).isoformat()


def test_anomalies_empty_store_is_empty_list(client):
    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    assert body["anomaly_count"] == 0
    assert body["anomalies"] == []


def test_billing_queue_spike_critical(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_a", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1},
                   timestamp=_ts(base)),
    ])
    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    spike = [a for a in body["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
    assert spike, "expected a queue spike anomaly"
    assert spike[0]["severity"] == "CRITICAL"
    assert spike[0]["suggested_action"]
    assert spike[0]["value"] == 9.0


def test_anomalies_all_carry_suggested_action(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    # Drive traffic only to SKINCARE -> MAKEUP/FRAGRANCE/HAIRCARE are dead zones.
    ingest(client, [
        make_event(visitor_id="VIS_a", event_type="ZONE_ENTER", zone_id="SKINCARE",
                   timestamp=_ts(base)),
        make_event(visitor_id="VIS_a", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 6, "sku_zone": None, "session_seq": 2},
                   timestamp=_ts(base, seconds=30)),
    ])
    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    assert body["anomaly_count"] >= 1
    for a in body["anomalies"]:
        assert a["severity"] in {"INFO", "WARN", "CRITICAL"}
        assert a["message"]
        assert a["suggested_action"]
        assert a["timestamp"]


def test_dead_zone_detected(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_a", event_type="ZONE_ENTER", zone_id="SKINCARE",
                   timestamp=_ts(base)),
    ])
    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    dead = {a["zone_id"] for a in body["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"}
    # MAKEUP was never visited -> flagged dead; SKINCARE was just visited -> not.
    assert "MAKEUP" in dead
    assert "SKINCARE" not in dead

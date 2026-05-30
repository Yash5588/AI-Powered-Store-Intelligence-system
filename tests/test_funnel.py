# PROMPT: "Write pytest tests for GET /stores/{id}/funnel on a FastAPI + SQLite
#          analytics service. The funnel stages are Entry -> Zone Visit ->
#          Billing Queue -> Purchase, counted by visitor SESSION (not raw
#          events), with drop-off % between stages. Cover: empty store returns a
#          zeroed 4-stage funnel, staff excluded, a re-entering visitor (same
#          visitor_id, ENTRY then EXIT then REENTRY) is counted ONCE at Entry,
#          and overall stage monotonicity / drop-off correctness."
# CHANGES MADE: Added an explicit assertion that REENTRY does not create a
#          second Entry-stage session, asserted stage counts are monotonically
#          non-increasing, and verified drop_off_pct is 0 for the first stage.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import ingest, make_event, set_pos_csv


def _ts(base, **kw):
    return (base + timedelta(**kw)).isoformat()


def _stages(body):
    return {s["stage"]: s for s in body["stages"]}


def test_funnel_empty_store(client):
    body = client.get("/stores/STORE_BLR_002/funnel").json()
    assert body["total_sessions"] == 0
    stages = _stages(body)
    assert set(stages) == {"Entry", "Zone Visit", "Billing Queue", "Purchase"}
    assert all(s["count"] == 0 for s in body["stages"])
    assert stages["Entry"]["drop_off_pct"] == 0.0


def test_funnel_excludes_staff(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_cust", event_type="ENTRY", timestamp=_ts(base)),
        make_event(visitor_id="VIS_staff", event_type="ENTRY", is_staff=True,
                   timestamp=_ts(base, seconds=5)),
    ])
    body = client.get("/stores/STORE_BLR_002/funnel").json()
    assert _stages(body)["Entry"]["count"] == 1


def test_reentry_not_double_counted(client):
    """Same visitor_id with ENTRY -> EXIT -> REENTRY is one Entry-stage session."""
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_loop", event_type="ENTRY", timestamp=_ts(base)),
        make_event(visitor_id="VIS_loop", event_type="EXIT", timestamp=_ts(base, minutes=2)),
        make_event(visitor_id="VIS_loop", event_type="REENTRY", timestamp=_ts(base, minutes=5)),
    ])
    body = client.get("/stores/STORE_BLR_002/funnel").json()
    assert body["total_sessions"] == 1
    assert _stages(body)["Entry"]["count"] == 1


def test_funnel_stage_progression_and_dropoff(client):
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = []
    # 4 visitors enter; 3 visit a zone; 2 reach billing.
    for i in range(4):
        vid = f"VIS_{i}"
        events.append(make_event(visitor_id=vid, event_type="ENTRY", timestamp=_ts(base, minutes=i)))
        if i < 3:
            events.append(make_event(visitor_id=vid, event_type="ZONE_ENTER", zone_id="MAKEUP",
                                     timestamp=_ts(base, minutes=i, seconds=30)))
        if i < 2:
            events.append(make_event(visitor_id=vid, event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                                     metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3},
                                     timestamp=_ts(base, minutes=i, seconds=50)))
    ingest(client, events)

    body = client.get("/stores/STORE_BLR_002/funnel").json()
    stages = _stages(body)
    assert stages["Entry"]["count"] == 4
    assert stages["Zone Visit"]["count"] == 3
    assert stages["Billing Queue"]["count"] == 2

    # Counts must be monotonically non-increasing down the funnel.
    counts = [s["count"] for s in body["stages"]]
    assert counts == sorted(counts, reverse=True)
    # Drop-off from Entry(4) -> Zone Visit(3) is 25%.
    assert stages["Zone Visit"]["drop_off_pct"] == 25.0
    # Billing Queue drop-off from Zone Visit(3) -> Billing(2) is 33.33%.
    assert stages["Billing Queue"]["drop_off_pct"] == 33.33


def test_funnel_billing_without_zone_visit_not_counted(client):
    """BILLING_QUEUE_JOIN without ZONE_ENTER/ZONE_DWELL must not inflate Billing Queue."""
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ingest(client, [
        make_event(visitor_id="VIS_skip", event_type="ENTRY", timestamp=_ts(base)),
        make_event(
            visitor_id="VIS_skip", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2},
            timestamp=_ts(base, minutes=1),
        ),
    ])
    stages = _stages(client.get("/stores/STORE_BLR_002/funnel").json())
    assert stages["Entry"]["count"] == 1
    assert stages["Zone Visit"]["count"] == 0
    assert stages["Billing Queue"]["count"] == 0
    assert stages["Purchase"]["count"] == 0


def test_funnel_purchase_cannot_exceed_billing_queue(client):
    """POS conversions are capped at the Billing Queue stage count."""
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = []
    for i in range(2):
        vid = f"VIS_buy{i}"
        events.extend([
            make_event(visitor_id=vid, event_type="ENTRY", timestamp=_ts(base, minutes=i)),
            make_event(visitor_id=vid, event_type="ZONE_ENTER", zone_id="MAKEUP",
                       timestamp=_ts(base, minutes=i, seconds=20)),
            make_event(
                visitor_id=vid, event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3},
                timestamp=_ts(base, minutes=i, seconds=40),
            ),
        ])
    ingest(client, events)
    # Two POS transactions, both within conversion window of billing events.
    set_pos_csv(client, [
        ("STORE_BLR_002", "TXN_1", _ts(base, minutes=0, seconds=90), 500),
        ("STORE_BLR_002", "TXN_2", _ts(base, minutes=1, seconds=90), 600),
    ])
    stages = _stages(client.get("/stores/STORE_BLR_002/funnel").json())
    assert stages["Billing Queue"]["count"] == 2
    assert stages["Purchase"]["count"] <= stages["Billing Queue"]["count"]
    assert stages["Purchase"]["count"] == 2
    assert stages["Purchase"]["drop_off_pct"] == 0.0


def test_funnel_nested_monotonicity(client):
    """Every stage count must be <= the previous stage (nested funnel)."""
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    events = []
    # Mixed paths: some skip zones, some skip billing, one full path with purchase.
    events.append(make_event(visitor_id="VIS_a", event_type="ENTRY", timestamp=_ts(base)))
    events.append(make_event(visitor_id="VIS_b", event_type="ENTRY", timestamp=_ts(base, minutes=1)))
    events.append(make_event(visitor_id="VIS_b", event_type="ZONE_ENTER", zone_id="SKINCARE",
                             timestamp=_ts(base, minutes=1, seconds=30)))
    events.append(make_event(visitor_id="VIS_c", event_type="ENTRY", timestamp=_ts(base, minutes=2)))
    events.append(make_event(visitor_id="VIS_c", event_type="ZONE_ENTER", zone_id="MAKEUP",
                             timestamp=_ts(base, minutes=2, seconds=30)))
    events.append(make_event(
        visitor_id="VIS_c", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
        metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3},
        timestamp=_ts(base, minutes=2, seconds=50),
    ))
    ingest(client, events)
    stages = _stages(client.get("/stores/STORE_BLR_002/funnel").json())
    counts = [stages[name]["count"] for name in ("Entry", "Zone Visit", "Billing Queue", "Purchase")]
    assert counts[0] >= counts[1] >= counts[2] >= counts[3]
    assert counts == [3, 2, 1, 0]

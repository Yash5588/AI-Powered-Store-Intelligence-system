"""Tests for the real-POS adapter in app.loaders and the POS-aligned generator.

Covers:
  * `_parse_date_time` for the Brigade `DD-MM-YYYY` + `HH:MM:SS` format.
  * `load_pos_transactions` auto-detecting and aggregating the rich line-item
    schema (one transaction per `order_id`, basket = sum of `total_amount`).
  * Backward-compatibility with the simple synthetic schema.
  * `pipeline.simulate_events.generate_events_for_pos` producing a nested,
    POS-aligned event stream.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from app import loaders
from app.loaders import _parse_date_time
from pipeline.simulate_events import generate_events_for_pos

_DATA_DIR = Path(os.environ["DATA_DIR"])

RICH_HEADER = (
    "store_id,store_name,order_id,order_date,order_time,dep_name,"
    "salesperson_name,customer_number,total_amount"
)


def _write_pos(text: str) -> None:
    (_DATA_DIR / "pos_transactions.csv").write_text(text, encoding="utf-8")


# --- _parse_date_time -------------------------------------------------------

def test_parse_date_time_ddmmyyyy():
    dt = _parse_date_time("10-04-2026", "12:15:05")
    assert dt == datetime(2026, 4, 10, 12, 15, 5, tzinfo=timezone.utc)


def test_parse_date_time_iso_and_short_time():
    assert _parse_date_time("2026-04-10", "09:30") == datetime(
        2026, 4, 10, 9, 30, 0, tzinfo=timezone.utc
    )


def test_parse_date_time_slashes_and_empty_time():
    assert _parse_date_time("10/04/2026", "") == datetime(
        2026, 4, 10, 0, 0, 0, tzinfo=timezone.utc
    )


# --- rich-schema aggregation ------------------------------------------------

def test_rich_pos_groups_line_items_by_order():
    _write_pos(
        RICH_HEADER + "\n"
        "ST1008,Brigade_Bangalore,1001,10-04-2026,12:15:05,makeup,Asha,9990001,500.00\n"
        "ST1008,Brigade_Bangalore,1001,10-04-2026,12:15:05,skin,Asha,9990001,747.98\n"
        "ST1008,Brigade_Bangalore,1002,10-04-2026,13:00:00,fragrance,Ravi,9990002,198.00\n"
    )
    txns = loaders.load_pos_transactions("ST1008")
    assert len(txns) == 2
    by_id = {t.transaction_id: t for t in txns}
    assert by_id["1001"].basket_value_inr == 1247.98  # 500 + 747.98 summed
    assert by_id["1001"].timestamp == datetime(2026, 4, 10, 12, 15, 5, tzinfo=timezone.utc)
    assert by_id["1002"].basket_value_inr == 198.0


def test_rich_pos_filters_by_store():
    _write_pos(
        RICH_HEADER + "\n"
        "ST1008,Brigade_Bangalore,1001,10-04-2026,12:15:05,makeup,Asha,9990001,500.00\n"
        "ST2222,Other_Store,2001,10-04-2026,12:15:05,makeup,Asha,9990001,900.00\n"
    )
    assert len(loaders.load_pos_transactions("ST1008")) == 1
    assert len(loaders.load_pos_transactions("ST2222")) == 1
    assert len(loaders.load_pos_transactions(None)) == 2


def test_rich_pos_skips_unparseable_date():
    _write_pos(
        RICH_HEADER + "\n"
        "ST1008,Brigade_Bangalore,1001,not-a-date,12:15:05,makeup,Asha,9990001,500.00\n"
    )
    assert loaders.load_pos_transactions("ST1008") == []


# --- simple-schema backward compatibility -----------------------------------

def test_simple_pos_still_parses():
    _write_pos(
        "store_id,transaction_id,timestamp,basket_value_inr\n"
        "STORE_BLR_002,T1,2026-04-10T12:00:00Z,999.5\n"
    )
    txns = loaders.load_pos_transactions("STORE_BLR_002")
    assert len(txns) == 1
    assert txns[0].basket_value_inr == 999.5


def test_empty_pos_file_returns_empty():
    _write_pos("")
    assert loaders.load_pos_transactions("ST1008") == []


def _write_synthetic(text: str) -> None:
    (_DATA_DIR / "synthetic_pos_transactions.csv").write_text(text, encoding="utf-8")


def test_official_layers_over_synthetic_per_store():
    """Official POS wins for its store; synthetic fills stores it doesn't cover."""
    _write_pos(
        RICH_HEADER + "\n"
        "ST1008,Brigade_Bangalore,1001,10-04-2026,12:15:05,makeup,Asha,9990001,500.00\n"
    )
    _write_synthetic(
        "store_id,transaction_id,timestamp,basket_value_inr\n"
        "ST1008,SHOULD_BE_IGNORED,2026-04-10T12:15:05Z,9999.0\n"
        "STORE_BLR_002,DEMO1,2026-04-10T12:00:00Z,250.0\n"
    )
    # ST1008 comes ONLY from the official file (synthetic row ignored).
    st = loaders.load_pos_transactions("ST1008")
    assert [t.transaction_id for t in st] == ["1001"]
    # Demo store still works from the synthetic fallback.
    blr = loaders.load_pos_transactions("STORE_BLR_002")
    assert [t.transaction_id for t in blr] == ["DEMO1"]
    # Unfiltered load returns both stores' transactions.
    assert len(loaders.load_pos_transactions(None)) == 2


# --- POS-aligned generator --------------------------------------------------

class _FakeTxn:
    def __init__(self, tid, ts):
        self.transaction_id = tid
        self.timestamp = ts


def test_generate_events_for_pos_aligns_billing_before_txn():
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
    txns = [_FakeTxn(f"T{i}", base.replace(hour=12 + i)) for i in range(3)]
    events = generate_events_for_pos(
        "ST1008", txns, product_zones=["SKINCARE", "MAKEUP"], seed=1
    )
    assert events, "expected a non-empty event stream"

    # Every transaction must have a billing-queue join 1-3 min before it.
    joins = [e for e in events if e["event_type"] == "BILLING_QUEUE_JOIN"]
    assert len(joins) >= len(txns)
    for txn in txns:
        window_lo = txn.timestamp.timestamp() - 300
        window_hi = txn.timestamp.timestamp()
        matched = any(
            window_lo <= datetime.fromisoformat(j["timestamp"]).timestamp() <= window_hi
            for j in joins
        )
        assert matched, f"no billing join in 5-min window before {txn.transaction_id}"

    # Funnel sanity: every visitor with billing also had an entry.
    entries = {e["visitor_id"] for e in events if e["event_type"] == "ENTRY"}
    billers = {e["visitor_id"] for e in events if e["event_type"] == "BILLING_QUEUE_JOIN"}
    assert billers.issubset(entries)


def test_generate_events_for_pos_empty_returns_empty():
    assert generate_events_for_pos("ST1008", [], seed=1) == []

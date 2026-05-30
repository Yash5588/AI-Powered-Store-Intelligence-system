"""External-file loaders: POS transactions and store layout.

The official files (pos_transactions.csv, store_layout.json) are not yet in the
resource ZIP. To keep the system runnable now and a 1:1 drop-in later, each
loader prefers the OFFICIAL file if present and falls back to the synthetic one
otherwise — no code changes needed when the official files arrive.

Resolution order:
  POS     : data/pos_transactions.csv      -> data/synthetic_pos_transactions.csv
  Layout  : data/store_layout.json         -> data/fallback_store_layout.json
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# data/ lives one level up from app/.
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

POS_OFFICIAL = DATA_DIR / "pos_transactions.csv"
POS_FALLBACK = DATA_DIR / "synthetic_pos_transactions.csv"
LAYOUT_OFFICIAL = DATA_DIR / "store_layout.json"
LAYOUT_FALLBACK = DATA_DIR / "fallback_store_layout.json"


@dataclass(frozen=True)
class PosTransaction:
    store_id: str
    transaction_id: str
    timestamp: datetime  # tz-aware UTC
    basket_value_inr: float


def _parse_ts(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp into tz-aware UTC."""
    raw = raw.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date_time(date_raw: str, time_raw: str) -> datetime:
    """Parse a real-POS ``order_date`` + ``order_time`` into tz-aware UTC.

    Handles ``DD-MM-YYYY`` / ``YYYY-MM-DD`` / ``DD/MM/YYYY`` dates with an
    ``HH:MM:SS`` (or ``HH:MM``) time. Naive values are treated as UTC.
    """
    date_raw = (date_raw or "").strip()
    time_raw = (time_raw or "").strip() or "00:00:00"
    if time_raw.count(":") == 1:
        time_raw += ":00"

    day = month = year = None
    if "-" in date_raw:
        parts = date_raw.split("-")
    elif "/" in date_raw:
        parts = date_raw.split("/")
    else:
        parts = [date_raw]
    if len(parts) == 3:
        a, b, c = (p.strip() for p in parts)
        if len(a) == 4:  # YYYY-MM-DD
            year, month, day = int(a), int(b), int(c)
        else:            # DD-MM-YYYY (Brigade format)
            day, month, year = int(a), int(b), int(c)
    else:
        raise ValueError(f"unparseable date: {date_raw!r}")

    h, m, s = (int(x) for x in time_raw.split(":")[:3])
    return datetime(year, month, day, h, m, s, tzinfo=timezone.utc)


# Columns that mark the rich "real POS" line-item schema (Brigade export).
_RICH_POS_MARKERS = {"order_id", "order_date", "order_time", "total_amount"}


def resolve_pos_path() -> Path | None:
    if POS_OFFICIAL.exists():
        return POS_OFFICIAL
    if POS_FALLBACK.exists():
        return POS_FALLBACK
    return None


def resolve_layout_path() -> Path | None:
    if LAYOUT_OFFICIAL.exists():
        return LAYOUT_OFFICIAL
    if LAYOUT_FALLBACK.exists():
        return LAYOUT_FALLBACK
    return None


def load_pos_transactions(store_id: str | None = None) -> list[PosTransaction]:
    """Load POS transactions (optionally filtered by store). Missing -> empty.

    Each file's schema is auto-detected so the official file drops in without
    code changes:

    1. **Simple** (one row per transaction): ``store_id, transaction_id,
       timestamp, basket_value_inr`` — used by the synthetic fallback.
    2. **Rich line-item export** (the real Brigade POS): one row per SKU with
       ``order_id, order_date, order_time, total_amount, store_id`` — line-items
       are grouped into a single transaction per ``order_id`` (basket value =
       sum of ``total_amount``, timestamp = order date+time).

    **Layering:** the official file is *layered over* the synthetic fallback per
    ``(store_id, transaction_id)`` — official rows always win, and the synthetic
    file fills in stores the official file doesn't cover. This means a store with
    real POS uses it, while demo stores (only in the synthetic file) still
    produce a real, computed conversion rate.
    """
    official: list[PosTransaction] = (
        _parse_pos_file(POS_OFFICIAL, store_id) if POS_OFFICIAL.exists() else []
    )
    # The official file is authoritative for every store it contains; synthetic
    # rows only fill in stores the official file does not cover at all.
    official_stores = {t.store_id for t in official}
    synthetic: list[PosTransaction] = (
        [t for t in _parse_pos_file(POS_FALLBACK, store_id) if t.store_id not in official_stores]
        if POS_FALLBACK.exists()
        else []
    )

    txns = official + synthetic
    txns.sort(key=lambda t: t.timestamp)
    return txns


def _parse_pos_file(path: Path, store_id: str | None) -> list[PosTransaction]:
    """Parse a single POS file, auto-detecting simple vs. rich line-item schema."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [
            {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in reader
        ]
    if not rows:
        return []
    header = set(rows[0].keys())
    if _RICH_POS_MARKERS.issubset(header):
        return _aggregate_rich_pos(rows, store_id)
    return _parse_simple_pos(rows, store_id)


def _parse_simple_pos(rows: list[dict], store_id: str | None) -> list[PosTransaction]:
    txns: list[PosTransaction] = []
    for norm in rows:
        sid = norm.get("store_id")
        ts_raw = norm.get("timestamp")
        if not sid or not ts_raw:
            continue
        if store_id is not None and sid != store_id:
            continue
        try:
            ts = _parse_ts(ts_raw)
            value = float(norm.get("basket_value_inr") or norm.get("amount") or 0.0)
        except (ValueError, TypeError):
            continue  # skip malformed rows rather than crashing analytics
        txns.append(
            PosTransaction(
                store_id=sid,
                transaction_id=norm.get("transaction_id", ""),
                timestamp=ts,
                basket_value_inr=value,
            )
        )
    return txns


def _aggregate_rich_pos(rows: list[dict], store_id: str | None) -> list[PosTransaction]:
    """Group real-POS line-items into one transaction per order_id."""
    orders: dict[str, dict] = {}
    for norm in rows:
        sid = norm.get("store_id")
        oid = norm.get("order_id")
        if not sid or not oid:
            continue
        if store_id is not None and sid != store_id:
            continue
        try:
            amount = float(norm.get("total_amount") or 0.0)
        except (ValueError, TypeError):
            amount = 0.0
        agg = orders.get(oid)
        if agg is None:
            try:
                ts = _parse_date_time(norm.get("order_date", ""), norm.get("order_time", ""))
            except (ValueError, TypeError):
                continue  # cannot place this order in time -> skip
            orders[oid] = {"store_id": sid, "timestamp": ts, "value": amount}
        else:
            agg["value"] += amount

    return [
        PosTransaction(
            store_id=o["store_id"],
            transaction_id=oid,
            timestamp=o["timestamp"],
            basket_value_inr=round(o["value"], 2),
        )
        for oid, o in orders.items()
    ]


@lru_cache(maxsize=1)
def load_store_layout() -> dict:
    """Load the store layout JSON. Missing file -> minimal empty structure."""
    path = resolve_layout_path()
    if path is None:
        return {"stores": []}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def get_store_zones(store_id: str) -> list[str]:
    """Return the list of zone_ids defined for a store (empty if unknown)."""
    layout = load_store_layout()
    for store in layout.get("stores", []):
        if store.get("store_id") == store_id:
            return [z.get("zone_id") for z in store.get("zones", []) if z.get("zone_id")]
    return []


def get_billing_zone_ids(store_id: str) -> set[str]:
    """Billing zone ids for a store. Falls back to the conventional 'BILLING'."""
    layout = load_store_layout()
    for store in layout.get("stores", []):
        if store.get("store_id") == store_id:
            billing = {
                z.get("zone_id")
                for z in store.get("zones", [])
                if z.get("type") == "billing" and z.get("zone_id")
            }
            if billing:
                return billing
    return {"BILLING"}

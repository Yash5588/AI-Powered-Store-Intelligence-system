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
    """Load POS rows (optionally filtered by store). Missing file -> empty list.

    Tolerant of header/column variations so the official file drops in cleanly.
    """
    path = resolve_pos_path()
    if path is None:
        return []

    txns: list[PosTransaction] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # Normalise keys defensively (strip/lower) for header tolerance.
            norm = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
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
                continue  # skip malformed POS rows rather than crashing analytics
            txns.append(
                PosTransaction(
                    store_id=sid,
                    transaction_id=norm.get("transaction_id", ""),
                    timestamp=ts,
                    basket_value_inr=value,
                )
            )
    txns.sort(key=lambda t: t.timestamp)
    return txns


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

"""Shared pytest fixtures.

Two things must be configured BEFORE the app is imported so module-level
constants bind to them:
  * DATABASE_URL  -> a throwaway SQLite file (never touch a dev DB)
  * DATA_DIR      -> a temp data dir so tests own the POS / layout files

We copy the real fallback layout into the temp DATA_DIR (so billing/zones are
defined for the analytics) and give tests a `set_pos_csv` helper to control
conversion correlation deterministically.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# --- configure env BEFORE importing the app ---------------------------------
_TMP_DIR = Path(tempfile.mkdtemp(prefix="si_test_"))
_TMP_DB = _TMP_DIR / "test.db"
_DATA_DIR = _TMP_DIR / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DATA_DIR"] = str(_DATA_DIR)
os.environ.setdefault("STALE_FEED_THRESHOLD_SECONDS", "600")
# Use ALL stored events for a store (no lower time bound) so fixed-timestamp
# test data is always inside the analytics window.
os.environ["ANALYTICS_WINDOW_MINUTES"] = "0"

# Copy the project's fallback layout into the test DATA_DIR.
_PROJECT_DATA = Path(__file__).resolve().parent.parent / "data"
shutil.copy(
    _PROJECT_DATA / "fallback_store_layout.json",
    _DATA_DIR / "fallback_store_layout.json",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import loaders  # noqa: E402
from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Recreate a clean schema and reset loader caches before each test."""
    Base.metadata.drop_all(bind=engine)
    init_db()
    loaders.load_store_layout.cache_clear()
    # Start each test with NO POS data unless the test sets it.
    for name in ("pos_transactions.csv", "synthetic_pos_transactions.csv"):
        p = _DATA_DIR / name
        if p.exists():
            p.unlink()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def make_event(**overrides) -> dict:
    """Build a schema-valid event dict; override any field per test."""
    event = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_test01",
        "event_type": "ENTRY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    event.update(overrides)
    return event


def ingest(client: TestClient, events: list[dict]):
    """POST a batch and assert it was structurally accepted (200)."""
    resp = client.post("/events/ingest", json=events)
    assert resp.status_code == 200, resp.text
    return resp.json()


def set_pos_csv(client, rows: list[tuple]) -> None:
    """Write the synthetic POS CSV used by conversion correlation.

    rows: iterable of (store_id, transaction_id, iso_timestamp, basket_value).
    Pass [] to write a header-only (zero-purchase) file.
    """
    path = _DATA_DIR / "synthetic_pos_transactions.csv"
    lines = ["store_id,transaction_id,timestamp,basket_value_inr"]
    for store_id, txn_id, ts, value in rows:
        lines.append(f"{store_id},{txn_id},{ts},{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

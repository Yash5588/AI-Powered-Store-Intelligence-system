# PROMPT: Improve pipeline coverage — test simulate_events.py with small
#          deterministic inputs; mock network POST; no real video.
# CHANGES MADE: generate_events POS correlation, write_jsonl/write_pos_csv,
#          append/reset POS, main() CLI paths, post_events batching.

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Event
from pipeline.simulate_events import (
    generate_events,
    main,
    post_events,
    write_jsonl,
    write_pos_csv,
)


def _assert_all_schema(events: list[dict]) -> None:
    for ev in events:
        Event.model_validate(ev)


def test_generate_events_reproducible_with_seed():
    start = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    ev1, pos1 = generate_events("STORE_BLR_002", n_visitors=5, start=start, seed=99)
    ev2, pos2 = generate_events("STORE_BLR_002", n_visitors=5, start=start, seed=99)
    assert len(ev1) == len(ev2)
    assert len(pos1) == len(pos2)
    assert ev1[0]["event_type"] == ev2[0]["event_type"]


def test_generate_events_includes_staff_and_visitors():
    start = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    events, pos = generate_events("STORE_BLR_002", n_visitors=3, start=start, seed=7)
    assert len(events) > 0
    staff = [e for e in events if e["is_staff"]]
    visitors = {e["visitor_id"] for e in events if not e["is_staff"]}
    assert len(staff) >= 2
    assert len(visitors) >= 3
    _assert_all_schema(events)


def test_generate_events_pos_follows_completed_billing():
    start = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    events, pos = generate_events("STORE_BLR_002", n_visitors=20, start=start, seed=42)
    billing_joins = [
        e for e in events
        if e["event_type"] == "BILLING_QUEUE_JOIN" and not e["is_staff"]
    ]
    abandons = {
        e["visitor_id"]
        for e in events
        if e["event_type"] == "BILLING_QUEUE_ABANDON"
    }
    completed = [e for e in billing_joins if e["visitor_id"] not in abandons]
    assert len(pos) == len(completed)
    assert all(t["store_id"] == "STORE_BLR_002" for t in pos)
    assert all(t["transaction_id"].startswith("TXN_SIM_") for t in pos)


def test_write_jsonl_creates_file(tmp_path):
    events = [{"event_id": "e1", "event_type": "ENTRY"}]
    out = tmp_path / "gen.jsonl"
    write_jsonl(events, out)
    assert out.read_text(encoding="utf-8").strip() == json.dumps(events[0])


def test_write_pos_csv_create_and_append(tmp_path):
    path = tmp_path / "pos.csv"
    rows = [
        {
            "store_id": "STORE_BLR_002",
            "transaction_id": "TXN_1",
            "timestamp": "2026-05-30T10:00:00Z",
            "basket_value_inr": 100.0,
        }
    ]
    write_pos_csv(rows, path)
    write_pos_csv(
        [
            {
                "store_id": "STORE_BLR_002",
                "transaction_id": "TXN_2",
                "timestamp": "2026-05-30T10:05:00Z",
                "basket_value_inr": 200.0,
            }
        ],
        path,
        append=True,
    )
    with path.open(encoding="utf-8") as fh:
        reader = list(csv.DictReader(fh))
    assert len(reader) == 2
    assert {r["transaction_id"] for r in reader} == {"TXN_1", "TXN_2"}


def test_post_events_batches_and_parses_response():
    events = [{"event_id": f"e{i}", "event_type": "ENTRY"} for i in range(3)]
    payload = json.dumps({"accepted": 3, "duplicates": 0, "rejected": 0}).encode("utf-8")

    def _fake_urlopen(_req):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("pipeline.simulate_events.urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
        post_events(events, "http://localhost:8000/", batch_size=2)
    assert mock_open.call_count == 2


def test_main_writes_events_and_pos(tmp_path, capsys):
    out = tmp_path / "generated_events.jsonl"
    pos = tmp_path / "synthetic_pos_transactions.csv"
    rc = main(
        [
            "--store", "STORE_BLR_002",
            "--visitors", "5",
            "--seed", "1",
            "--out", str(out),
            "--reset-pos",
        ]
    )
    assert rc == 0
    assert out.exists()
    assert pos.exists()
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) > 0
    captured = capsys.readouterr()
    assert "Generated" in captured.out
    assert "Reset" in captured.out


def test_main_no_pos_flag_skips_csv(tmp_path, capsys):
    out = tmp_path / "generated_events.jsonl"
    pos = tmp_path / "synthetic_pos_transactions.csv"
    main(["--store", "STORE_BLR_002", "--visitors", "3", "--seed", "2", "--out", str(out), "--no-pos"])
    assert out.exists()
    assert not pos.exists()


def test_main_leaves_official_pos_untouched(tmp_path, capsys):
    out = tmp_path / "generated_events.jsonl"
    official = tmp_path / "pos_transactions.csv"
    official.write_text("store_id,transaction_id,timestamp,basket_value_inr\n", encoding="utf-8")
    synthetic = tmp_path / "synthetic_pos_transactions.csv"
    main(["--store", "STORE_BLR_002", "--visitors", "3", "--seed", "3", "--out", str(out)])
    captured = capsys.readouterr()
    assert "Official POS file present" in captured.out
    assert not synthetic.exists()


def test_main_posts_when_post_url_given(tmp_path, capsys):
    out = tmp_path / "generated_events.jsonl"
    payload = json.dumps({"accepted": 1, "duplicates": 0, "rejected": 0}).encode("utf-8")

    def _fake_urlopen(_req):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("pipeline.simulate_events.urllib.request.urlopen", side_effect=_fake_urlopen):
        rc = main(
            [
                "--store", "STORE_BLR_002",
                "--visitors", "2",
                "--seed", "5",
                "--out", str(out),
                "--no-pos",
                "--post", "http://localhost:8000",
            ]
        )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Posting to" in captured.out
    assert "accepted" in captured.out

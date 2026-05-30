# PROMPT: Test pipeline/ingest_jsonl.py — JSONL parsing, batching, HTTP errors.
# CHANGES MADE: Unit tests with mocked urlopen; no live API required.

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from pipeline.ingest_jsonl import ingest_file, read_jsonl


def test_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":"a"}\n\n{"event_id":"b"}\n', encoding="utf-8")
    events = read_jsonl(path)
    assert len(events) == 2


def test_read_jsonl_invalid_json_exits(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_jsonl(path)


def test_ingest_file_batches_and_totals(tmp_path):
    path = tmp_path / "events.jsonl"
    lines = [json.dumps({"event_id": f"e{i}", "event_type": "ENTRY"}) for i in range(3)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    responses = [
        {"received": 2, "accepted": 2, "duplicates": 0, "rejected": 0},
        {"received": 1, "accepted": 1, "duplicates": 0, "rejected": 0},
    ]
    call_count = {"n": 0}

    def _fake_urlopen(_req):
        resp = MagicMock()
        body = responses[call_count["n"]]
        call_count["n"] += 1
        resp.read.return_value = json.dumps(body).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("pipeline.ingest_jsonl.request.urlopen", side_effect=_fake_urlopen):
        totals = ingest_file(path, "http://localhost:8000/events/ingest", batch_size=2)

    assert call_count["n"] == 2
    assert totals == {"received": 3, "accepted": 3, "duplicates": 0, "rejected": 0}


def test_ingest_file_http_error_exits(tmp_path):
    path = tmp_path / "one.jsonl"
    path.write_text('{"event_id":"e1"}\n', encoding="utf-8")

    def _raise_http(_req):
        raise HTTPError(
            "http://localhost:8000/events/ingest",
            422,
            "Unprocessable",
            hdrs=None,
            fp=BytesIO(b'{"error":"bad"}'),
        )

    with patch("pipeline.ingest_jsonl.request.urlopen", side_effect=_raise_http):
        with pytest.raises(SystemExit):
            ingest_file(path, "http://localhost:8000/events/ingest")

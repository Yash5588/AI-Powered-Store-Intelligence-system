# PROMPT: Improve pipeline coverage — test emit.py sinks and event builders
#          without real video or network I/O (mock urlopen).
# CHANGES MADE: JsonlSink, ApiSink batching/flush, MultiSink fan-out,
#          build_event schema, clip_start_time, frame_time helpers.

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Event
from pipeline.emit import (
    ApiSink,
    JsonlSink,
    MultiSink,
    build_event,
    clip_start_time,
    frame_time,
)


def test_build_event_matches_api_schema():
    ts = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    ev = build_event(
        store_id="STORE_BLR_002",
        camera_id="CAM_FLOOR_01",
        visitor_id="VIS_000001",
        event_type="ZONE_ENTER",
        timestamp=ts,
        zone_id="SKINCARE",
        dwell_ms=0,
        confidence=0.9123,
        queue_depth=2,
        sku_zone="MOISTURISER",
        session_seq=3,
    )
    parsed = Event.model_validate(ev)
    assert parsed.store_id == "STORE_BLR_002"
    assert parsed.event_type.value == "ZONE_ENTER"
    assert parsed.metadata.queue_depth == 2
    assert parsed.confidence == 0.9123


def test_clip_start_time_is_utc_without_microseconds():
    ts = clip_start_time("any/path.mp4")
    assert ts.tzinfo is not None
    assert ts.microsecond == 0


def test_frame_time_advances_by_frame_index():
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    t0 = frame_time(base, 0, fps=30.0)
    t1 = frame_time(base, 30, fps=30.0)
    assert t0 == base
    assert (t1 - t0).total_seconds() == pytest.approx(1.0)


def test_frame_time_zero_fps_uses_fallback():
    base = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
    t = frame_time(base, 15, fps=0)
    assert (t - base).total_seconds() == pytest.approx(1.0)


def test_jsonl_sink_writes_lines(tmp_path):
    path = tmp_path / "out" / "events.jsonl"
    sink = JsonlSink(str(path))
    ev = build_event(
        store_id="S1",
        camera_id="C1",
        visitor_id="V1",
        event_type="ENTRY",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    sink.emit(ev)
    sink.close()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "ENTRY"
    assert sink.count == 1


def test_api_sink_flushes_on_batch_boundary():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"accepted":2,"duplicates":0,"rejected":0}'
    with patch("pipeline.emit.urlrequest.urlopen", return_value=mock_resp) as mock_open:
        sink = ApiSink("http://localhost:8000", batch_size=2)
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(2):
            sink.emit(
                build_event(
                    store_id="S1",
                    camera_id="C1",
                    visitor_id=f"V{i}",
                    event_type="ENTRY",
                    timestamp=ts,
                )
            )
        # Auto-flush at batch boundary should have cleared the buffer.
        assert sink.count == 2
        mock_open.assert_called_once()
        sink.close()


def test_api_sink_flush_noop_on_empty_buffer():
    with patch("pipeline.emit.urlrequest.urlopen") as mock_open:
        sink = ApiSink("http://localhost:8000")
        sink.flush()
        mock_open.assert_not_called()


def test_api_sink_close_flushes_remaining():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{}'
    with patch("pipeline.emit.urlrequest.urlopen", return_value=mock_resp) as mock_open:
        sink = ApiSink("http://localhost:8000", batch_size=500)
        sink.emit(
            build_event(
                store_id="S1",
                camera_id="C1",
                visitor_id="V1",
                event_type="ENTRY",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        sink.close()
        mock_open.assert_called_once()


def test_multi_sink_fans_out_to_all_sinks(tmp_path):
    path = tmp_path / "events.jsonl"
    jsonl = JsonlSink(str(path))
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{}'
    with patch("pipeline.emit.urlrequest.urlopen", return_value=mock_resp):
        api = ApiSink("http://localhost:8000", batch_size=500)
        multi = MultiSink([jsonl, api])
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ev = build_event(
            store_id="S1",
            camera_id="C1",
            visitor_id="V1",
            event_type="ENTRY",
            timestamp=ts,
        )
        multi.emit(ev)
        multi.close()
    assert multi.count == 1
    assert jsonl.count == 1
    assert api.count == 1
    assert "ENTRY" in path.read_text(encoding="utf-8")

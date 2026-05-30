"""Event construction + emission  [PHASE 4].

Builds events in EXACTLY the schema the API validates (app/models.Event) and
sends them to a sink:

  * JsonlSink  - append schema-valid lines to data/generated_events.jsonl
  * ApiSink    - POST batches of <=500 to /events/ingest
  * MultiSink  - both at once

The builders here are the single place the pipeline encodes the event contract,
so detect.py stays focused on vision + tracking.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib import request as urlrequest

MAX_BATCH = 500


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(
    *,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 1,
) -> dict:
    """Construct one schema-compliant event dict."""
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _iso(timestamp),
        "zone_id": zone_id,
        "dwell_ms": int(dwell_ms),
        "is_staff": bool(is_staff),
        "confidence": round(float(confidence), 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def clip_start_time(video_path: str) -> datetime:
    """Derive a deterministic clip start timestamp.

    Real CCTV carries a wall-clock start; our anonymised clips don't, so we
    anchor each run to "now minus clip length" lazily — detect.py adds the
    per-frame offset. Here we just provide a stable base (UTC now, truncated).
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def frame_time(base: datetime, frame_index: int, fps: float) -> datetime:
    """Timestamp for a given frame = clip base + (frame / fps) seconds."""
    fps = fps if fps and fps > 0 else 15.0
    return base + timedelta(seconds=frame_index / fps)


class JsonlSink:
    """Append events as JSON lines to a file."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self.count = 0

    def emit(self, event: dict) -> None:
        self._fh.write(json.dumps(event) + "\n")
        self.count += 1

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


class ApiSink:
    """Buffer events and POST them to /events/ingest in batches of <=500."""

    def __init__(self, base_url: str, batch_size: int = MAX_BATCH):
        self.url = base_url.rstrip("/") + "/events/ingest"
        self.batch_size = min(batch_size, MAX_BATCH)
        self._buf: list[dict] = []
        self.count = 0

    def emit(self, event: dict) -> None:
        self._buf.append(event)
        self.count += 1
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        data = json.dumps(self._buf).encode("utf-8")
        req = urlrequest.Request(
            self.url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlrequest.urlopen(req) as resp:  # noqa: S310 - local trusted URL
            resp.read()
        self._buf.clear()

    def close(self) -> None:
        self.flush()


class MultiSink:
    """Fan out events to multiple sinks."""

    def __init__(self, sinks: list):
        self.sinks = sinks

    @property
    def count(self) -> int:
        return max((s.count for s in self.sinks), default=0)

    def emit(self, event: dict) -> None:
        for s in self.sinks:
            s.emit(event)

    def close(self) -> None:
        for s in self.sinks:
            s.close()

"""In-memory video processing job manager.

Wraps the EXISTING ``pipeline.detect.run_detection`` (no detection logic is
duplicated here) in a background thread so the React frontend can upload a clip,
kick off processing, and poll for live progress.

Design / limitations (hackathon-acceptable, documented):
  * Job state lives **in memory** (a process-local dict). It is lost on API
    restart and does not span multiple workers/replicas. For production this
    would move to SQLite/Redis + a real task queue (Celery/RQ). The seam is the
    ``JobStore`` class, so swapping the backing store is contained.
  * Detection runs in a daemon thread (FastAPI BackgroundTasks would also work,
    but a thread lets us cap concurrency and expose live counters).
  * The heavy CV wheels (OpenCV/Ultralytics) are imported lazily inside the
    worker, exactly as the CLI does, so importing this module is cheap and the
    API stays importable without them.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Where uploads + per-job outputs live. Overridable for tests / containers.
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
JOB_OUTPUT_DIR = DATA_DIR / "outputs"

# Base URL the worker posts generated events to (the API itself).
SELF_API_URL = os.getenv("SELF_API_URL", "http://localhost:8000")

ALLOWED_VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
MAX_EVENTS_KEPT = 200  # recent events kept in memory per job for the UI


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class VideoJob:
    job_id: str
    filename: str
    video_path: str
    status: str = "queued"  # queued | running | completed | failed
    store_id: Optional[str] = None
    camera_id: Optional[str] = None
    frames_processed: int = 0
    frames_written: int = 0
    events_emitted: int = 0
    events_ingested: int = 0
    output_jsonl_path: Optional[str] = None
    annotated_video_path: Optional[str] = None
    latest_frame_path: Optional[str] = None
    model_name: str = "yolov8n.pt"
    conf_threshold: float = 0.25
    error: Optional[str] = None
    last_flush_at: Optional[str] = None
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def public(self) -> dict:
        d = asdict(self)
        # Don't leak absolute filesystem paths to the browser; expose booleans.
        d.pop("video_path", None)
        d.pop("output_jsonl_path", None)
        d.pop("annotated_video_path", None)
        d.pop("latest_frame_path", None)
        d["has_annotated_video"] = bool(
            self.annotated_video_path and Path(self.annotated_video_path).exists()
        )
        d["has_latest_frame"] = bool(
            self.latest_frame_path and Path(self.latest_frame_path).exists()
        )
        return d


class JobStore:
    """Thread-safe in-memory job registry. Swap this out for SQLite/Redis later."""

    def __init__(self) -> None:
        self._jobs: dict[str, VideoJob] = {}
        self._events: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, video_path: str) -> VideoJob:
        job_id = uuid.uuid4().hex[:12]
        job = VideoJob(job_id=job_id, filename=filename, video_path=video_path)
        with self._lock:
            self._jobs[job_id] = job
            self._events[job_id] = []
        return job

    def get(self, job_id: str) -> Optional[VideoJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[VideoJob]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = _utcnow()

    def append_events(self, job_id: str, events: list[dict]) -> None:
        with self._lock:
            buf = self._events.setdefault(job_id, [])
            buf.extend(events)
            if len(buf) > MAX_EVENTS_KEPT:
                del buf[: len(buf) - MAX_EVENTS_KEPT]

    def recent_events(self, job_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._events.get(job_id, []))[-limit:]


# Module-level singleton store.
store = JobStore()
_restored_from_disk = False


def restore_jobs_from_disk() -> int:
    """Rebuild minimal job state from upload/output files after an API restart."""
    global _restored_from_disk
    restored = 0
    if _restored_from_disk:
        return restored
    _restored_from_disk = True
    if not UPLOAD_DIR.exists():
        return restored

    for upload in sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not upload.is_file() or "_" not in upload.name:
            continue
        job_id, filename = upload.name.split("_", 1)
        if len(job_id) != 12 or not all(c in "0123456789abcdef" for c in job_id):
            continue
        if store.get(job_id) is not None:
            continue

        jsonl_path = JOB_OUTPUT_DIR / f"{job_id}_events.jsonl"
        annotated_path = JOB_OUTPUT_DIR / f"{job_id}_annotated.webm"
        latest_frame_path = JOB_OUTPUT_DIR / f"{job_id}_latest_frame.jpg"
        first_event = _first_event(jsonl_path)
        has_output = jsonl_path.exists() or annotated_path.exists()
        job = VideoJob(
            job_id=job_id,
            filename=filename,
            video_path=str(upload),
            status="completed" if has_output else "failed",
            store_id=first_event.get("store_id") if first_event else None,
            camera_id=first_event.get("camera_id") if first_event else None,
            output_jsonl_path=str(jsonl_path) if jsonl_path.exists() else None,
            annotated_video_path=str(annotated_path) if annotated_path.exists() else None,
            latest_frame_path=str(latest_frame_path) if latest_frame_path.exists() else None,
            events_emitted=_count_jsonl_lines(jsonl_path),
            error=None if has_output else "API restarted before this upload was processed. Start a new run.",
        )
        with store._lock:
            store._jobs[job_id] = job
            store._events[job_id] = []
        if jsonl_path.exists():
            _load_recent_events(job_id, str(jsonl_path))
        restored += 1
    return restored


def _first_event(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            return event if isinstance(event, dict) else None
    return None


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def save_upload(filename: str, data: bytes) -> VideoJob:
    """Persist an uploaded video and register a job for it."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name or "upload.mp4"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise ValueError(
            f"Unsupported video type '{suffix}'. Allowed: {sorted(ALLOWED_VIDEO_SUFFIXES)}"
        )
    job_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{job_id}_{safe_name}"
    dest.write_bytes(data)
    job = VideoJob(job_id=job_id, filename=safe_name, video_path=str(dest))
    with store._lock:  # register the pre-built job (keeps job_id == file prefix)
        store._jobs[job_id] = job
        store._events[job_id] = []
    return job


def start_processing(
    job_id: str,
    *,
    store_id: str,
    camera_id: str,
    layout_path: Optional[str] = None,
    max_frames: Optional[int] = 300,
    save_annotated_video: bool = False,
    all_staff: bool = False,
    model_name: str = "yolov8n.pt",
    conf_threshold: float = 0.25,
) -> bool:
    """Launch detection for a job in a background daemon thread. Returns started."""
    job = store.get(job_id)
    if job is None:
        return False
    if job.status == "running":
        return False

    JOB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = str(JOB_OUTPUT_DIR / f"{job_id}_events.jsonl")
    annotated_path = (
        str(JOB_OUTPUT_DIR / f"{job_id}_annotated.webm") if save_annotated_video else None
    )
    frame_preview_path = str(JOB_OUTPUT_DIR / f"{job_id}_latest_frame.jpg")

    store.update(
        job_id,
        status="running",
        store_id=store_id,
        camera_id=camera_id,
        output_jsonl_path=jsonl_path,
        annotated_video_path=annotated_path,
        latest_frame_path=frame_preview_path,
        model_name=model_name,
        conf_threshold=conf_threshold,
        error=None,
        frames_processed=0,
        frames_written=0,
        events_emitted=0,
        events_ingested=0,
        last_flush_at=None,
    )

    thread = threading.Thread(
        target=_run_job,
        args=(job_id,),
        kwargs=dict(
            video_path=job.video_path,
            store_id=store_id,
            camera_id=camera_id,
            layout_path=layout_path,
            output_path=jsonl_path,
            annotated_video_path=annotated_path,
            latest_frame_path=frame_preview_path,
            max_frames=max_frames,
            all_staff=all_staff,
            model_name=model_name,
            conf_threshold=conf_threshold,
        ),
        daemon=True,
    )
    thread.start()
    return True


def _run_job(
    job_id: str,
    *,
    video_path: str,
    store_id: str,
    camera_id: str,
    layout_path: Optional[str],
    output_path: str,
    annotated_video_path: Optional[str],
    latest_frame_path: Optional[str],
    max_frames: Optional[int],
    all_staff: bool,
    model_name: str,
    conf_threshold: float,
) -> None:
    """Worker body: reuse run_detection, stream progress into the job store."""
    from pipeline.detect import run_detection  # lazy: pulls in CV wheels

    def _progress(frames: int, events: int, written: int) -> None:
        store.update(
            job_id,
            frames_processed=frames,
            events_emitted=events,
            events_ingested=events,
            frames_written=written,
            last_flush_at=_utcnow(),
        )
        _load_recent_events(job_id, output_path)

    try:
        count = run_detection(
            video_path=video_path,
            store_id=store_id,
            camera_id=camera_id,
            layout_path=layout_path,
            output_path=output_path,
            post_url=SELF_API_URL,  # reuse existing /events/ingest
            annotated_video_path=annotated_video_path,
            latest_frame_path=latest_frame_path,
            max_frames=max_frames,
            all_staff=all_staff,
            model_name=model_name,
            conf_threshold=conf_threshold,
            progress_cb=_progress,
            visitor_prefix=f"{camera_id}_v{job_id[:4]}_VIS_",
        )
        # Capture recent events for the UI from the JSONL the run produced.
        _load_recent_events(job_id, output_path)
        store.update(
            job_id,
            status="completed",
            events_emitted=count,
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")


def _load_recent_events(job_id: str, jsonl_path: str) -> None:
    path = Path(jsonl_path)
    if not path.exists():
        return
    events: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    store.append_events(job_id, events[-MAX_EVENTS_KEPT:])

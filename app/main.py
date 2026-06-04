"""FastAPI entrypoint for the Store Intelligence API.

Phase 1 surface:
  * POST /events/ingest  - batch ingest (<=500), validate, dedup, partial success
  * GET  /health         - DB status + per-store feed freshness (STALE_FEED)
  * GET  /                - service banner

Cross-cutting concerns handled here:
  * a per-request trace_id + one structured log line per request
  * graceful degradation: DB errors -> 503, unexpected errors -> 500,
    never a raw stack trace in the response body
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.anomalies import detect_anomalies
from app.database import get_db, init_db
from app.funnel import compute_funnel
from app.health import build_health
from app.heatmap import compute_heatmap
from app.ingestion import MAX_BATCH_SIZE, dominant_store_id, ingest_events, normalise_batch
from app.logging_config import (
    configure_logging,
    log_request,
    new_trace_id,
    trace_id_var,
)
from app.metrics import compute_store_metrics
from app.models import (
    AnomaliesResponse,
    FunnelResponse,
    HealthResponse,
    HeatmapResponse,
    IngestResult,
    MetricsResponse,
)

logger = configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables on startup."""
    init_db()
    from app import video_jobs

    restored_jobs = video_jobs.restore_jobs_from_disk()
    logger.info("startup", extra={"event": "startup", "version": __version__})
    if restored_jobs:
        logger.info("video_jobs_restored", extra={"event": "video_jobs_restored", "count": restored_jobs})
    yield
    logger.info("shutdown", extra={"event": "shutdown"})


app = FastAPI(
    title="Store Intelligence API",
    version=__version__,
    description="AI-powered Store Intelligence System — real-time offline retail analytics.",
    lifespan=lifespan,
)

# CORS so the React dev server (and the containerised frontend) can call the API
# from the browser. Origins are overridable via CORS_ORIGINS (comma-separated).
_default_origins = (
    "http://localhost:3000,http://127.0.0.1:3000,http://frontend:3000,"
    "http://localhost:5173,http://127.0.0.1:5173"
)
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Middleware: trace id, latency, single structured request log
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
    token = trace_id_var.set(trace_id)
    request.state.store_id = None
    request.state.event_count = None
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Trace-Id"] = trace_id
        return response
    finally:
        latency_ms = (time.perf_counter() - start) * 1000.0
        log_request(
            endpoint=request.url.path,
            method=request.method,
            status_code=status_code,
            latency_ms=latency_ms,
            store_id=getattr(request.state, "store_id", None),
            event_count=getattr(request.state, "event_count", None),
        )
        trace_id_var.reset(token)


# --------------------------------------------------------------------------- #
# Exception handlers: structured bodies, never a raw stack trace
# --------------------------------------------------------------------------- #
def _error_body(code: str, message: str, **extra) -> dict:
    body = {"error": {"code": code, "message": message}, "trace_id": trace_id_var.get()}
    if extra:
        body["error"].update(extra)
    return body


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", "Request body failed validation."),
    )


@app.exception_handler(SQLAlchemyError)
async def db_exception_handler(request: Request, exc: SQLAlchemyError):
    # Graceful degradation: DB problems surface as 503, not 500, and never leak SQL.
    logger.error("database_error", extra={"event": "database_error", "exc": type(exc).__name__})
    return JSONResponse(
        status_code=503,
        content=_error_body("DATABASE_UNAVAILABLE", "The data store is temporarily unavailable."),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_error", extra={"event": "unhandled_error", "exc": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "An unexpected error occurred."),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> dict:
    return {
        "service": "store-intelligence",
        "version": __version__,
        "status": "ok",
        "endpoints": [
            "/events/ingest",
            "/events/clear",
            "/health",
            "/stores/{id}/metrics",
            "/stores/{id}/funnel",
            "/stores/{id}/heatmap",
            "/stores/{id}/anomalies",
            "/videos",
            "/videos/upload",
            "/docs",
        ],
    }


@app.post("/events/ingest", response_model=IngestResult)
async def ingest(request: Request) -> JSONResponse:
    """Ingest a batch of events.

    Body: a JSON array of events, or {"events": [...]}. Max 500 per call.
    Always returns 200 with a per-event verdict unless the batch itself is
    structurally invalid (bad JSON / oversized) -> 422.
    """
    # Parse JSON defensively so we control the error shape (no stack trace).
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JSONResponse(
            status_code=422,
            content=_error_body("INVALID_JSON", "Request body is not valid JSON."),
        )

    try:
        raw_events = normalise_batch(payload)
    except ValueError as exc:
        return JSONResponse(
            status_code=422, content=_error_body("INVALID_BODY", str(exc))
        )

    if len(raw_events) == 0:
        return JSONResponse(
            status_code=422,
            content=_error_body("EMPTY_BATCH", "Batch must contain at least one event."),
        )
    if len(raw_events) > MAX_BATCH_SIZE:
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "BATCH_TOO_LARGE",
                f"Batch exceeds the maximum of {MAX_BATCH_SIZE} events.",
                max_batch_size=MAX_BATCH_SIZE,
                received=len(raw_events),
            ),
        )

    # Attach observability context for the request log line.
    request.state.store_id = dominant_store_id(raw_events)
    request.state.event_count = len(raw_events)

    db = next(get_db())
    try:
        result = ingest_events(db, raw_events)
    finally:
        db.close()

    return JSONResponse(status_code=200, content=json.loads(result.model_dump_json()))


def _analytics_response(request: Request, store_id: str, model) -> JSONResponse:
    """Run an analytics computation in a managed session and serialise it.

    Attaches store_id to the request log line. DB errors propagate to the
    SQLAlchemyError handler (-> structured 503); no stack traces leak.
    """
    request.state.store_id = store_id
    db = next(get_db())
    try:
        report = model(db, store_id)
    finally:
        db.close()
    return JSONResponse(status_code=200, content=json.loads(report.model_dump_json()))


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def store_metrics(store_id: str, request: Request) -> JSONResponse:
    """Live store metrics: unique visitors, conversion, dwell, queue, abandonment."""
    return _analytics_response(request, store_id, compute_store_metrics)


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def store_funnel(store_id: str, request: Request) -> JSONResponse:
    """Session-based conversion funnel with per-stage drop-off."""
    return _analytics_response(request, store_id, compute_funnel)


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def store_heatmap(store_id: str, request: Request) -> JSONResponse:
    """Per-zone visit frequency + dwell, normalised 0-100, with confidence flag."""
    return _analytics_response(request, store_id, compute_heatmap)


@app.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def store_anomalies(store_id: str, request: Request) -> JSONResponse:
    """Active operational anomalies with severity and suggested actions."""
    return _analytics_response(request, store_id, detect_anomalies)


# --------------------------------------------------------------------------- #
# Database management
# --------------------------------------------------------------------------- #
@app.delete("/events/clear")
async def clear_events() -> JSONResponse:
    """Delete ALL events from the database and reset video job state.

    Intended for demo resets — gives a clean slate without restarting the server.
    """
    from app.models import EventRecord as ER

    db = next(get_db())
    try:
        count = db.query(ER).count()
        db.query(ER).delete()
        db.commit()
    finally:
        db.close()

    # Also clear the in-memory video-job registry so the UI starts fresh.
    try:
        from app import video_jobs
        video_jobs.store._jobs.clear()
        video_jobs.store._events.clear()
    except Exception:
        pass  # nosec B110 - non-critical; events table is the source of truth

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "deleted_events": count},
    )


# --------------------------------------------------------------------------- #
# Video processing jobs (upload -> background detection -> live status)
# --------------------------------------------------------------------------- #
class ProcessRequest(BaseModel):
    store_id: str = Field(default="ST1008")
    camera_id: str = Field(default="CAM_FLOOR_A_01")
    layout_path: str | None = Field(default="data/store_layout.json")
    max_frames: int | None = Field(default=300)
    save_annotated_video: bool = Field(default=False)
    all_staff: bool = Field(default=False)
    model: Literal["yolov8n.pt", "yolov8s.pt", "yolov8m.pt", "yolo11s.pt", "yolo11m.pt"] = Field(
        default="yolov8n.pt"
    )
    conf: float = Field(default=0.25, ge=0.05, le=0.9)


@app.post("/videos/upload")
async def upload_video(file: UploadFile = File(...)) -> JSONResponse:
    """Accept a CCTV clip, store it locally, and register a processing job."""
    from app import video_jobs

    data = await file.read()
    if not data:
        return JSONResponse(status_code=422, content=_error_body("EMPTY_FILE", "Uploaded file is empty."))
    try:
        job = video_jobs.save_upload(file.filename or "upload.mp4", data)
    except ValueError as exc:
        return JSONResponse(status_code=422, content=_error_body("UNSUPPORTED_VIDEO", str(exc)))
    return JSONResponse(
        status_code=201,
        content={"job_id": job.job_id, "filename": job.filename, "size_bytes": len(data)},
    )


@app.post("/videos/{job_id}/process")
async def process_video(job_id: str, body: ProcessRequest) -> JSONResponse:
    """Start background detection on a previously uploaded clip."""
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content=_error_body("JOB_NOT_FOUND", f"No job '{job_id}'."))
    # Stockroom is never a customer area: force all_staff for safety.
    all_staff = body.all_staff or body.camera_id == "CAM_STOCKROOM_01"
    started = video_jobs.start_processing(
        job_id,
        store_id=body.store_id,
        camera_id=body.camera_id,
        layout_path=body.layout_path,
        max_frames=body.max_frames,
        save_annotated_video=body.save_annotated_video,
        all_staff=all_staff,
        model_name=body.model,
        conf_threshold=body.conf,
    )
    if not started:
        return JSONResponse(
            status_code=409,
            content=_error_body("JOB_NOT_STARTABLE", "Job is already running or missing."),
        )
    return JSONResponse(status_code=202, content=video_jobs.store.get(job_id).public())


@app.get("/videos/{job_id}/status")
async def video_status(job_id: str) -> JSONResponse:
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content=_error_body("JOB_NOT_FOUND", f"No job '{job_id}'."))
    return JSONResponse(status_code=200, content=job.public())


@app.get("/videos/{job_id}/events")
async def video_events(job_id: str, limit: int = 100) -> JSONResponse:
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content=_error_body("JOB_NOT_FOUND", f"No job '{job_id}'."))
    events = video_jobs.store.recent_events(job_id, limit=limit)
    return JSONResponse(status_code=200, content={"job_id": job_id, "count": len(events), "events": events})


@app.get("/videos/{job_id}/annotated-video")
async def video_annotated(job_id: str):
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None or not job.annotated_video_path:
        return JSONResponse(status_code=404, content=_error_body("NO_VIDEO", "No annotated video for this job."))
    path = video_jobs.Path(job.annotated_video_path)
    if not path.exists():
        return JSONResponse(status_code=404, content=_error_body("NO_VIDEO", "Annotated video not ready."))
    return FileResponse(
        str(path), media_type="video/webm", filename=path.name,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/videos/{job_id}/original-video")
async def video_original(job_id: str):
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None or not job.video_path:
        return JSONResponse(status_code=404, content=_error_body("NO_VIDEO", "No original video for this job."))
    path = video_jobs.Path(job.video_path)
    if not path.exists():
        return JSONResponse(status_code=404, content=_error_body("NO_VIDEO", "Original video not found."))
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@app.get("/videos/{job_id}/latest-frame")
async def video_latest_frame(job_id: str):
    from app import video_jobs

    job = video_jobs.store.get(job_id)
    if job is None or not job.latest_frame_path:
        return JSONResponse(status_code=404, content=_error_body("NO_FRAME", "No live frame available."))
    path = video_jobs.Path(job.latest_frame_path)
    if not path.exists():
        return JSONResponse(status_code=404, content=_error_body("NO_FRAME", "Frame not ready yet."))
    from starlette.responses import Response
    frame_bytes = path.read_bytes()
    return Response(content=frame_bytes, media_type="image/jpeg", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    })

@app.get("/videos")
async def list_videos(limit: int = 50) -> JSONResponse:
    from app import video_jobs

    jobs = [j.public() for j in video_jobs.store.list(limit=limit)]
    return JSONResponse(status_code=200, content={"count": len(jobs), "jobs": jobs})


@app.get("/health", response_model=HealthResponse)
async def health() -> JSONResponse:
    """Service + per-store feed health. Returns 200 ok / 503 when DB is down."""
    db = next(get_db())
    try:
        report = build_health(db)
    finally:
        db.close()

    status_code = 200 if report.database == "up" else 503
    return JSONResponse(status_code=status_code, content=json.loads(report.model_dump_json()))

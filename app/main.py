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
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
    logger.info("startup", extra={"event": "startup", "version": __version__})
    yield
    logger.info("shutdown", extra={"event": "shutdown"})


app = FastAPI(
    title="Store Intelligence API",
    version=__version__,
    description="AI-powered Store Intelligence System — real-time offline retail analytics.",
    lifespan=lifespan,
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
            "/health",
            "/stores/{id}/metrics",
            "/stores/{id}/funnel",
            "/stores/{id}/heatmap",
            "/stores/{id}/anomalies",
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

"""Structured (JSON) logging.

Every request emits one structured log line with the fields the spec requires:
trace_id, store_id (when known), endpoint, latency_ms, event_count (ingest),
and status_code. JSON lines are chosen over plaintext so logs are directly
queryable in an aggregator (Loki/ELK/CloudWatch) by an on-call engineer.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

# Propagates the current request's trace id to any log call within the request.
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)

LOGGER_NAME = "store_intelligence"

# Reserved attributes on a LogRecord we don't want to duplicate as "extra".
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach the active trace id if not explicitly provided on the record.
        tid = getattr(record, "trace_id", None) or trace_id_var.get()
        if tid:
            payload["trace_id"] = tid

        # Promote any structured extras passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "trace_id":
                payload[key] = value

        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", str(record.exc_info[0]))
            payload["exc_message"] = str(record.exc_info[1])

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the application logger. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    # Don't double-emit through the root logger.
    logger.propagate = False
    return logger


def new_trace_id() -> str:
    """Generate a fresh trace id for a request."""
    return uuid.uuid4().hex


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_request(
    *,
    endpoint: str,
    method: str,
    status_code: int,
    latency_ms: float,
    store_id: Optional[str] = None,
    event_count: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Emit the canonical one-line-per-request structured log."""
    fields: dict[str, Any] = {
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
    }
    if store_id is not None:
        fields["store_id"] = store_id
    if event_count is not None:
        fields["event_count"] = event_count
    if extra:
        fields.update(extra)

    get_logger().info("request", extra=fields)

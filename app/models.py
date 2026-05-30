"""Schemas and ORM models for the Store Intelligence System.

Two layers live here:

1. Pydantic models  -> the wire contract (validation, (de)serialisation).
   `Event` matches the problem-statement schema EXACTLY, including the nested
   `metadata` object.
2. SQLAlchemy model -> the storage contract. The nested metadata is flattened
   into columns because `metadata` is a reserved attribute on the SQLAlchemy
   declarative base, and flat columns index/aggregate efficiently for the
   analytics endpoints built in later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class EventType(str, Enum):
    """Catalogue of behavioural events the detection pipeline may emit."""

    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


# --------------------------------------------------------------------------- #
# Pydantic wire models
# --------------------------------------------------------------------------- #
class EventMetadata(BaseModel):
    """Nested `metadata` object from the event schema."""

    model_config = ConfigDict(extra="allow")  # tolerate future detector fields

    queue_depth: Optional[int] = Field(
        default=None, ge=0, description="Set for BILLING_QUEUE_JOIN; else null."
    )
    sku_zone: Optional[str] = Field(
        default=None, description="Zone/SKU label from store_layout.json."
    )
    session_seq: Optional[int] = Field(
        default=None, ge=0, description="Ordinal position of event in the visitor session."
    )


class Event(BaseModel):
    """A single behavioural event — the exact schema the pipeline must emit."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="Globally unique (uuid-v4).")
    store_id: str = Field(..., min_length=1)
    camera_id: str = Field(..., min_length=1)
    visitor_id: str = Field(..., min_length=1, description="Re-ID token, unique per visit session.")
    event_type: EventType
    timestamp: datetime = Field(..., description="ISO-8601 UTC.")
    zone_id: Optional[str] = Field(default=None, description="null for ENTRY / EXIT events.")
    dwell_ms: int = Field(default=0, ge=0, description="Duration; 0 for instantaneous events.")
    is_staff: bool = Field(default=False)
    confidence: float = Field(..., ge=0.0, le=1.0, description="Detection confidence; not suppressed.")
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        """Normalise all timestamps to timezone-aware UTC.

        Naive timestamps are assumed UTC (the schema specifies UTC). This keeps
        lag math in /health and time-window joins in later phases unambiguous.
        """
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class IngestRequest(BaseModel):
    """Batch ingest payload. Hard cap of 500 events per the spec."""

    model_config = ConfigDict(extra="forbid")

    events: list[Event] = Field(..., min_length=1, max_length=500)


# --------------------------------------------------------------------------- #
# Ingest response models (structured, no stack traces)
# --------------------------------------------------------------------------- #
class RejectedEvent(BaseModel):
    """One rejected event with a stable, machine-readable reason."""

    index: int = Field(..., description="Position of the event in the submitted batch.")
    event_id: Optional[str] = None
    reason: str
    detail: Optional[str] = None


class IngestResult(BaseModel):
    """Summary of an ingest call. Partial success is the norm, not an error."""

    received: int
    accepted: int
    duplicates: int
    rejected: int
    accepted_ids: list[str] = Field(default_factory=list)
    duplicate_ids: list[str] = Field(default_factory=list)
    rejected_events: list[RejectedEvent] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Health response models
# --------------------------------------------------------------------------- #
class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_timestamp: Optional[datetime] = None
    lag_seconds: Optional[float] = None
    stale_feed: bool = False


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    database: str  # "up" | "down"
    event_count: int
    server_time: datetime
    stale_feed_threshold_seconds: int
    stores: list[StoreFeedStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Phase 2 — Analytics response models
# --------------------------------------------------------------------------- #
class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    avg_dwell_seconds: float


class MetricsResponse(BaseModel):
    store_id: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    unique_visitors: int = 0
    converted_visitors: int = 0
    conversion_rate: float = 0.0  # 0..1
    avg_dwell_per_zone: list[ZoneDwell] = Field(default_factory=list)
    current_queue_depth: int = 0
    abandonment_rate: float = 0.0  # 0..1
    transactions: int = 0
    data_confidence: bool = True


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float = 0.0  # % lost from the previous stage


class FunnelResponse(BaseModel):
    store_id: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    total_sessions: int = 0
    stages: list[FunnelStage] = Field(default_factory=list)


class HeatmapCell(BaseModel):
    zone_id: str
    visit_frequency: int  # number of distinct sessions that visited the zone
    avg_dwell_ms: float
    avg_dwell_seconds: float
    normalized_score: float  # 0..100


class HeatmapResponse(BaseModel):
    store_id: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    session_count: int = 0
    data_confidence: bool = True  # False if < MIN_SESSIONS_FOR_CONFIDENCE sessions
    cells: list[HeatmapCell] = Field(default_factory=list)


class Anomaly(BaseModel):
    anomaly_type: str  # BILLING_QUEUE_SPIKE | CONVERSION_DROP | DEAD_ZONE
    severity: str  # INFO | WARN | CRITICAL
    message: str
    suggested_action: str
    timestamp: datetime
    zone_id: Optional[str] = None
    value: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    evaluated_at: datetime
    anomaly_count: int = 0
    anomalies: list[Anomaly] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# SQLAlchemy ORM model
# --------------------------------------------------------------------------- #
class EventRecord(Base):
    """Persisted event. `event_id` is the idempotency / dedup key."""

    __tablename__ = "events"

    # event_id is the natural primary key -> enforces idempotency at the DB level.
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    camera_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    visitor_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String, index=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    zone_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    dwell_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Flattened metadata.*
    queue_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sku_zone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    session_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Ingest bookkeeping (useful for ordering / debugging, not part of the wire schema).
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    @classmethod
    def from_event(cls, event: Event) -> "EventRecord":
        """Build an ORM row from a validated Pydantic event."""
        return cls(
            event_id=event.event_id,
            store_id=event.store_id,
            camera_id=event.camera_id,
            visitor_id=event.visitor_id,
            event_type=event.event_type.value,
            timestamp=event.timestamp,
            zone_id=event.zone_id,
            dwell_ms=event.dwell_ms,
            is_staff=event.is_staff,
            confidence=event.confidence,
            queue_depth=event.metadata.queue_depth,
            sku_zone=event.metadata.sku_zone,
            session_seq=event.metadata.session_seq,
        )

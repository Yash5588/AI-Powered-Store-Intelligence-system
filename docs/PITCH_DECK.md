# Pitch Deck Content

Use this as a quick slide outline for the HackerEarth presentation upload.

## 1. Problem

Offline stores lack the same funnel visibility that e-commerce teams use every day. Raw CCTV exists, but it is not directly useful for conversion, queue, dwell, or merchandising decisions.

## 2. Solution

An AI-powered Store Intelligence System that converts CCTV into structured events, ingests them through production-ready APIs, and visualizes real-time store performance in a React dashboard.

## 3. Architecture

CCTV upload -> YOLO person detection -> tracking -> camera zone mapping -> JSON events -> FastAPI + SQLite -> analytics endpoints -> React dashboard.

The event schema is the contract between computer vision and analytics, so the detector can be replaced without rewriting the dashboard or APIs.

## 4. Computer Vision Pipeline

- Detects people using configurable YOLO models.
- Tracks identities across frames.
- Maps person positions into calibrated zones.
- Excludes staff-only or staff-zone activity.
- Optionally saves annotated videos for visual verification.

## 5. Intelligence APIs

- Batch event ingestion with idempotency.
- Metrics: visitors, conversion rate, queue depth, abandonment.
- Funnel: entry, zone visit, billing queue, purchase.
- Heatmap: zone dwell and visit density.
- Anomalies: queue spikes, conversion drops, dead zones.
- Health checks with feed freshness.

## 6. Dashboard

The React dashboard gives reviewers a complete live workflow:

- Overview
- Video Processing
- Live Events
- Metrics
- Funnel
- Heatmap
- Anomalies
- Runbook

## 7. Production Readiness

- Clean module boundaries between CV, event ingestion, analytics, and UI.
- SQLite-backed persistence for demo simplicity.
- Docker Compose for one-command startup.
- Privacy-safe repository rules that exclude videos, databases, model weights, and generated outputs.
- Test suite for analytics, ingestion, pipeline, dashboard contracts, and video jobs.

## 8. Trade-Offs

- SQLite is used for a portable demo; production would move to Postgres/ClickHouse depending on event volume.
- In-memory video job state is simple for local review; production would use Redis and a task queue.
- YOLO is used for practical local detection; the event contract allows future backends such as TensorRT, ONNX Runtime, or Frigate.
- Zone polygons are normalized approximations and should be calibrated per deployed camera.

## 9. Demo Flow

1. Start Docker Compose or local backend/frontend.
2. Open http://localhost:3000.
3. Seed ST1008 demo events.
4. Review metrics, funnel, heatmap, and anomaly pages.
5. Upload a CCTV clip in Video Processing.
6. Process it and preview the annotated output.
7. Inspect generated live events.

## 10. Impact

Store teams can measure offline conversion, identify bottlenecks, spot dead zones, understand dwell behavior, and use real-time alerts to act faster on operational problems.


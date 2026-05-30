# DESIGN.md — Store Intelligence System

## 1. Problem framing

Apex Retail can measure everything online but is blind in its 40 physical
stores. This system converts raw CCTV footage into the same kind of funnel
analytics an e-commerce team takes for granted, anchored on a single north-star
metric: **offline conversion rate** (purchasing visitors ÷ unique visitors per
session window). Every component is justified by whether it makes that number
more *accurate* (detection layer) or more *useful* (API layer).

## 2. Architecture overview

The system is a four-stage pipeline with a clean seam between the messy,
probabilistic CV world and the deterministic analytics world:

```
CCTV clips → Detection (YOLO + tracker) → Events (JSON) → Intelligence API (FastAPI/SQLite) → Dashboard
```

The **event** is the contract between the two halves. The detector's only job
is to emit schema-valid events; the API's only job is to ingest, store, and
aggregate them. This decoupling means the detector can be rewritten, re-run, or
replayed from a file without the API ever knowing, and the API can be tested
exhaustively with synthetic events long before the CV pipeline exists — which
is exactly how this project is sequenced (the API was built and tested first).

### The Intelligence API (`app/`)

- **`app/models.py`** — one Pydantic `Event` model that *is* the wire schema,
  plus a flattened `EventRecord` ORM row, plus the analytics response models. The
  Pydantic `Event` is reused by the detection pipeline so there is a single
  source of truth for the schema.
- **`app/ingestion.py`** — per-event validation, intra- and cross-batch
  deduplication, and persistence. Partial success is a first-class concept.
- **`app/sessions.py`** — the shared analytics primitive: reconstructs visitor
  *sessions* from raw events (re-entry collapses into one session via the reused
  `visitor_id`; staff excluded). Metrics, funnel, and heatmap all build on it.
- **`app/metrics.py` / `funnel.py` / `heatmap.py` / `anomalies.py`** — the four
  analytics endpoints. `metrics`/`funnel` correlate POS transactions for
  conversion; `anomalies` runs queue-spike / conversion-drop / dead-zone detectors.
- **`app/loaders.py`** — official-or-fallback loaders for POS + layout.
- **`app/health.py`** — DB ping + per-store feed freshness with `STALE_FEED`.
- **`app/main.py`** — FastAPI app, observability middleware (trace id + one
  structured log line per request), and exception handlers that guarantee no raw
  stack trace ever reaches a client.

### The detection pipeline (`pipeline/`)

- **`pipeline/detect.py`** — CLI + video driver. OpenCV reads frames, a
  pretrained **YOLOv8** model detects the `person` class only, and a tracker
  assigns a stable `visitor_id`. OpenCV/YOLO are **imported lazily** so the
  module (and the bulk of its logic) is unit-testable without the heavy wheels.
- **`pipeline/tracker.py`** — `CentroidTracker`: a dependency-free greedy
  nearest-centroid tracker with approximate trajectory-distance re-entry
  detection. Used as the fallback when ByteTrack ids aren't available.
- **`pipeline/zones.py`** — loads normalised polygon zones from the layout and
  classifies a point to a zone (ray-casting point-in-polygon), with default
  frame regions when no geometry/layout exists.
- **`pipeline/emit.py`** — schema-compliant event builders + JSONL/API sinks.
- **`SessionStateMachine`** (in `detect.py`) — the pure-Python core that turns
  per-frame `(visitor, zone, confidence, time)` observations into ENTRY / EXIT /
  ZONE_ENTER / ZONE_EXIT / ZONE_DWELL (every 30s) / BILLING_QUEUE_JOIN /
  BILLING_QUEUE_ABANDON / REENTRY events. It is tested directly, no video needed.
- **`pipeline/simulate_events.py`** — generates the same schema without a clip,
  for exercising the API/dashboard. **`pipeline/run.sh`** processes a clip folder.

### The dashboard (`dashboard/terminal_dashboard.py`)

Polls the API every 2s and renders unique visitors, conversion rate, queue
depth, and active anomalies (`rich` when available, plain text otherwise) —
proof the pipeline → API → UI loop is genuinely connected.

## 3. Key design decisions

**Idempotency at the storage layer.** `event_id` is the SQLite primary key, so
re-ingesting a batch can never create duplicate rows even under a crash-retry.
The ingest logic also dedups *within* a batch and tolerates a concurrent-insert
race by falling back to row-by-row commits. This directly satisfies the "safe
to call twice" requirement and protects the conversion metric from inflation.

**Partial success over all-or-nothing.** A real detector emits imperfect data.
If FastAPI validated the whole batch as one model, a single bad event would
422 the entire payload and the on-call engineer would lose 499 good events. We
validate each event independently and return an indexed, machine-readable
verdict (`accepted` / `duplicates` / `rejected_events[]`).

**Graceful degradation.** A `SQLAlchemyError` becomes a structured `503`, any
unexpected error a structured `500`, and `/health` *itself* never raises — it
reports `degraded` when the DB is down. On-call tooling can rely on the shape.

**Timestamps normalised to UTC at the boundary.** Naive timestamps are coerced
to UTC on the way in, so lag math in `/health` and the time-window POS joins for
conversion are never ambiguous.

**Sessions, not raw events, are the analytics unit.** Funnel, metrics, and
heatmap all derive from `app/sessions.py`, which groups events by
`(store_id, visitor_id)`. Because a re-entry reuses the same `visitor_id`, a
customer who leaves and returns collapses into one session and is counted once —
the "re-entries must not double-count" requirement is satisfied structurally
rather than with special cases.

**Analytics window is anchored on the latest event per store.** Replayed
historical clips (timestamped in the past) still populate a window, and the
window/conversion/confidence thresholds are all env-configurable.

**Structured JSON logs.** Every request logs `trace_id`, `store_id` (when
known), `endpoint`, `latency_ms`, `event_count`, and `status_code` as one JSON
line — directly queryable in an aggregator. The `trace_id` is returned in the
`X-Trace-Id` response header so a client error can be traced end-to-end.

## 4. AI-Assisted Decisions

This section records where an LLM materially shaped the design and where I
overrode it.

1. **Batch validation strategy — I overrode the AI.** The first AI suggestion
   was to type the endpoint as `def ingest(req: IngestRequest)` and let FastAPI
   validate the batch automatically. That is cleaner code, but it makes the
   *whole* batch fail on one bad event — the opposite of the "partial success"
   requirement. I rejected it and instead read the raw body and validate each
   event in `ingestion.py`, trading a little boilerplate for the correct
   production semantics. (I kept `IngestRequest` in the models as documentation
   of the intended shape and for the size constraint.)

2. **Storing `metadata` as JSON vs. flat columns — I partially agreed.** The AI
   proposed a single JSON column for the nested `metadata`. I agreed it's
   simplest but overrode it for the three known sub-fields (`queue_depth`,
   `sku_zone`, `session_seq`) because Phase 2's heatmap/queue analytics will
   `GROUP BY`/filter on them, and indexed scalar columns beat JSON extraction in
   SQLite. `metadata` is also a reserved attribute on SQLAlchemy's declarative
   base, which independently forced flattening. Unknown future fields are still
   tolerated via `extra="allow"` on the Pydantic metadata model.

3. **Health endpoint semantics — I agreed and extended.** The AI suggested
   `/health` should never depend on the DB being up. I agreed and extended it:
   it returns `degraded` + a `DATABASE_UNAVAILABLE` warning and HTTP 503 rather
   than raising, so it stays the single most reliable signal for an on-call
   engineer — which the scoring rubric explicitly calls out.

4. **Testing the vision pipeline without video — I adopted the AI's idea.** The
   AI suggested extracting the event-generation logic into a video-free
   `SessionStateMachine` so it could be unit-tested without OpenCV/YOLO or a
   clip. I adopted this fully: `detect.py` imports the CV libraries lazily, and
   the entire ENTRY/ZONE/BILLING/REENTRY event logic is tested through the state
   machine against the real Pydantic schema. This is why the suite runs in
   seconds on any machine with no GPU and no model download.

5. **Staff detection — I overrode the AI's over-reach.** The AI proposed an
   appearance/uniform-colour classifier for `is_staff`. With no labelled staff
   data, that would be fabricated accuracy I couldn't defend in the follow-up
   interview. I overrode it: `is_staff` defaults to `false` with an explicit,
   documented `--staff-zone` config hook, and the limitation is stated plainly in
   CHOICES.md. The API already excludes `is_staff=true` correctly, so the seam is
   ready the moment real staff labels exist.

## 5. Known limitations / next steps

- **SQLite is single-writer**; at 40 live stores this is the first bottleneck —
  mitigated by the `DATABASE_URL` PostgreSQL swap path (see CHOICES.md).
- **Staff classification is a placeholder** (no labelled data) — see decision 5.
- **Re-entry / cross-camera Re-ID is trajectory-distance only** (no appearance
  embedding); it approximates rather than guarantees identity across long gaps.
- **Conversion is time-window correlation**, not true identity matching, because
  POS has no `customer_id` — the spec's intended approach, but it can mis-credit
  a transaction when two customers are in billing within the same 5-min window.
- No auth/rate-limiting yet (noted for production, out of scope here).

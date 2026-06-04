# AI-Powered Store Intelligence System

Turn raw retail CCTV into the same funnel analytics an e-commerce team takes for
granted. CCTV clips → person detection + tracking → schema-valid **events** →
an **Intelligence API** (FastAPI/SQLite) → dashboards, anchored on one
north-star metric: **offline conversion rate**.

```
CCTV clips → Detection (YOLOv8 + tracker) → Events (JSON) → Intelligence API (FastAPI/SQLite) → React Dashboard
```

The **event** is the contract between the messy CV world and the deterministic
analytics world, so each half is built, tested, and replaced independently. See
[`docs/DESIGN.md`](docs/DESIGN.md) and [`docs/CHOICES.md`](docs/CHOICES.md) for
the full architecture and decision log.

## Full Stack Demo

```bash
# ---- Docker (recommended — one command, everything starts) ----
docker compose up --build
#   React frontend:   http://localhost:3000
#   API (Swagger):    http://localhost:8000/docs
#   Streamlit:        http://localhost:8501  (optional legacy dashboard)

# ---- Full demo walkthrough ----
# 1. Open http://localhost:3000 in a browser.
# 2. Navigate to "Video Processing" in the sidebar.
# 3. Upload a CCTV clip (.mp4, .avi, .mov, .mkv, .webm).
# 4. Select the camera (CAM_ENTRY_01, CAM_FLOOR_A_01, CAM_FLOOR_B_01,
#    CAM_BILLING_01, or CAM_STOCKROOM_01 for staff-only footage).
# 5. Set Store ID (default: ST1008), max frames, detector model,
#    confidence threshold, and annotated video toggle.
#    Stockroom camera auto-enables "all staff" — no customer analytics.
# 6. Click "Start processing". Watch live progress (frames, events, status).
# 7. When complete, preview the annotated video inline.
# 8. Navigate to Metrics / Funnel / Heatmap / Anomalies — all update live.
# 9. Navigate to "Live Events" to inspect generated events with filters.
# 10. Check "Runbook" for CLI commands and data-privacy notes.

# ---- Seed demo data (no video required) ----
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000

# ---- Run detection locally (requires CV wheels) ----
pip install -r requirements-pipeline.txt
python -m pipeline.detect --video "data/clips/CAM 1.mp4" \
  --store-id ST1008 --camera-id CAM_FLOOR_A_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8n.pt --conf 0.25 \
  --save-annotated-video data/outputs/annotated.mp4 --max-frames 300 \
  --post http://localhost:8000

# ---- Tests ----
pytest --cov=app --cov=pipeline --cov-report=term-missing
cd frontend && npm install && npm run build
```

### Data privacy

> **No CCTV videos, model weights, generated outputs, or database files are
> committed to this repository.** The `.gitignore` blocks `*.mp4`, `*.pt`,
> `yolo*.pt`, `*.db`, `data/clips/`, `data/outputs/`, `data/uploads/`, and
> `generated_events.jsonl`. Uploaded videos remain local in `data/uploads/`.
> The committed POS file is **de-identified** (no customer names/phones).

## Quickstart (local dev)

```bash
# 1. Install (API + dashboards only; CV wheels are separate — see below)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Start the API
uvicorn app.main:app --reload --port 8000
#    Swagger UI:  http://localhost:8000/docs
#    Health:      http://localhost:8000/health

# 3. Start the React frontend (new shell)
cd frontend && npm install && npm run dev
#    React:  http://localhost:3000

# 4. (optional) Seed realistic data and open the web dashboard
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
streamlit run dashboard/app.py        # http://localhost:8501
```

Run with Docker instead:

```bash
docker compose up --build
# API on :8000, React frontend on :3000, Streamlit dashboard on :8501
```

## Real Brigade Road data (and how official files drop in)

The system always runs on synthetic fallbacks, but it is also grounded in the
**real Brigade Road (Bangalore) store**:

| File | Role | Notes |
|------|------|-------|
| `data/store_layout.json` | **official** layout | Store `ST1008` zones derived from the real floor plan (ENTRY, SKINCARE, MAKEUP, FRAGRANCE, HAIRCARE, ACCESSORIES, PMU, BILLING). Demo stores carried over. |
| `data/pos_transactions.csv` | **official** POS | Real Brigade export, **de-identified** (all customer + employee personal fields stripped). Rich line-item schema, auto-detected & aggregated by `order_id`. |
| `data/fallback_store_layout.json` | fallback layout | Used only when the official file is absent. |
| `data/synthetic_pos_transactions.csv` | fallback POS | Simple one-row-per-transaction schema. |

`app/loaders.py` resolves **official → fallback** automatically, and
**auto-detects the POS schema** (simple vs. rich line-item), so dropping the real
files into `data/` required **zero changes to any caller**. Later official files
(`sample_events.jsonl`, `assertions.py`) drop in the same way.

> **Why no model retraining?** The provided files are business metadata (sales
> rows + a floor-plan image), not labelled camera frames, so they can't train a
> person detector. We keep a pretrained YOLO person detector (default
> `yolov8n.pt` for speed; selectable from the UI/API/CLI) and use this data where
> it genuinely helps: real camera-calibrated zones, real product categories, and
> a real *computed* conversion rate. See `docs/CHOICES.md` Decision 5.

### Detection configuration

People detection is configurable without changing the analytics contract:

| Model | UI label | Trade-off |
|-------|----------|-----------|
| `yolov8n.pt` | Fast | Default, fastest local demo path |
| `yolov8s.pt` | Balanced | Stronger person detection, slower than nano |
| `yolov8m.pt` | Accurate | Better detections on harder footage, slower |
| `yolo11s.pt` | Balanced newer | Newer YOLO family, balanced speed/accuracy |
| `yolo11m.pt` | Accurate newer | Newer YOLO family, stronger but slower |

The React Video Processing page sends `model` and `conf` to
`POST /videos/{id}/process`; the backend forwards them to the existing
`pipeline.detect.run_detection(model_name=..., conf_threshold=...)`. The default
confidence threshold is `0.25`, with UI/API validation from `0.05` to `0.9`.

Store sections are **not detected by AI**. The detector finds people; their
positions are mapped into camera-calibrated zones from `data/store_layout.json`.
Future production deployments could swap the detector backend behind the same
event contract, for example Frigate, ONNX Runtime, or TensorRT. Those are
documented options only; they are not dependencies of this project.

## API endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /events/ingest` | Batch-ingest events (idempotent, partial success) |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, queue depth, abandonment |
| `GET /stores/{id}/funnel` | Nested funnel: Entry ≥ Zone Visit ≥ Billing Queue ≥ Purchase |
| `GET /stores/{id}/heatmap` | Per-zone dwell / visit density |
| `GET /stores/{id}/anomalies` | Queue-spike / conversion-drop / dead-zone detectors |
| `GET /health` | DB ping + per-store feed freshness (`stale_feed`, lag) |
| `POST /videos/upload` | Upload a CCTV clip for processing |
| `POST /videos/{id}/process` | Start background detection on an uploaded clip |
| `GET /videos/{id}/status` | Job progress: frames, events, status, errors |
| `GET /videos/{id}/events` | Recent events generated by a job |
| `GET /videos/{id}/annotated-video` | Serve the annotated output video |
| `GET /videos` | List recent video processing jobs |

## Demo walkthrough

```bash
# --- Real-POS-aligned demo (conversion is computed from the real POS file) ---
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
curl -s localhost:8000/stores/ST1008/metrics | python -m json.tool
curl -s localhost:8000/stores/ST1008/funnel  | python -m json.tool
#   -> ~36 visitors, 24 converted (one per real transaction), ~67% conversion,
#      nested funnel, non-zero abandonment.

# --- Detection on the 5 real CCTV angles (requires the CV wheels) ---
# Each camera carries its OWN calibrated zones in store_layout.json; pass the
# matching --camera-id so the right per-camera polygons are used.
pip install -r requirements-pipeline.txt

# Entry camera (threshold / door):
python -m pipeline.detect --video "data/clips/entry.mp4" \
  --store-id ST1008 --camera-id CAM_ENTRY_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8n.pt --conf 0.25 --post http://localhost:8000

# Floor camera A (skincare back wall + centre makeup units):
python -m pipeline.detect --video "data/clips/floor_a.mp4" \
  --store-id ST1008 --camera-id CAM_FLOOR_A_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8s.pt --conf 0.25 --post http://localhost:8000

# Floor camera B (makeup / haircare / accessories front wall):
python -m pipeline.detect --video "data/clips/floor_b.mp4" \
  --store-id ST1008 --camera-id CAM_FLOOR_B_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8m.pt --conf 0.25 --post http://localhost:8000

# Billing camera (cashier behind the counter is auto-excluded via the staff zone):
python -m pipeline.detect --video "data/clips/billing.mp4" \
  --store-id ST1008 --camera-id CAM_BILLING_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8s.pt --conf 0.25 --post http://localhost:8000

# Stockroom / back office — NON-customer area: --all-staff so it never inflates counts
# (or simply don't run it):
python -m pipeline.detect --video "data/clips/stockroom.mp4" \
  --store-id ST1008 --camera-id CAM_STOCKROOM_01 --all-staff \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8n.pt --conf 0.25 --post http://localhost:8000

# Visually verify a camera's zones line up (boxes, IDs, zone polygons, event labels):
python -m pipeline.detect --video "data/clips/floor_a.mp4" \
  --store-id ST1008 --camera-id CAM_FLOOR_A_01 \
  --layout data/store_layout.json --output data/generated_events.jsonl \
  --model yolov8s.pt --conf 0.25 \
  --save-annotated-video data/outputs/annotated_floor_a.mp4 --max-frames 300

# --- Ingest an existing JSONL file in batches ---
python -m pipeline.ingest_jsonl \
  --file data/generated_events.jsonl --url http://localhost:8000/events/ingest --batch-size 500
```

## Tests

```bash
pytest --cov=app --cov=pipeline --cov-report=term-missing   # backend (currently ~91%)
cd frontend && npm run build                                  # frontend build
```

The suite is video-free and GPU-free: the event-generation core
(`SessionStateMachine`), trackers, zones, loaders, and the POS adapter are all
unit-tested with synthetic inputs and mocks. CV libraries are imported lazily.

## Layout

```
app/        FastAPI app: models, ingestion, sessions, analytics, health, video_jobs
pipeline/   detect.py (YOLO+tracker), tracker, zones, emit, simulate_events, ingest_jsonl
frontend/   React (Vite) UI: Overview, VideoProcessing, LiveEvents, Metrics, Funnel,
            Heatmap, Anomalies, Runbook — all fetched from the API, zero hardcoded data
dashboard/  app.py (Streamlit), terminal_dashboard.py
data/       layout + POS files (official preferred, synthetic fallback)
docs/       DESIGN.md, CHOICES.md
tests/      pytest suite
```

## Notes

- Heavy CV wheels (OpenCV, Ultralytics/torch) live in `requirements-pipeline.txt`
  so the API image stays slim — install them only to run detection.
- Videos, model weights, and generated outputs are git-ignored.
- Stronger YOLO models can improve person detection on crowded or occluded
  footage, but they take more CPU/GPU time and may reduce throughput.
- Frigate, ONNX Runtime, and TensorRT are production detector backend options
  behind the same event contract; they are not integrated in this repo.
- The committed POS is **de-identified** (no customer names/phones, no employee
  data). The raw export must never be committed.
- Floor-plan zone polygons are normalised approximations; calibrate against a
  real CCTV frame per camera for production accuracy.
- Video job state is **in-memory** (process-local dict); it is lost on API
  restart. For production, this would move to SQLite/Redis + a task queue. The
  seam is the `JobStore` class in `app/video_jobs.py`.

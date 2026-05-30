# AI-Powered Store Intelligence System

Turn raw retail CCTV into the same funnel analytics an e-commerce team takes for
granted. CCTV clips → person detection + tracking → schema-valid **events** →
an **Intelligence API** (FastAPI/SQLite) → dashboards, anchored on one
north-star metric: **offline conversion rate**.

```
CCTV clips → Detection (YOLOv8 + tracker) → Events (JSON) → Intelligence API (FastAPI/SQLite) → Dashboard
```

The **event** is the contract between the messy CV world and the deterministic
analytics world, so each half is built, tested, and replaced independently. See
[`docs/DESIGN.md`](docs/DESIGN.md) and [`docs/CHOICES.md`](docs/CHOICES.md) for
the full architecture and decision log.

## Quickstart

```bash
# 1. Install (API + dashboards only; CV wheels are separate — see below)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Start the API
uvicorn app.main:app --reload --port 8000
#    Swagger UI:  http://localhost:8000/docs
#    Health:      http://localhost:8000/health

# 3. (new shell) Seed realistic data and open the web dashboard
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
streamlit run dashboard/app.py        # http://localhost:8501
```

Run with Docker instead:

```bash
docker compose up --build      # API on :8000, dashboard on :8501
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
> person detector. We keep pretrained **YOLOv8** and use this data where it
> genuinely helps: real zones, real product categories, and a real *computed*
> conversion rate. See `docs/CHOICES.md` Decision 5.

## API endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /events/ingest` | Batch-ingest events (idempotent, partial success) |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, queue depth, abandonment |
| `GET /stores/{id}/funnel` | Nested funnel: Entry ≥ Zone Visit ≥ Billing Queue ≥ Purchase |
| `GET /stores/{id}/heatmap` | Per-zone dwell / visit density |
| `GET /stores/{id}/anomalies` | Queue-spike / conversion-drop / dead-zone detectors |
| `GET /health` | DB ping + per-store feed freshness (`stale_feed`, lag) |

## Demo walkthrough

```bash
# --- Real-POS-aligned demo (conversion is computed from the real POS file) ---
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
curl -s localhost:8000/stores/ST1008/metrics | python -m json.tool
curl -s localhost:8000/stores/ST1008/funnel  | python -m json.tool
#   -> ~36 visitors, 24 converted (one per real transaction), ~67% conversion,
#      nested funnel, non-zero abandonment.

# --- Detection on a real CCTV clip (requires the CV wheels) ---
pip install -r requirements-pipeline.txt
python -m pipeline.detect \
  --video "data/clips/CAM 1.mp4" --store ST1008 --camera CAM_FLOOR_01 \
  --layout data/store_layout.json --out data/generated_events.jsonl \
  --post http://localhost:8000

# Visually verify detection (boxes, IDs, zones, event labels):
python -m pipeline.detect \
  --video "data/clips/CAM 1.mp4" --store ST1008 --camera CAM_FLOOR_01 \
  --layout data/store_layout.json --out data/generated_events.jsonl \
  --save-annotated-video data/outputs/annotated_CAM1.mp4 --max-frames 300

# --- Ingest an existing JSONL file in batches ---
python -m pipeline.ingest_jsonl \
  --file data/generated_events.jsonl --url http://localhost:8000/events/ingest --batch-size 500
```

## Tests

```bash
pytest                       # full suite + coverage (currently ~91%)
```

The suite is video-free and GPU-free: the event-generation core
(`SessionStateMachine`), trackers, zones, loaders, and the POS adapter are all
unit-tested with synthetic inputs and mocks. CV libraries are imported lazily.

## Layout

```
app/        FastAPI app: models, ingestion, sessions, analytics, loaders, health
pipeline/   detect.py (YOLO+tracker), tracker, zones, emit, simulate_events, ingest_jsonl
dashboard/  app.py (Streamlit), terminal_dashboard.py
data/       layout + POS files (official preferred, synthetic fallback)
docs/       DESIGN.md, CHOICES.md
tests/      pytest suite
```

## Notes

- Heavy CV wheels (OpenCV, Ultralytics/torch) live in `requirements-pipeline.txt`
  so the API image stays slim — install them only to run detection.
- Videos, model weights, and generated outputs are git-ignored.
- The committed POS is **de-identified** (no customer names/phones, no employee
  data). The raw export must never be committed.
- Floor-plan zone polygons are normalised approximations; calibrate against a
  real CCTV frame per camera for production accuracy.

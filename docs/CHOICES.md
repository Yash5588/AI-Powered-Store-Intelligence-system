# CHOICES.md — Key decisions, fully reasoned

Each decision lists the options I considered, what AI suggested, what I chose,
and *why*.

---

## Decision 1 — Detection model: pretrained YOLOv8, not custom training

**Options considered**

| Option | Pros | Cons |
|--------|------|------|
| **Pretrained YOLOv8 (Ultralytics) + ByteTrack** ✅ | Mature, fast on 1080p/15fps, robust COCO `person`, ByteTrack handles occlusion, **zero training** | Generic person class only — no staff/customer distinction out of the box |
| Custom-trained detector | Could learn store-specific cues / staff | No labelled data, no time budget; training in a 48h window is unjustifiable |
| RT-DETR | Strong on crowded/occluded scenes | Heavier, slower, more tuning |
| MediaPipe | Lightweight | Weak for multi-person tracking across a retail floor |
| VLM (GPT-4V / Gemini) for every frame | Zero training, semantic understanding | Far too slow/expensive for 15fps × 3 cams × 5 stores |

**What AI suggested.** The LLM recommended pretrained YOLOv8 + ByteTrack as the
default and proposed (a) a VLM *per frame* for staff classification and (b) an
appearance-embedding Re-ID model.

**What I chose and why.** **Pretrained YOLOv8 + ByteTrack, no custom training.**
Rationale: the challenge explicitly evaluates *handling uncertainty and edge
cases*, not a perfect detection rate, and a pretrained `person` detector is the
best accuracy-per-engineering-hour for anonymised retail CCTV. Training a custom
model in the window — with no labels — would be fabricated rigour I couldn't
defend. I keep YOLO confidence on **every** emitted event and do **not** suppress
low-confidence detections (I run YOLO at a low `conf=0.10` and flag rather than
drop), matching the "confidence calibration" criterion.

**Detector configurability.** The default remains `yolov8n.pt` because it is the
fastest practical demo model, but the React Video Processing page and
`POST /videos/{job_id}/process` now accept `model` and `conf`. Supported UI
choices are `yolov8n.pt` (Fast), `yolov8s.pt` (Balanced), `yolov8m.pt`
(Accurate), `yolo11s.pt` (Balanced newer), and `yolo11m.pt` (Accurate newer).
The default UI/API confidence threshold is `0.25` and valid values are `0.05` to
`0.9`. The CLI keeps the same surface:
`python -m pipeline.detect --model yolov8s.pt --conf 0.25 ...`.

Stronger YOLO models usually improve person detection in crowded or occluded
footage, but they are slower and may reduce throughput on CPU-only machines.
Frigate, ONNX Runtime, and TensorRT are good future production detector backend
options behind the same event contract; they are documented here only and are
not dependencies of the current repo.

I **overrode** both AI add-ons:
- **No per-frame VLM for staff.** Cost/latency aside, with no labelled staff data
  any classifier is unverifiable. `is_staff` defaults to `false` with a
  documented `--staff-zone` config hook (see Decision 4 / limitation below).
- **No appearance-embedding Re-ID.** I implemented a **trajectory-distance**
  centroid tracker with approximate re-entry (reuse a recently-retired
  `visitor_id` if someone reappears nearby soon after), and use ByteTrack ids
  when available. **What would change my mind:** if held-out re-entry accuracy is
  poor, OSNet/torchreid embeddings are the next step — but only with the ground
  truth to measure against.

**Documented limitation (staff).** Because there is no labelled staff data, staff
exclusion is only as good as the optional zone config. The *system* is correct —
the API already excludes `is_staff=true` everywhere — but the *detector* cannot
reliably set that flag yet. This is intentional, not an oversight.

---

## Decision 2 — Event schema design

**Options considered**

1. **Flat schema** — every field top-level, no nesting.
2. **Nested schema with a `metadata` envelope** (the spec's shape).
3. **Event-type-specific schemas** (a different model per event type).

**What AI suggested.** The LLM leaned toward option 3 (a discriminated union per
event type) for maximum type-safety — e.g. `BILLING_QUEUE_JOIN` *requires*
`queue_depth`.

**What I chose and why.** **Option 2 — one unified `Event` model with a nested
`metadata` object**, matching the problem statement exactly. Reasons:

- **Schema compliance is graded**, and a single model that mirrors the spec is
  the lowest-risk path to "all emitted events validate against the schema."
- **Ingestion stays uniform** — one validation path, one table, one dedup key.
  A discriminated union would fan out into per-type handling that buys little at
  ingest time.
- I kept the model **strict** (`extra="forbid"` at the top level) to catch
  detector bugs early, but **lenient inside `metadata`** (`extra="allow"`) so a
  future detector field doesn't break ingestion.
- `event_id` is the **idempotency key** (also the DB primary key), and
  timestamps are normalised to UTC at validation time so all downstream time math
  is unambiguous.

I accepted the *spirit* of the AI's type-safety concern (e.g. `queue_depth`
should be set for queue events) but will enforce that as an **analytics-time
data-quality check**, not a hard ingest rejection — because rejecting a
real-but-incomplete event would *lose* signal, which hurts the north-star metric
more than a missing field does.

---

## Decision 3 — API architecture: storage engine & ingest semantics

**The choice:** SQLite + SQLAlchemy ORM, with **idempotent, partial-success**
ingest.

**Options considered**

| Option | Why considered | Why not (now) |
|--------|----------------|---------------|
| **SQLite + SQLAlchemy** ✅ | Zero-dependency `docker compose up`, fast enough for batch ingest, ORM keeps a clean swap path | Single-writer; not for 40 live high-throughput stores |
| PostgreSQL | Concurrency, production-grade | Adds a container + setup; overkill for the challenge's data volume |
| In-memory / NoSQL | Simple / flexible | Loses durability or relational aggregation the analytics need |

**What AI suggested.** The LLM suggested going straight to PostgreSQL "for
production realism."

**What I chose and why.** I chose **SQLite** for the deliverable because the
acceptance gate is *"`docker compose up` with no manual steps,"* and a
file-backed SQLite on a named volume meets that with the least operational
surface. Crucially, **the code is written against the SQLAlchemy ORM and reads
`DATABASE_URL` from the environment**, so moving to PostgreSQL is a
connection-string change, not a rewrite. I documented this explicitly because
the follow-up question *"at 40 live stores, what breaks first?"* has a concrete
answer here: **SQLite's single-writer lock** — at which point the
already-present `DATABASE_URL` seam lets me point at Postgres and add a
connection pool without touching the ingest or analytics code.

On **ingest semantics**, I made the deliberate choice that ingest returns `200`
with a per-event verdict (accepted/duplicate/rejected) rather than failing the
batch, and that re-posting is safe (dedup by `event_id`). This matches how a
real event pipeline behaves under retries and protects the conversion metric
from both double-counting (idempotency) and silent data loss (partial success).

---

## Decision 4 — Fallback files because official metadata was missing

**Context.** At development time, the official resource ZIP contained only the
CCTV videos. The supporting files the spec describes —
`store_layout.json`, `pos_transactions.csv`, `sample_events.jsonl`, and
`assertions.py` — were **not yet available**. I had to keep the project fully
runnable now without blocking on files I couldn't see.

**Options considered**

1. **Hard-code/stub the missing data inline** — fastest, but couples logic to
   throwaway data and breaks the moment real files arrive.
2. **Wait / require the official files** — non-starter; the acceptance gate
   requires `docker compose up` to work with nothing but `git clone`.
3. **Synthetic fallbacks behind an official-or-fallback loader** ✅ — ship
   realistic synthetic files matching the documented schemas, and a loader that
   prefers the official file when present.

**What AI suggested.** The LLM suggested hard-coding a small layout dict in the
pipeline to "just get detection working."

**What I chose and why.** **Option 3.** `app/loaders.py` and `pipeline/zones.py`
resolve in the order *official → synthetic fallback*, so:

- The system runs **today** on `data/fallback_store_layout.json` and
  `data/synthetic_pos_transactions.csv`.
- When the official `data/store_layout.json` / `data/pos_transactions.csv`
  arrive, they are picked up **automatically with zero code changes** — drop the
  file in `data/` and it wins.
- The synthetic files mirror the exact documented schemas (POS:
  `store_id, transaction_id, timestamp, basket_value_inr`; layout: zones with
  normalised polygon `region`s), so they are 1:1 replaceable.

I overrode the hard-coding suggestion because it would have to be torn out later
and re-tested; the loader seam costs a few lines now and makes the official-file
swap a non-event. The same principle covers `sample_events.jsonl` (drop in as the
validation set) and `assertions.py` (place at repo root; `pytest` runs it
alongside the suite).

**Update — the real Brigade data arrived and proved the seam.** The real store
files (`Brigade Road - Store layout.xlsx` floor plan + `Brigade_Bangalore_10_April_26.csv`
POS export) were later provided and dropped in as the official `data/store_layout.json`
(store `ST1008`) and `data/pos_transactions.csv` — **no caller code changed**. One
extension was needed and made *inside the loader only*: the real POS is a *rich
line-item export* (one row per SKU: `order_id, order_date, order_time, dep_name,
total_amount, …`) rather than the simple one-row-per-transaction schema. So
`load_pos_transactions` now **auto-detects** the schema and aggregates line-items
by `order_id` (basket = Σ `total_amount`; `DD-MM-YYYY HH:MM:SS` → UTC). The simple
synthetic schema still parses unchanged. This is the loader seam paying off exactly
as intended — the messy real format is absorbed in one place behind the same
`PosTransaction` contract the analytics already consume.

## Decision 5 — No model retraining from the provided real data

**Context.** When the real Brigade files arrived, the obvious question was
"can we now train/fine-tune for better accuracy?"

**What I chose and why.** **No — keep the pretrained YOLOv8 detector.** Neither
file is *labelled image data*: the CSV is sales rows and the XLSX is a top-down
floor-plan picture. Training or fine-tuning a person detector needs annotated
camera frames (bounding boxes + classes) from this store, which we don't have.
Using sales/floor-plan metadata as "training data" would be category-error
rigour. Instead this data is used where it genuinely raises accuracy — **real
zone geometry, real product categories, and a real, computed conversion rate**
(via `--align-pos`). The event seam keeps a store-specific fine-tuned detector a
drop-in upgrade if labelled frames are ever captured. (See DESIGN.md §5.)

## Decision 6 — Per-camera zones for 5 different CCTV angles

**Context.** The store's test footage comes from **5 cameras at very different
angles**: entry (top-down on the door), two floor cams (skincare wall; makeup
wall), the billing counter, and a stockroom/back office. A single normalised
polygon set (my first cut, derived from the top-down floor plan) cannot line up
with five perspectives — a shopper at the skincare wall has their feet mid-frame
in the floor cam, nowhere near a top-band "SKINCARE" polygon.

**Options considered**

1. **One store-level polygon set for all cameras** — simplest, but wrong: zones
   only align for one hypothetical viewpoint; every other camera mis-classifies.
2. **Full homography per camera** (map each frame to a common floor plane) —
   most accurate, but needs surveyed ground-control points I don't have, and is
   heavy for the time budget.
3. **Per-camera polygons calibrated in each camera's own frame** ✅ — the layout
   lets every camera carry its own `zones`; `load_zone_map(store, layout,
   camera_id)` prefers them, falling back to store zones then defaults.

**What I chose and why.** **Option 3.** It's the right accuracy-per-effort point:
each camera's zones are drawn directly on *its* frame (calibrated from the
provided reference frames), classification runs on the **feet point** for
perspective stability, and the change is backward-compatible (older single-view
layouts still work). I also handled the two cases that would otherwise corrupt
the headline metric:
- the **stockroom** camera is `customer_facing:false` and run with `--all-staff`,
  so back-office activity never inflates `unique_visitors`;
- the **cashier** is excluded via a `type:"staff"` zone behind the counter
  (`ZoneMap.staff_zone_ids`), while customers on the queue side still count.

**What I explicitly did NOT do.** No cross-camera appearance Re-ID — so I
namespace `visitor_id` by `camera_id` to prevent false merges (two cameras
emitting the same id). The honest cost is that one shopper seen by two cameras
counts twice; the fix (Re-ID) is documented as the next step. I'd add it the
moment cross-camera identity materially changed the conversion number. The
calibrated polygons are approximations from reference frames and should be
fine-tuned against the real video resolution.

Store sections are **not detected by AI**. The detector emits person positions;
zone assignment is a deterministic lookup against the camera-calibrated polygons
in `data/store_layout.json`.

## Decision 7 — React as a thin presentation layer, not an analytics engine

**Context.** The challenge requires a dashboard. We already ship a Streamlit
dashboard and a terminal dashboard, but neither supports a rich interactive
workflow like video upload → live processing → event inspection in one UI.

**Options considered**

1. **Streamlit-only** — already built, quick, but limited interactivity (no
   real-time polling, no file upload progress, no SPA navigation).
2. **React (Vite)** — rich interactivity, proper SPA, but more code to write.
3. **Next.js / full framework** — more than needed for a read-only dashboard.

**What I chose and why.** **React (Vite) as a thin frontend layer.** Key design
rules:

- **Zero analytics computed in the browser.** Every metric, funnel stage, heatmap
  cell, and anomaly is fetched from FastAPI. The React code is pure presentation.
- **No hardcoded values.** If the API returns no data, pages show an empty state.
  If the API is unreachable, pages show an error state. No fallback numbers.
- **Video job lifecycle stays server-side.** The React Video Processing page
  uploads a file, calls `/videos/{job_id}/process`, and polls
  `/videos/{job_id}/status` every 1s. All detection logic runs in the FastAPI
  backend thread — the browser just renders progress.
- **Stockroom camera rule enforced on both sides.** The React UI auto-checks and
  disables the "all staff" checkbox when `CAM_STOCKROOM_01` is selected, and the
  backend independently forces `all_staff=True` for that camera. Defence in depth.

**Documented limitation: in-memory video job state.** The `JobStore` is a
process-local dict (thread-safe, but not persistent). Jobs are lost on restart.
For production, the `JobStore` interface would swap to SQLite or Redis + a real
task queue. This is an acceptable hackathon trade-off documented in DESIGN.md.

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

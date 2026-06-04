# HackerEarth Submission Packet

## Title

AI-Powered Store Intelligence System from CCTV

## Theme

Purplle Tech Challenge 2026 - Round 2 Problem Statement

## Description

This project turns raw retail CCTV footage into production-style store analytics for an offline beauty retail store. It uses a modular computer-vision pipeline to detect and track people, maps tracks into calibrated store zones, emits a stable JSON event contract, ingests events through a FastAPI intelligence API, and exposes live analytics in a React dashboard.

The system focuses on metrics that store teams can act on: unique visitors, offline conversion rate, zone-level dwell and heatmap density, billing queue depth, funnel drop-offs, and anomaly alerts for queue spikes, conversion drops, and dead zones. The implementation also includes a video-processing workflow where reviewers can upload a clip, choose a camera/model/confidence threshold, process the video, watch job progress, inspect generated events, and preview an annotated output video.

The architecture intentionally separates the messy CV layer from deterministic analytics through an event schema. This keeps detection replaceable while the API, dashboards, tests, and business metrics remain stable. It includes real-POS-aligned demo data for store ST1008, fallback data loaders, privacy-safe repository rules, Docker/local development paths, and tests for ingestion, metrics, funnel, heatmap, anomalies, video jobs, and pipeline behavior.

## Key Features

- CCTV upload and background processing with YOLO-based person detection.
- Camera-aware zone mapping for entry, floor, billing, and stockroom views.
- Staff-only and staff-zone handling so non-customer footage does not inflate analytics.
- Idempotent batch event ingestion API.
- Real-time React dashboard for overview, metrics, funnel, heatmap, anomalies, live events, video jobs, and runbook.
- Streamlit legacy dashboard for quick alternate visualization.
- SQLite-backed analytics with health and feed freshness checks.
- Real-POS-aligned synthetic event seeding for demos without sharing private videos.
- Test suite covering API analytics, ingestion, pipeline logic, dashboard contracts, and video jobs.

## Demo Link

Use the deployed demo URL if you host it. If you are submitting locally only, record a demo video and use the video link, then put the repository URL here if HackerEarth requires a URL.

Local demo URLs after running:

- React dashboard: http://localhost:3000
- API Swagger docs: http://localhost:8000/docs
- Optional Streamlit dashboard: http://localhost:8501

## Repository URL

Paste your GitHub repository URL here after pushing the project.

Important: do not commit or upload CCTV videos, model weights, generated outputs, database files, or virtual environments.

## Instructions To Run

### Docker

```bash
docker compose up --build
```

Open:

- React dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs
- Streamlit dashboard: http://localhost:8501

Seed demo events:

```bash
python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
```

### Local Development

```powershell
cd "AI-Powered-Store-Intelligence-system"
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-pipeline.txt
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```powershell
cd "AI-Powered-Store-Intelligence-system\frontend"
npm install
npm run dev
```

Open http://localhost:3000.

Optional demo data:

```powershell
..\.venv\Scripts\python.exe -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000
```

Run tests:

```powershell
..\.venv\Scripts\python.exe -m pytest
cd frontend
npm run build
```

## Source Code Upload Notes

Before uploading a source ZIP, exclude these from the archive:

- `.git/`
- `.venv/`
- `frontend/node_modules/`
- `.pytest_cache/`
- `logs/`
- `*.db`
- `*.pt`
- `yolo*.pt`
- `data/clips/`
- `data/outputs/`
- `data/uploads/`
- `data/generated_events.jsonl`

The repository already includes `.gitignore` rules for private/heavy runtime artifacts.


import React from "react";
import { API_BASE_URL } from "../api";

function Block({ title, children }) {
  return (
    <div className="section">
      <h3>{title}</h3>
      <div className="codeblock">{children}</div>
    </div>
  );
}

export default function Runbook() {
  return (
    <div>
      <div className="card">
        <div className="sub muted">
          API base URL in use: <b className="tag">{API_BASE_URL}</b>
        </div>
      </div>

      <Block title="Full stack with Docker">
{`docker compose up --build
# API:        http://localhost:8000  (Swagger at /docs)
# Frontend:   http://localhost:3000
# Dashboard:  http://localhost:8501  (Streamlit, optional)`}
      </Block>

      <Block title="Seed demo data (real-POS-aligned conversion)">
{`python -m pipeline.simulate_events --store ST1008 --align-pos --post http://localhost:8000`}
      </Block>

      <Block title="Run detection on a CCTV clip (5 calibrated cameras)">
{`pip install -r requirements-pipeline.txt

python -m pipeline.detect --video "data/clips/CAM 1.mp4" \\
  --store-id ST1008 --camera-id CAM_FLOOR_A_01 \\
  --layout data/store_layout.json --output data/generated_events.jsonl \\
  --save-annotated-video data/outputs/annotated.mp4 --max-frames 300 \\
  --post http://localhost:8000

# Stockroom (non-customer): add --all-staff so it never inflates visitor counts
python -m pipeline.detect --video "data/clips/stockroom.mp4" \\
  --store-id ST1008 --camera-id CAM_STOCKROOM_01 --all-staff \\
  --layout data/store_layout.json --output data/generated_events.jsonl`}
      </Block>

      <Block title="Tests">
{`pytest --cov=app --cov=pipeline --cov-report=term-missing   # backend
cd frontend && npm run build                                  # frontend build`}
      </Block>

      <div className="card" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Data privacy</h3>
        <p className="muted" style={{ margin: 0 }}>
          CCTV videos, model weights, generated events, annotated outputs, and the
          local SQLite DB are <b>not committed to GitHub</b>. Official clips stay
          local. The committed POS file is de-identified (no customer names or
          phone numbers). Uploaded videos are stored only in the local
          <span className="tag"> data/uploads</span> directory.
        </p>
      </div>
    </div>
  );
}

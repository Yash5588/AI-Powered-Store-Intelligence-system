import React, { useEffect, useRef, useState } from "react";
import { api, CAMERAS } from "../api";
import { ErrorBox } from "../components/Common";
import Metrics from "./Metrics";
import Funnel from "./Funnel";
import Heatmap from "./Heatmap";
import Anomalies from "./Anomalies";

const DETECTOR_MODELS = [
  { value: "yolov8n.pt", label: "yolov8n.pt - Fast" },
  { value: "yolov8s.pt", label: "yolov8s.pt - Balanced" },
  { value: "yolov8m.pt", label: "yolov8m.pt - Accurate" },
  { value: "yolo11s.pt", label: "yolo11s.pt - Balanced newer" },
  { value: "yolo11m.pt", label: "yolo11m.pt - Accurate newer" },
];

export default function VideoProcessing({ storeId, setStoreId }) {
  const [file, setFile] = useState(null);
  const [camera, setCamera] = useState("CAM_FLOOR_A_01");
  const [maxFrames, setMaxFrames] = useState(300);
  const [model, setModel] = useState("yolov8n.pt");
  const [confidence, setConfidence] = useState(0.25);
  const [saveAnnotated, setSaveAnnotated] = useState(true);
  const [allStaff, setAllStaff] = useState(false);

  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef(null);

  function clearMissingJob(message) {
    stopPolling();
    setJobId(null);
    setJob(null);
    setEvents([]);
    setError(message);
  }

  // Stockroom is never a customer area: auto-enable all_staff and lock it.
  const stockroom = camera === "CAM_STOCKROOM_01";
  useEffect(() => {
    if (stockroom) setAllStaff(true);
  }, [stockroom]);

  function stopPolling() {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => {
    let active = true;
    async function init() {
      const { data } = await api.listVideos();
      if (!active) return;
      if (data?.jobs?.length > 0) {
        const latest = data.jobs[0];
        setJobId(latest.job_id);
        setJob(latest);
        const evs = await api.videoEvents(latest.job_id, 100);
        if (!evs.error && active) {
          setEvents(evs.data.events || []);
        }
        if (latest.status === "running" || latest.status === "queued") {
          pollRef.current = setInterval(() => pollStatus(latest.job_id), 1000);
        }
      }
    }
    init();
    return () => {
      active = false;
      stopPolling();
    };
  }, []);

  async function pollStatus(id) {
    const { data, error, status } = await api.videoStatus(id);
    if (error) {
      if (status === 404) {
        clearMissingJob(error);
        return;
      }
      setError(error);
      return;
    }
    setJob(data);
    const evs = await api.videoEvents(id, 100);
    if (!evs.error) {
      setEvents(evs.data.events || []);
    }
    if (data.status === "completed" || data.status === "failed") {
      stopPolling();
    }
  }

  async function onStart() {
    setError(null);
    if (!file) {
      setError("Please choose a video file first.");
      return;
    }
    setBusy(true);
    const up = await api.uploadVideo(file);
    if (up.error) {
      setError(`Upload failed: ${up.error}`);
      setBusy(false);
      return;
    }
    const id = up.data.job_id;
    setJobId(id);
    setEvents([]);

    const proc = await api.processVideo(id, {
      store_id: storeId,
      camera_id: camera,
      layout_path: "data/store_layout.json",
      max_frames: Number(maxFrames) || null,
      model,
      conf: Number(confidence),
      save_annotated_video: saveAnnotated,
      all_staff: allStaff || stockroom,
    });
    setBusy(false);
    if (proc.error) {
      if (proc.status === 404) {
        clearMissingJob(proc.error);
        return;
      }
      setError(`Could not start processing: ${proc.error}`);
      return;
    }
    setJob(proc.data);
    stopPolling();
    pollRef.current = setInterval(() => pollStatus(id), 1000);
  }

  const isRunning = job?.status === "running" || job?.status === "queued";

  return (
    <div>
      <div className="card">
        <div className="form-row">
          <div className="field">
            <label>CCTV video file</label>
            <input
              type="file"
              accept="video/mp4,video/avi,video/quicktime,video/x-matroska,video/webm"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </div>
          <div className="field">
            <label>Camera</label>
            <select value={camera} onChange={(e) => setCamera(e.target.value)}>
              {CAMERAS.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Store ID</label>
            <input type="text" value={storeId} onChange={(e) => setStoreId(e.target.value)} />
          </div>
          <div className="field">
            <label>Max frames</label>
            <input
              type="number"
              min="1"
              value={maxFrames}
              onChange={(e) => setMaxFrames(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Detector model</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {DETECTOR_MODELS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Confidence threshold</label>
            <input
              type="number"
              min="0.05"
              max="0.9"
              step="0.05"
              value={confidence}
              onChange={(e) => setConfidence(e.target.value)}
            />
          </div>
        </div>

        <div className="form-row" style={{ marginTop: 14 }}>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={saveAnnotated}
              onChange={(e) => setSaveAnnotated(e.target.checked)}
            />
            Save annotated video
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={allStaff}
              disabled={stockroom}
              onChange={(e) => setAllStaff(e.target.checked)}
            />
            Treat all as staff {stockroom && "(forced for stockroom)"}
          </label>
          <button className="btn" disabled={busy} onClick={onStart}>
            {busy ? "Starting…" : "Start processing"}
          </button>
        </div>

        {stockroom && (
          <div className="sub" style={{ marginTop: 10 }}>
            CAM_STOCKROOM_01 is a non-customer area — all detections are flagged
            <strong> is_staff=true</strong> and excluded from visitor analytics.
          </div>
        )}
      </div>

      <div className="section">
        <ErrorBox message={error} />
      </div>

      {job && (
        <div className="section" style={{ display: "flex", gap: "20px", flexDirection: "column" }}>
          {/* ── Job Progress Bar ── */}
          <JobPanel job={job} jobId={jobId} />

          {/* ── Video Comparison: Original vs Live Detection ── */}
          <div style={{ display: "flex", gap: "20px", flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 48%", minWidth: "300px" }}>
              <h3 style={{ margin: "0 0 8px 0" }}>Original CCTV Feed</h3>
              <video
                controls
                autoPlay={isRunning}
                muted
                src={api.originalVideoUrl(jobId)}
                style={{ width: "100%", borderRadius: "8px", background: "#111" }}
              />
            </div>
            <div style={{ flex: "1 1 48%", minWidth: "300px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                <h3 style={{ margin: 0 }}>Live Detection Output</h3>
                {isRunning && <span className="pill warn" style={{ fontSize: "11px" }}>LIVE</span>}
              </div>
              {isRunning && job.has_latest_frame ? (
                <LiveFramePreview jobId={jobId} />
              ) : job.status === "completed" && job.has_annotated_video ? (
                <video
                  key={`${jobId}_${job.status}`}
                  controls
                  autoPlay
                  src={`${api.annotatedVideoUrl(jobId)}?t=${Date.now()}`}
                  style={{ width: "100%", borderRadius: "8px", background: "#111" }}
                />
              ) : (
                <div className="card" style={{ height: "300px", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <span className="muted">Waiting for processing to start…</span>
                </div>
              )}
            </div>
          </div>

          {/* ── Live Store Analytics (ALL panels visible at once) ── */}
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
              <h3 style={{ margin: 0 }}>Live Store Analytics — {storeId}</h3>
              {isRunning && <span className="pill warn" style={{ fontSize: "11px" }}>Updating every 2s…</span>}
            </div>
            <Metrics storeId={storeId} processing={isRunning} />
            
            <div style={{ marginTop: "16px" }}>
              <h4 style={{ margin: "0 0 8px 0" }}>Conversion Funnel</h4>
              <Funnel storeId={storeId} processing={isRunning} />
            </div>

            <div style={{ display: "flex", gap: "20px", flexWrap: "wrap", marginTop: "24px" }}>
              <div style={{ flex: "1 1 48%", minWidth: "300px" }}>
                <h4 style={{ margin: "0 0 8px 0" }}>Zone Heatmap</h4>
                <Heatmap storeId={storeId} processing={isRunning} />
              </div>
              <div style={{ flex: "1 1 48%", minWidth: "300px" }}>
                <h4 style={{ margin: "0 0 8px 0" }}>Anomaly Detection</h4>
                <Anomalies storeId={storeId} processing={isRunning} />
              </div>
            </div>
          </div>

          {/* ── Live Events Feed ── */}
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
              <h3 style={{ margin: 0 }}>Live Event Stream ({events.length} events)</h3>
              {isRunning && <span className="pill warn" style={{ fontSize: "11px" }}>Streaming…</span>}
            </div>
            <div className="card" style={{ maxHeight: "400px", overflowY: "auto" }}>
              {events.length === 0 ? (
                <div className="empty">No events yet. Start processing to see events appear in real-time.</div>
              ) : (
                <div className="table-wrap">
                  <table style={{ fontSize: "12px" }}>
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Type</th>
                        <th>Camera</th>
                        <th>Visitor</th>
                        <th>Zone</th>
                        <th>Who</th>
                      </tr>
                    </thead>
                    <tbody>
                      {events.slice().reverse().slice(0, 100).map((e, i) => (
                        <tr key={e.event_id || i}>
                          <td className="tag" style={{ whiteSpace: "nowrap" }}>{(e.timestamp || "").split("T")[1]?.slice(0, 8) || ""}</td>
                          <td><strong>{e.event_type}</strong></td>
                          <td className="tag">{e.camera_id}</td>
                          <td className="tag">{e.visitor_id}</td>
                          <td>{e.zone_id || "—"}</td>
                          <td>
                            <span className={`badge ${e.is_staff ? "staff" : "customer"}`} style={{ padding: "2px 6px", fontSize: "10px" }}>
                              {e.is_staff ? "staff" : "customer"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


/* ── Live annotated frame preview (refreshes every 1s while processing) ── */
function LiveFramePreview({ jobId }) {
  const [ts, setTs] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setTs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <img
      src={`${api.latestFrameUrl(jobId)}?t=${ts}`}
      alt="Live annotated frame"
      style={{
        width: "100%",
        borderRadius: "8px",
        background: "#111",
        minHeight: "200px",
        objectFit: "contain",
      }}
      onError={(e) => { e.target.style.opacity = "0.3"; }}
      onLoad={(e) => { e.target.style.opacity = "1"; }}
    />
  );
}


function JobPanel({ job, jobId }) {
  const total = job.frames_processed || 0;
  const denom = 300; // visual reference only; not a metric
  const pct = Math.min(100, Math.round((total / denom) * 100));

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <span className={`status-chip ${job.status}`}>{job.status}</span>
        <span className="tag muted">{job.filename}</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="muted">{job.camera_id} · {job.store_id}</span>
      </div>

      {job.status === "running" && (
        <div className="progressbar" style={{ marginBottom: 14 }}>
          <div style={{ width: `${pct}%` }} />
        </div>
      )}

      <div className="grid cards">
        <div className="card"><div className="label">Frames processed</div><div className="value">{job.frames_processed}</div></div>
        <div className="card"><div className="label">Events emitted</div><div className="value">{job.events_emitted}</div></div>
        <div className="card"><div className="label">Events ingested</div><div className="value">{job.events_ingested || 0}</div></div>
        <div className="card"><div className="label">Frames written</div><div className="value">{job.frames_written}</div></div>
      </div>
      {job.last_flush_at && (
        <div style={{ marginTop: "10px", fontSize: "12px", color: "#888" }}>
          Last flush: {new Date(job.last_flush_at).toLocaleTimeString()}
        </div>
      )}

      {job.error && <div className="error-box" style={{ marginTop: 12 }}>⚠ {job.error}</div>}
    </div>
  );
}

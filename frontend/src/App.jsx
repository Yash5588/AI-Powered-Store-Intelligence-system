import React, { useEffect, useState } from "react";
import { api } from "./api";
import Overview from "./pages/Overview";
import VideoProcessing from "./pages/VideoProcessing";
import LiveEvents from "./pages/LiveEvents";
import Metrics from "./pages/Metrics";
import Funnel from "./pages/Funnel";
import Heatmap from "./pages/Heatmap";
import Anomalies from "./pages/Anomalies";
import Runbook from "./pages/Runbook";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "video", label: "Video Processing" },
  { id: "events", label: "Live Events" },
  { id: "metrics", label: "Metrics" },
  { id: "funnel", label: "Funnel" },
  { id: "heatmap", label: "Heatmap" },
  { id: "anomalies", label: "Anomalies" },
  { id: "runbook", label: "Runbook" },
];

export default function App() {
  const [tab, setTab] = useState("overview");
  const [storeId, setStoreId] = useState("ST1008");
  const [health, setHealth] = useState(null);
  const [online, setOnline] = useState(null);
  const [processing, setProcessing] = useState(false);

  // Top-bar health poll + "is any job running" flag to drive 2s analytics polling.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const h = await api.health();
      if (cancelled) return;
      setOnline(!h.error);
      setHealth(h.data);
      const jobs = await api.listVideos();
      if (cancelled) return;
      setProcessing((jobs.data?.jobs || []).some((j) => j.status === "running"));
    }
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const stale = (health?.warnings || []).some((w) => w.startsWith("STALE_FEED"));
  const statusDot = online == null ? "" : online ? (stale ? "warn" : "ok") : "bad";
  const statusText =
    online == null ? "Checking…" : online ? (stale ? "Stale feed" : "API online") : "API offline";

  function render() {
    switch (tab) {
      case "overview": return <Overview storeId={storeId} />;
      case "video": return <VideoProcessing storeId={storeId} setStoreId={setStoreId} />;
      case "events": return <LiveEvents />;
      case "metrics": return <Metrics storeId={storeId} processing={processing} />;
      case "funnel": return <Funnel storeId={storeId} processing={processing} />;
      case "heatmap": return <Heatmap storeId={storeId} processing={processing} />;
      case "anomalies": return <Anomalies storeId={storeId} processing={processing} />;
      case "runbook": return <Runbook />;
      default: return null;
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">Store<span>·</span>Intelligence</div>
        <nav className="nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          Offline retail analytics<br />from raw CCTV.
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="title">{TABS.find((t) => t.id === tab)?.label}</div>
          <div className="spacer" />
          {processing && <span className="pill"><span className="dot warn" /> processing…</span>}
          <span className="pill">
            <span className={`dot ${statusDot}`} /> {statusText}
          </span>
          <span className="pill">
            store
            <input
              className="store-input"
              value={storeId}
              onChange={(e) => setStoreId(e.target.value)}
            />
          </span>
        </header>
        <div className="content">{render()}</div>
      </main>
    </div>
  );
}

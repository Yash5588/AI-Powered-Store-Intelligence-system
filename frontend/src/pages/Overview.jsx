import React, { useEffect, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Loading } from "../components/Common";

export default function Overview({ storeId }) {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [clearMsg, setClearMsg] = useState(null);

  async function load() {
    const { data, error } = await api.health();
    if (error) setError(error);
    else {
      setHealth(data);
      setError(null);
    }
    setLoading(false);
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  async function handleClear() {
    if (!window.confirm("This will delete ALL events from the database and reset video jobs. Continue?")) {
      return;
    }
    setClearing(true);
    setClearMsg(null);
    const { data, error } = await api.clearEvents();
    setClearing(false);
    if (error) {
      setClearMsg({ type: "error", text: `Failed to clear: ${error}` });
    } else {
      setClearMsg({ type: "success", text: `Cleared ${data.deleted_events} events. Database is clean.` });
      load(); // refresh health immediately
    }
  }

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={`API unavailable: ${error}`} />;

  const stale = (health?.warnings || []).filter((w) => w.startsWith("STALE_FEED"));
  const future = (health?.warnings || []).filter((w) => w.startsWith("FUTURE_EVENT"));

  return (
    <div>
      <div className="grid cards">
        <Card label="API Status" value={health.status === "ok" ? "OK" : health.status} />
        <Card label="Database" value={health.database} />
        <Card label="Event Count" value={(health.event_count ?? 0).toLocaleString()} />
        <Card label="Stores Tracked" value={(health.stores || []).length} />
        <Card label="Selected Store" value={storeId} />
      </div>

      {stale.length > 0 && (
        <div className="section">
          <ErrorBox message={`Stale feed: ${stale.join(", ")}`} />
        </div>
      )}
      {future.length > 0 && (
        <div className="section">
          <ErrorBox message={`Future event timestamps: ${future.join(", ")}`} />
        </div>
      )}

      {/* ── Clear Database ── */}
      <div className="section">
        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <button
            id="clear-database-btn"
            className="btn"
            disabled={clearing}
            onClick={handleClear}
            style={{
              background: "linear-gradient(135deg, #e74c3c, #c0392b)",
              border: "none",
              color: "#fff",
              padding: "10px 24px",
              borderRadius: "8px",
              cursor: clearing ? "not-allowed" : "pointer",
              fontWeight: 600,
              fontSize: "13px",
              opacity: clearing ? 0.6 : 1,
              transition: "opacity 0.2s, transform 0.15s",
            }}
          >
            {clearing ? "Clearing…" : "🗑️ Clear Database"}
          </button>
          <span className="muted" style={{ fontSize: "12px" }}>
            Removes all events &amp; resets video jobs for a clean demo.
          </span>
        </div>
        {clearMsg && (
          <div
            style={{
              marginTop: 10,
              padding: "10px 16px",
              borderRadius: "8px",
              background: clearMsg.type === "success" ? "rgba(46, 204, 113, 0.15)" : "rgba(231, 76, 60, 0.15)",
              border: `1px solid ${clearMsg.type === "success" ? "rgba(46, 204, 113, 0.4)" : "rgba(231, 76, 60, 0.4)"}`,
              color: clearMsg.type === "success" ? "#2ecc71" : "#e74c3c",
              fontSize: "13px",
            }}
          >
            {clearMsg.text}
          </div>
        )}
      </div>

      <div className="section">
        <h3>Per-store feed freshness</h3>
        {(health.stores || []).length === 0 ? (
          <div className="empty">No events ingested yet. Use the Video Processing tab.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Store</th>
                  <th>Last event</th>
                  <th>Lag (s)</th>
                  <th>Stale?</th>
                </tr>
              </thead>
              <tbody>
                {health.stores.map((s) => (
                  <tr key={s.store_id}>
                    <td className="tag">{s.store_id}</td>
                    <td>{s.last_event_timestamp || "—"}</td>
                    <td>{Math.round(s.lag_seconds)}</td>
                    <td>{s.stale_feed ? "yes" : "no"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

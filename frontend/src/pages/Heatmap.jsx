import React, { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorBox, Loading, Empty } from "../components/Common";

export default function Heatmap({ storeId, processing }) {
  const [h, setH] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    const { data, error } = await api.heatmap(storeId);
    if (error) setError(error);
    else {
      setH(data);
      setError(null);
    }
    setLoading(false);
  }

  useEffect(() => {
    load();
    const t = setInterval(load, processing ? 2000 : 5000);
    return () => clearInterval(t);
  }, [storeId, processing]);

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} />;

  const cells = (h?.cells || []).slice().sort((a, b) => b.normalized_score - a.normalized_score);

  function heat(score) {
    // 0..100 -> green→amber→red gradient cell background.
    const hue = 140 - Math.round((score / 100) * 140); // 140=green, 0=red
    return `hsl(${hue}, 70%, 40%)`;
  }

  return (
    <div>
      <div className="sub muted" style={{ marginBottom: 12 }}>
        {storeId} · sessions {h?.session_count ?? 0} · confidence {String(h?.data_confidence)}
      </div>
      {cells.length === 0 ? (
        <Empty text="No zone activity yet for this store." />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Zone</th>
                <th>Visit frequency</th>
                <th>Avg dwell (s)</th>
                <th>Normalized score</th>
              </tr>
            </thead>
            <tbody>
              {cells.map((c) => (
                <tr key={c.zone_id}>
                  <td>{c.zone_id}</td>
                  <td>{c.visit_frequency}</td>
                  <td>{(c.avg_dwell_seconds ?? 0).toFixed(1)}</td>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div
                        style={{
                          width: 60, height: 12, borderRadius: 4,
                          background: heat(c.normalized_score),
                        }}
                      />
                      {Math.round(c.normalized_score)}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

import { api } from "../api";
import { Card, ErrorBox, Loading } from "../components/Common";
import { usePollingResource } from "../hooks/usePollingResource";

export default function Metrics({ storeId, processing }) {
  const { data: m, error, loading } = usePollingResource(
    () => api.metrics(storeId),
    [storeId, processing],
    processing ? 3000 : 5000
  );

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} />;
  if (!m) return null;

  const conv = `${((m.conversion_rate || 0) * 100).toFixed(1)}%`;
  const aband = `${((m.abandonment_rate || 0) * 100).toFixed(1)}%`;

  return (
    <div>
      <div className="grid cards">
        <Card label="Unique Visitors" value={m.unique_visitors ?? 0} />
        <Card label="Converted Visitors" value={m.converted_visitors ?? 0} />
        <Card label="Conversion Rate" value={conv} sub="purchasing ÷ unique" />
        <Card label="Current Queue Depth" value={m.current_queue_depth ?? 0} />
        <Card label="Abandonment Rate" value={aband} />
        <Card label="Transactions (POS)" value={m.transactions ?? 0} />
      </div>

      <div className="section">
        <h3>Average dwell per zone</h3>
        {(m.avg_dwell_per_zone || []).length === 0 ? (
          <div className="empty">No dwell data for {storeId} yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Zone</th><th>Avg dwell (s)</th></tr></thead>
              <tbody>
                {m.avg_dwell_per_zone.map((z) => (
                  <tr key={z.zone_id}>
                    <td>{z.zone_id}</td>
                    <td>{(z.avg_dwell_seconds ?? z.avg_dwell_ms / 1000 ?? 0).toFixed?.(1) ?? z.avg_dwell_seconds}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="sub muted" style={{ marginTop: 8 }}>
        Data confidence: {String(m.data_confidence)} · window {m.window_start || "—"} → {m.window_end || "—"}
      </div>
    </div>
  );
}

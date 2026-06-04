import { api } from "../api";
import { ErrorBox, Loading, Empty } from "../components/Common";
import { usePollingResource } from "../hooks/usePollingResource";

export default function Anomalies({ storeId, processing }) {
  const { data: a, error, loading } = usePollingResource(
    () => api.anomalies(storeId),
    [storeId, processing],
    processing ? 3000 : 5000
  );

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} />;

  const items = a?.anomalies || [];

  return (
    <div>
      <div className="sub muted" style={{ marginBottom: 12 }}>
        {storeId} · {a?.anomaly_count ?? 0} active anomalies · evaluated {a?.evaluated_at}
      </div>
      {items.length === 0 ? (
        <Empty text="No anomalies detected. Operations look healthy." />
      ) : (
        items.map((an, i) => (
          <div className={`anomaly ${an.severity}`} key={i}>
            <div className={`sev ${an.severity}`}>
              {an.severity} · {an.anomaly_type}
              {an.zone_id ? ` · ${an.zone_id}` : ""}
            </div>
            <div className="msg">{an.message}</div>
            <div className="action">→ {an.suggested_action}</div>
          </div>
        ))
      )}
    </div>
  );
}

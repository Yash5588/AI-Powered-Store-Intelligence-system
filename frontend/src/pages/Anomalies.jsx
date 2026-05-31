import React, { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorBox, Loading, Empty } from "../components/Common";

export default function Anomalies({ storeId, processing }) {
  const [a, setA] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    const { data, error } = await api.anomalies(storeId);
    if (error) setError(error);
    else {
      setA(data);
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

import { api } from "../api";
import { ErrorBox, Loading, Empty } from "../components/Common";
import { usePollingResource } from "../hooks/usePollingResource";

export default function Funnel({ storeId, processing }) {
  const { data: f, error, loading } = usePollingResource(
    () => api.funnel(storeId),
    [storeId, processing],
    processing ? 3000 : 5000
  );

  if (loading) return <Loading />;
  if (error) return <ErrorBox message={error} />;

  const stages = f?.stages || [];
  const top = stages.length ? Math.max(1, stages[0].count) : 1;

  return (
    <div>
      <div className="card">
        <div className="sub muted" style={{ marginBottom: 16 }}>
          Session-based, nested funnel for <b className="tag">{storeId}</b> · total
          sessions {f?.total_sessions ?? 0}. Each stage is a subset of the one above
          (Entry ≥ Zone Visit ≥ Billing Queue ≥ Purchase).
        </div>
        {stages.length === 0 ? (
          <Empty text="No funnel data yet. Process a video or seed events." />
        ) : (
          stages.map((s) => {
            const width = Math.max(6, Math.round((s.count / top) * 100));
            return (
              <div className="funnel-row" key={s.stage}>
                <div>{s.stage}</div>
                <div className="funnel-bar">
                  <div className="funnel-fill" style={{ width: `${width}%` }}>
                    {s.count}
                  </div>
                </div>
                <div>
                  {s.drop_off_pct > 0 ? (
                    <span className="drop">▼ {s.drop_off_pct}% drop</span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

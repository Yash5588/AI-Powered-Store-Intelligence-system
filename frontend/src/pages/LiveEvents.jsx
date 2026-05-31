import React, { useEffect, useMemo, useState } from "react";
import { api, CAMERAS } from "../api";
import { ErrorBox, Empty } from "../components/Common";

const EVENT_TYPES = [
  "ENTRY", "REENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
  "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON",
];

export default function LiveEvents() {
  const [jobs, setJobs] = useState([]);
  const [jobId, setJobId] = useState("");
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const [fType, setFType] = useState("");
  const [fCamera, setFCamera] = useState("");
  const [fStaff, setFStaff] = useState("");

  async function loadJobs() {
    const { data } = await api.listVideos();
    if (data?.jobs) {
      setJobs(data.jobs);
      if (!jobId && data.jobs.length) setJobId(data.jobs[0].job_id);
    }
  }

  async function loadEvents(id) {
    if (!id) return;
    const { data, error } = await api.videoEvents(id, 200);
    if (error) setError(error);
    else {
      setEvents(data.events || []);
      setError(null);
    }
  }

  useEffect(() => {
    loadJobs();
  }, []);

  useEffect(() => {
    if (!jobId) return;
    loadEvents(jobId);
    const t = setInterval(() => loadEvents(jobId), 2000);
    return () => clearInterval(t);
  }, [jobId]);

  const filtered = useMemo(
    () =>
      events.filter((e) => {
        if (fType && e.event_type !== fType) return false;
        if (fCamera && e.camera_id !== fCamera) return false;
        if (fStaff === "staff" && !e.is_staff) return false;
        if (fStaff === "customer" && e.is_staff) return false;
        return true;
      }),
    [events, fType, fCamera, fStaff]
  );

  const counts = useMemo(() => {
    const c = {};
    for (const e of filtered) c[e.event_type] = (c[e.event_type] || 0) + 1;
    return c;
  }, [filtered]);

  return (
    <div>
      <div className="card">
        <div className="form-row">
          <div className="field">
            <label>Job</label>
            <select value={jobId} onChange={(e) => setJobId(e.target.value)}>
              <option value="">— select a job —</option>
              {jobs.map((j) => (
                <option key={j.job_id} value={j.job_id}>
                  {j.filename} · {j.camera_id || "?"} · {j.status}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Event type</label>
            <select value={fType} onChange={(e) => setFType(e.target.value)}>
              <option value="">All</option>
              {EVENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Camera</label>
            <select value={fCamera} onChange={(e) => setFCamera(e.target.value)}>
              <option value="">All</option>
              {CAMERAS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Staff / customer</label>
            <select value={fStaff} onChange={(e) => setFStaff(e.target.value)}>
              <option value="">All</option>
              <option value="customer">Customer</option>
              <option value="staff">Staff</option>
            </select>
          </div>
        </div>
      </div>

      <div className="section">
        <ErrorBox message={error} />
        <div className="grid cards">
          {Object.keys(counts).length === 0 ? null : (
            Object.entries(counts).map(([t, n]) => (
              <div className="card" key={t}>
                <div className="label">{t}</div>
                <div className="value">{n}</div>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="section">
        <h3>Recent events ({filtered.length})</h3>
        {filtered.length === 0 ? (
          <Empty text="No events for this selection. Process a video, or relax the filters." />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Type</th>
                  <th>Camera</th>
                  <th>Visitor</th>
                  <th>Zone</th>
                  <th>Conf</th>
                  <th>Who</th>
                </tr>
              </thead>
              <tbody>
                {filtered.slice().reverse().slice(0, 100).map((e, i) => (
                  <tr key={e.event_id || i}>
                    <td className="tag">{(e.timestamp || "").replace("T", " ").replace("Z", "")}</td>
                    <td>{e.event_type}</td>
                    <td className="tag">{e.camera_id}</td>
                    <td className="tag">{e.visitor_id}</td>
                    <td>{e.zone_id || "—"}</td>
                    <td>{e.confidence}</td>
                    <td>
                      <span className={`badge ${e.is_staff ? "staff" : "customer"}`}>
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
  );
}

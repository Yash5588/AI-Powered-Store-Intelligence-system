import React from "react";

export function Card({ label, value, sub }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}

export function ErrorBox({ message }) {
  if (!message) return null;
  return <div className="error-box">⚠ {message}</div>;
}

export function Empty({ text = "No data yet." }) {
  return <div className="empty">{text}</div>;
}

export function Loading({ text = "Loading…" }) {
  return <div className="empty">{text}</div>;
}

export function pct(n) {
  if (n == null || Number.isNaN(n)) return "0%";
  // API returns conversion_rate as 0..1 and drop-offs as 0..100; callers pass
  // the already-correct number, so just format.
  return `${n}%`;
}

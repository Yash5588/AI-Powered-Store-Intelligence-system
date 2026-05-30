"""Interactive web dashboard for the Store Intelligence System (Streamlit).

Run:
    streamlit run dashboard/app.py            # http://localhost:8501

It is a thin VIEW over the API — it never computes analytics itself. Every
number comes from the existing endpoints:
    GET /stores/{id}/metrics | /funnel | /heatmap | /anomalies
    GET /health
Demo controls can ingest data/generated_events.jsonl or run the synthetic
simulator so the whole loop can be shown from the browser.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# Optional nicer auto-refresh; degrade gracefully if not installed.
try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except ImportError:  # pragma: no cover
    _HAS_AUTOREFRESH = False

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_FILE = REPO_ROOT / "data" / "generated_events.jsonl"
SIM_BATCH = 500
REQUEST_TIMEOUT = 8
# In docker-compose the API is reachable as http://api:8000; locally it's
# http://localhost:8000. The env var lets compose override the default.
DEFAULT_API_URL = os.getenv("STORE_INTELLIGENCE_API_URL", "http://localhost:8000")

st.set_page_config(page_title="Store Intelligence", page_icon="🏬", layout="wide")


# --------------------------------------------------------------------------- #
# API client (all data comes from here — nothing is hardcoded)
# --------------------------------------------------------------------------- #
def api_get(base_url: str, path: str) -> tuple[dict | None, str | None]:
    """GET a JSON endpoint. Returns (data, error_message)."""
    try:
        resp = requests.get(base_url.rstrip("/") + path, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except requests.RequestException as exc:
        return None, str(exc)


def api_post_events(base_url: str, events: list[dict]) -> tuple[dict | None, str | None]:
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/events/ingest", json=events, timeout=60
        )
        if resp.status_code >= 500:
            return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except requests.RequestException as exc:
        return None, str(exc)


# --------------------------------------------------------------------------- #
# Sidebar — connection + refresh + demo controls
# --------------------------------------------------------------------------- #
def sidebar() -> dict:
    st.sidebar.title("🏬 Store Intelligence")
    st.sidebar.caption("Live web dashboard")

    base_url = st.sidebar.text_input("API base URL", value=DEFAULT_API_URL)

    # Try to populate a store dropdown from /health; fall back to a text input.
    health, _ = api_get(base_url, "/health")
    store_ids = []
    if health and health.get("stores"):
        store_ids = sorted({s["store_id"] for s in health["stores"]})

    if store_ids:
        default_idx = store_ids.index("STORE_BLR_002") if "STORE_BLR_002" in store_ids else 0
        store_id = st.sidebar.selectbox("Store", store_ids, index=default_idx)
    else:
        store_id = st.sidebar.text_input("Store ID", value="STORE_BLR_002")

    st.sidebar.divider()
    auto_refresh = st.sidebar.toggle("Auto-refresh", value=False)
    interval = st.sidebar.number_input(
        "Refresh interval (seconds)", min_value=1, max_value=60, value=2, step=1
    )
    if st.sidebar.button("🔄 Refresh now", use_container_width=True):
        st.rerun()

    st.sidebar.divider()
    _demo_controls(base_url, store_id)

    return {
        "base_url": base_url,
        "store_id": store_id,
        "auto_refresh": auto_refresh,
        "interval": int(interval),
        "health": health,
    }


def _demo_controls(base_url: str, store_id: str) -> None:
    st.sidebar.subheader("Demo controls")

    # 1) Ingest data/generated_events.jsonl
    if st.sidebar.button("📥 Ingest generated_events.jsonl", use_container_width=True):
        if not EVENTS_FILE.exists():
            st.sidebar.error(f"Not found: {EVENTS_FILE.name}. Run the simulator or detector first.")
        else:
            try:
                events = [json.loads(line) for line in EVENTS_FILE.read_text().splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                st.sidebar.error(f"Bad JSONL: {exc}")
                events = []
            if events:
                total = {"accepted": 0, "duplicates": 0, "rejected": 0}
                err = None
                for i in range(0, len(events), SIM_BATCH):
                    res, err = api_post_events(base_url, events[i : i + SIM_BATCH])
                    if err:
                        break
                    for k in total:
                        total[k] += res.get(k, 0)
                if err:
                    st.sidebar.error(f"Ingest failed: {err}")
                else:
                    st.sidebar.success(
                        f"Ingested {len(events)} events — "
                        f"{total['accepted']} new, {total['duplicates']} dup, {total['rejected']} rejected."
                    )

    # 2) Run the synthetic simulator (writes + posts events).
    n_visitors = st.sidebar.slider("Simulator visitors", 5, 100, 30, step=5)
    if st.sidebar.button("🎬 Run synthetic simulation", use_container_width=True):
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "pipeline.simulate_events",
                    "--store", store_id, "--visitors", str(n_visitors),
                    "--post", base_url,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0:
                st.sidebar.success(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "Simulation complete.")
            else:
                st.sidebar.error(f"Simulator error: {(proc.stderr or proc.stdout)[:300]}")
        except (subprocess.SubprocessError, OSError) as exc:
            st.sidebar.error(f"Could not run simulator: {exc}")


# --------------------------------------------------------------------------- #
# Main panels
# --------------------------------------------------------------------------- #
def render_metrics(base_url: str, store_id: str) -> None:
    st.subheader("📊 Metrics")
    data, err = api_get(base_url, f"/stores/{store_id}/metrics")
    if err:
        st.error(f"Could not reach API: {err}")
        return
    if not data or data.get("unique_visitors", 0) == 0:
        st.info("No visitor data yet for this store. Use **Demo controls** in the sidebar to ingest or simulate events.")
        # Still show zeroed cards for a clean empty state.
        data = data or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Unique visitors", data.get("unique_visitors", 0))
    c2.metric("Conversion rate", f"{data.get('conversion_rate', 0.0) * 100:.1f}%")
    c3.metric("Queue depth", data.get("current_queue_depth", 0))
    c4.metric("Abandonment rate", f"{data.get('abandonment_rate', 0.0) * 100:.1f}%")

    zones = data.get("avg_dwell_per_zone", [])
    if zones:
        df = pd.DataFrame(zones)[["zone_id", "avg_dwell_seconds"]].rename(
            columns={"zone_id": "Zone", "avg_dwell_seconds": "Avg dwell (s)"}
        )
        st.caption("Average dwell per zone")
        st.bar_chart(df.set_index("Zone"))


def render_funnel(base_url: str, store_id: str) -> None:
    st.subheader("🔻 Conversion funnel")
    data, err = api_get(base_url, f"/stores/{store_id}/funnel")
    if err:
        st.error(f"Could not reach API: {err}")
        return
    stages = (data or {}).get("stages", [])
    if not stages or (data or {}).get("total_sessions", 0) == 0:
        st.info("No sessions yet — funnel will populate once events are ingested.")
        return
    df = pd.DataFrame(stages).rename(
        columns={"stage": "Stage", "count": "Count", "drop_off_pct": "Drop-off %"}
    )
    left, right = st.columns([2, 1])
    with left:
        st.bar_chart(df.set_index("Stage")["Count"])
    with right:
        st.dataframe(df, hide_index=True, use_container_width=True)


def render_heatmap(base_url: str, store_id: str) -> None:
    st.subheader("🗺️ Zone heatmap")
    data, err = api_get(base_url, f"/stores/{store_id}/heatmap")
    if err:
        st.error(f"Could not reach API: {err}")
        return
    cells = (data or {}).get("cells", [])
    if not cells:
        st.info("No zone activity yet.")
        return
    if not data.get("data_confidence", True):
        st.warning(
            f"⚠️ Low data confidence — only {data.get('session_count', 0)} sessions "
            "in window (≥20 recommended). Treat scores as indicative."
        )
    df = pd.DataFrame(cells).rename(
        columns={
            "zone_id": "Zone",
            "visit_frequency": "Visits",
            "avg_dwell_seconds": "Avg dwell (s)",
            "normalized_score": "Score (0-100)",
        }
    )[["Zone", "Visits", "Avg dwell (s)", "Score (0-100)"]]
    left, right = st.columns([1, 1])
    with left:
        st.bar_chart(df.set_index("Zone")["Score (0-100)"])
    with right:
        st.dataframe(df, hide_index=True, use_container_width=True)


def render_anomalies(base_url: str, store_id: str) -> None:
    st.subheader("🚨 Active anomalies")
    data, err = api_get(base_url, f"/stores/{store_id}/anomalies")
    if err:
        st.error(f"Could not reach API: {err}")
        return
    anomalies = (data or {}).get("anomalies", [])
    if not anomalies:
        st.success("✅ No active anomalies.")
        return
    icon = {"CRITICAL": "🔴", "WARN": "🟡", "INFO": "🔵"}
    for a in anomalies:
        sev = a.get("severity", "INFO")
        with st.container(border=True):
            st.markdown(f"{icon.get(sev, '⚪')} **{sev} — {a.get('anomaly_type', '')}**")
            st.write(a.get("message", ""))
            st.caption(f"💡 Suggested action: {a.get('suggested_action', '—')}")
            if a.get("timestamp"):
                st.caption(f"🕒 {a['timestamp']}")


def render_health(base_url: str, health: dict | None) -> None:
    st.subheader("❤️ Service health")
    if not health:
        health, err = api_get(base_url, "/health")
        if err:
            st.error(f"Could not reach API: {err}")
            return
    status = health.get("status", "unknown")
    badge = "🟢" if status == "ok" else "🔴"
    c1, c2, c3 = st.columns(3)
    c1.metric("Service", f"{badge} {status}")
    c2.metric("Database", health.get("database", "?"))
    c3.metric("Total events", health.get("event_count", 0))

    warnings = health.get("warnings", [])
    if warnings:
        for w in warnings:
            st.warning(f"⚠️ {w}")

    stores = health.get("stores", [])
    if stores:
        df = pd.DataFrame(stores).rename(
            columns={
                "store_id": "Store",
                "last_event_timestamp": "Last event",
                "lag_seconds": "Lag (s)",
                "stale_feed": "Stale?",
            }
        )
        st.caption("Per-store feed freshness")
        st.dataframe(df, hide_index=True, use_container_width=True)
    else:
        st.info("No stores have reported events yet.")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def main() -> None:
    cfg = sidebar()

    if cfg["auto_refresh"]:
        if _HAS_AUTOREFRESH:
            st_autorefresh(interval=cfg["interval"] * 1000, key="auto_refresh")
        else:
            st.caption("`streamlit-autorefresh` not installed — use 🔄 Refresh now.")

    st.title("Store Intelligence — Live Dashboard")
    st.caption(f"Store **{cfg['store_id']}** · API `{cfg['base_url']}`")

    render_metrics(cfg["base_url"], cfg["store_id"])
    st.divider()
    col_left, col_right = st.columns(2)
    with col_left:
        render_funnel(cfg["base_url"], cfg["store_id"])
    with col_right:
        render_heatmap(cfg["base_url"], cfg["store_id"])
    st.divider()
    render_anomalies(cfg["base_url"], cfg["store_id"])
    st.divider()
    render_health(cfg["base_url"], cfg["health"])


main()

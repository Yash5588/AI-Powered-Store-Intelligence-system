"""Live terminal dashboard  [PHASE 5/E].

Polls the Intelligence API every 2 seconds and renders, for one store:
  * unique visitors
  * conversion rate
  * current billing queue depth
  * active anomalies (with severity)

Uses `rich` for a live table when available; otherwise falls back to plain
prints so it runs with zero extra dependencies.

Usage:
  python -m dashboard.terminal_dashboard --store STORE_BLR_002 --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import time
from urllib import error as urlerror
from urllib import request as urlrequest


def _get(base_url: str, path: str) -> dict | None:
    url = base_url.rstrip("/") + path
    try:
        with urlrequest.urlopen(url, timeout=5) as resp:  # noqa: S310 - local URL
            return json.loads(resp.read().decode("utf-8"))
    except (urlerror.URLError, TimeoutError, ValueError):
        return None


def fetch_snapshot(base_url: str, store_id: str) -> dict:
    """Pull the metrics + anomalies the dashboard displays."""
    metrics = _get(base_url, f"/stores/{store_id}/metrics") or {}
    anomalies = _get(base_url, f"/stores/{store_id}/anomalies") or {}
    health = _get(base_url, "/health") or {}
    return {"metrics": metrics, "anomalies": anomalies, "health": health}


def _plain_render(store_id: str, snap: dict) -> None:
    m = snap["metrics"]
    a = snap["anomalies"]
    print("\033[2J\033[H", end="")  # clear screen
    print(f"=== Store Intelligence — {store_id} ===")
    print(f"Unique visitors : {m.get('unique_visitors', 0)}")
    print(f"Conversion rate : {m.get('conversion_rate', 0.0) * 100:.1f}%")
    print(f"Queue depth     : {m.get('current_queue_depth', 0)}")
    print(f"Abandonment     : {m.get('abandonment_rate', 0.0) * 100:.1f}%")
    anoms = a.get("anomalies", [])
    print(f"Active anomalies: {len(anoms)}")
    for an in anoms:
        print(f"  [{an.get('severity')}] {an.get('anomaly_type')}: {an.get('message')}")


def _rich_table(store_id: str, snap: dict):
    from rich.panel import Panel
    from rich.table import Table

    m = snap["metrics"]
    a = snap["anomalies"]

    metrics_tbl = Table(title=f"Store {store_id} — live metrics", expand=True)
    metrics_tbl.add_column("Metric")
    metrics_tbl.add_column("Value", justify="right")
    metrics_tbl.add_row("Unique visitors", str(m.get("unique_visitors", 0)))
    metrics_tbl.add_row("Conversion rate", f"{m.get('conversion_rate', 0.0) * 100:.1f}%")
    metrics_tbl.add_row("Queue depth", str(m.get("current_queue_depth", 0)))
    metrics_tbl.add_row("Abandonment rate", f"{m.get('abandonment_rate', 0.0) * 100:.1f}%")

    anoms = a.get("anomalies", [])
    anom_tbl = Table(title=f"Active anomalies ({len(anoms)})", expand=True)
    anom_tbl.add_column("Severity")
    anom_tbl.add_column("Type")
    anom_tbl.add_column("Message")
    color = {"CRITICAL": "bold red", "WARN": "yellow", "INFO": "cyan"}
    for an in anoms:
        sev = an.get("severity", "INFO")
        anom_tbl.add_row(f"[{color.get(sev, 'white')}]{sev}[/]", an.get("anomaly_type", ""), an.get("message", ""))
    if not anoms:
        anom_tbl.add_row("[green]OK[/]", "-", "No active anomalies")

    from rich.console import Group

    return Panel(Group(metrics_tbl, anom_tbl), title="Store Intelligence — live")


def run(base_url: str, store_id: str, interval: float = 2.0, iterations: int | None = None) -> None:
    """Poll loop. `iterations` bounds the loop (used by tests); None = forever."""
    try:
        from rich.live import Live

        with Live(refresh_per_second=4, screen=False) as live:
            i = 0
            while iterations is None or i < iterations:
                snap = fetch_snapshot(base_url, store_id)
                live.update(_rich_table(store_id, snap))
                i += 1
                if iterations is not None and i >= iterations:
                    break
                time.sleep(interval)
    except ImportError:
        i = 0
        while iterations is None or i < iterations:
            snap = fetch_snapshot(base_url, store_id)
            _plain_render(store_id, snap)
            i += 1
            if iterations is not None and i >= iterations:
                break
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live terminal dashboard.")
    p.add_argument("--store", default="STORE_BLR_002")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--interval", type=float, default=2.0)
    args = p.parse_args(argv)
    try:
        run(args.api, args.store, interval=args.interval)
    except KeyboardInterrupt:
        print("\n[dashboard] stopped.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())

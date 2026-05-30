# PROMPT: "Write a pytest test for a terminal dashboard module that polls a REST
#          API's /stores/{id}/metrics, /stores/{id}/anomalies, and /health every
#          2s and renders them. Without a live server, verify the snapshot
#          fetcher degrades gracefully (returns empty dicts) on connection
#          errors and that the bounded poll loop runs N iterations without
#          raising."
# CHANGES MADE: Used a non-routable API URL so _get hits a URLError path, and
#          bounded run() with iterations + interval=0 so the test is instant.

from __future__ import annotations

from dashboard import terminal_dashboard as td


def test_fetch_snapshot_graceful_on_unreachable_api():
    snap = td.fetch_snapshot("http://127.0.0.1:59999", "STORE_BLR_002")
    assert snap["metrics"] == {}
    assert snap["anomalies"] == {}
    assert snap["health"] == {}


def test_run_loop_bounded_iterations_no_raise():
    # interval 0 + 2 iterations -> returns immediately, no server required.
    td.run("http://127.0.0.1:59999", "STORE_BLR_002", interval=0.0, iterations=2)

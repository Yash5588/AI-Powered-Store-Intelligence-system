"""Tests for the video processing job endpoints.

Detection (`run_detection`) is mocked so no OpenCV/YOLO or real video is needed:
we drive the job lifecycle (upload -> process -> status -> events -> list) and
assert the manager wiring, the stockroom auto-staff rule, and error paths.
"""

from __future__ import annotations

import time

from app import video_jobs


def _fake_run_detection(*, output_path, store_id, camera_id, progress_cb=None, **kw):
    """Stand-in for pipeline.detect.run_detection: writes a couple of events."""
    if progress_cb:
        progress_cb(10, 1, 0)
    import json
    from pathlib import Path

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    is_staff = bool(kw.get("all_staff"))
    events = [
        {
            "event_id": f"ev{i}", "store_id": store_id, "camera_id": camera_id,
            "visitor_id": f"{camera_id}_bt{i:06d}", "event_type": "ENTRY",
            "timestamp": "2026-04-10T12:00:00Z", "zone_id": None, "dwell_ms": 0,
            "is_staff": is_staff, "confidence": 0.9,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        }
        for i in range(2)
    ]
    with open(output_path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return len(events)


def _wait_for_status(client, job_id, target, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/videos/{job_id}/status").json()
        if body["status"] == target:
            return body
        time.sleep(0.05)
    return client.get(f"/videos/{job_id}/status").json()


def test_upload_rejects_non_video(client):
    resp = client.post("/videos/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNSUPPORTED_VIDEO"


def test_upload_rejects_empty_file(client):
    resp = client.post("/videos/upload", files={"file": ("clip.mp4", b"", "video/mp4")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "EMPTY_FILE"


def test_upload_then_process_lifecycle(client, monkeypatch):
    monkeypatch.setattr("pipeline.detect.run_detection", _fake_run_detection)

    up = client.post("/videos/upload", files={"file": ("clip.mp4", b"\x00\x01\x02data", "video/mp4")})
    assert up.status_code == 201
    job_id = up.json()["job_id"]

    proc = client.post(f"/videos/{job_id}/process", json={
        "store_id": "ST1008", "camera_id": "CAM_FLOOR_A_01", "max_frames": 50,
    })
    assert proc.status_code == 202

    body = _wait_for_status(client, job_id, "completed")
    assert body["status"] == "completed"
    assert body["events_emitted"] == 2

    events = client.get(f"/videos/{job_id}/events").json()
    assert events["count"] == 2
    assert all(e["is_staff"] is False for e in events["events"])  # floor cam = customers


def test_stockroom_camera_forces_all_staff(client, monkeypatch):
    monkeypatch.setattr("pipeline.detect.run_detection", _fake_run_detection)

    up = client.post("/videos/upload", files={"file": ("stock.mp4", b"\x00\x01video", "video/mp4")})
    job_id = up.json()["job_id"]
    # all_staff omitted; selecting the stockroom camera must force it on.
    client.post(f"/videos/{job_id}/process", json={
        "store_id": "ST1008", "camera_id": "CAM_STOCKROOM_01", "max_frames": 50,
    })
    _wait_for_status(client, job_id, "completed")
    events = client.get(f"/videos/{job_id}/events").json()["events"]
    assert events and all(e["is_staff"] is True for e in events)


def test_process_unknown_job_404(client):
    resp = client.post("/videos/does_not_exist/process", json={
        "store_id": "ST1008", "camera_id": "CAM_ENTRY_01",
    })
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_status_unknown_job_404(client):
    assert client.get("/videos/nope/status").status_code == 404


def test_list_videos_includes_uploaded(client):
    client.post("/videos/upload", files={"file": ("a.mp4", b"\x00abc", "video/mp4")})
    body = client.get("/videos").json()
    assert body["count"] >= 1
    assert all("job_id" in j for j in body["jobs"])


def test_job_failure_is_reported(client, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("yolo exploded")

    monkeypatch.setattr("pipeline.detect.run_detection", _boom)
    up = client.post("/videos/upload", files={"file": ("clip.mp4", b"\x00data", "video/mp4")})
    job_id = up.json()["job_id"]
    client.post(f"/videos/{job_id}/process", json={"store_id": "ST1008", "camera_id": "CAM_ENTRY_01"})
    body = _wait_for_status(client, job_id, "failed")
    assert body["status"] == "failed"
    assert "yolo exploded" in body["error"]

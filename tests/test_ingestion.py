# PROMPT: "Write pytest tests for a FastAPI POST /events/ingest endpoint that
#          ingests batches of <=500 events, validates each event against a
#          Pydantic schema, deduplicates by event_id (idempotent), and returns
#          partial success: accepted / duplicates / rejected counts. Cover the
#          happy path, batch size limits, intra-batch + cross-batch dedup,
#          idempotency, malformed JSON, and per-event validation failures."
# CHANGES MADE: Added the empty-batch and dominant-store-id-in-logs cases,
#          tightened assertions to check the structured `rejected_events`
#          payload (index + reason) rather than only counts, and used a shared
#          make_event() factory from conftest so every event is schema-valid by
#          default and tests only perturb the field under test.

from __future__ import annotations

import uuid

from tests.conftest import make_event


def test_ingest_single_valid_event(client):
    resp = client.post("/events/ingest", json=[make_event()])
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] == 1
    assert body["accepted"] == 1
    assert body["duplicates"] == 0
    assert body["rejected"] == 0


def test_ingest_accepts_wrapped_events_object(client):
    resp = client.post("/events/ingest", json={"events": [make_event(), make_event()]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2


def test_intra_batch_deduplication(client):
    dupe_id = str(uuid.uuid4())
    batch = [make_event(event_id=dupe_id), make_event(event_id=dupe_id)]
    body = client.post("/events/ingest", json=batch).json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1


def test_idempotent_reingest(client):
    """POST twice with the same payload -> no new rows the second time."""
    batch = [make_event(), make_event()]
    first = client.post("/events/ingest", json=batch).json()
    second = client.post("/events/ingest", json=batch).json()
    assert first["accepted"] == 2
    assert second["accepted"] == 0
    assert second["duplicates"] == 2


def test_partial_success_with_malformed_events(client):
    good = make_event()
    bad_confidence = make_event(confidence=5.0)  # > 1.0
    missing_field = make_event()
    del missing_field["store_id"]
    not_an_object = "i am not an event"

    resp = client.post(
        "/events/ingest", json=[good, bad_confidence, missing_field, not_an_object]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] == 4
    assert body["accepted"] == 1
    assert body["rejected"] == 3
    # Structured, indexed rejection reasons (no stack traces).
    reasons = {r["index"]: r["reason"] for r in body["rejected_events"]}
    assert reasons[1] == "validation_error"
    assert reasons[2] == "validation_error"
    assert reasons[3] == "malformed"


def test_batch_too_large_is_rejected(client):
    batch = [make_event() for _ in range(501)]
    resp = client.post("/events/ingest", json=batch)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"


def test_max_batch_size_is_accepted(client):
    batch = [make_event() for _ in range(500)]
    resp = client.post("/events/ingest", json=batch)
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 500


def test_empty_batch_is_rejected(client):
    resp = client.post("/events/ingest", json=[])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "EMPTY_BATCH"


def test_invalid_json_is_rejected(client):
    resp = client.post(
        "/events/ingest",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_JSON"


def test_invalid_event_type_is_rejected(client):
    resp = client.post("/events/ingest", json=[make_event(event_type="DANCING")])
    body = resp.json()
    assert body["accepted"] == 0
    assert body["rejected"] == 1


def test_unknown_extra_field_is_rejected(client):
    """Schema is strict (extra=forbid) to catch detector bugs early."""
    resp = client.post("/events/ingest", json=[make_event(rogue="x")])
    assert resp.json()["rejected"] == 1


def test_trace_id_header_present(client):
    resp = client.post("/events/ingest", json=[make_event()])
    assert "x-trace-id" in {k.lower() for k in resp.headers}

"""Ingest a JSONL event file into POST /events/ingest.

Reads one JSON object per line, batches up to 500 events per request, and
POSTs ``{"events": [...]}`` to the Intelligence API. Prints per-batch and
running totals; exits non-zero on HTTP 4xx/5xx or connection errors.

Usage
-----
  python -m pipeline.ingest_jsonl \\
      --file data/generated_events.jsonl \\
      --url http://localhost:8000/events/ingest \\
      --batch-size 500
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib import error, request

MAX_BATCH = 500


def read_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file; skip blank lines."""
    events: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                print(
                    f"[ingest_jsonl] ERROR: invalid JSON on line {line_no}: {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(1) from exc
    return events


def post_batch(url: str, batch: list[dict]) -> dict:
    """POST one batch; raise SystemExit(1) on HTTP or network failure."""
    payload = json.dumps({"events": batch}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req) as resp:  # noqa: S310 # nosec B310 - trusted local URL
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(
            f"[ingest_jsonl] ERROR: HTTP {exc.code} from {url}: {body[:500]}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except error.URLError as exc:
        print(f"[ingest_jsonl] ERROR: request failed: {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from exc


def ingest_file(path: Path, url: str, batch_size: int = MAX_BATCH) -> dict[str, int]:
    """Read JSONL and ingest all events. Returns aggregate counts."""
    events = read_jsonl(path)
    if not events:
        print(f"[ingest_jsonl] ERROR: no events found in {path}", file=sys.stderr)
        raise SystemExit(1)

    batch_size = min(max(batch_size, 1), MAX_BATCH)
    totals = {"received": 0, "accepted": 0, "duplicates": 0, "rejected": 0}
    num_batches = (len(events) + batch_size - 1) // batch_size

    print(f"[ingest_jsonl] ingesting {len(events)} events in {num_batches} batch(es) -> {url}")

    for offset in range(0, len(events), batch_size):
        batch = events[offset : offset + batch_size]
        batch_num = offset // batch_size + 1
        result = post_batch(url, batch)
        for key in totals:
            totals[key] += int(result.get(key, 0))
        print(
            f"  batch {batch_num}/{num_batches}: "
            f"{result.get('accepted', 0)} accepted, "
            f"{result.get('duplicates', 0)} dup, "
            f"{result.get('rejected', 0)} rejected"
        )

    print(
        f"[ingest_jsonl] totals: {totals['accepted']} accepted, "
        f"{totals['duplicates']} dup, {totals['rejected']} rejected "
        f"({totals['received']} received)"
    )
    return totals


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingest JSONL events into the Intelligence API.")
    p.add_argument("--file", required=True, help="Path to a .jsonl file.")
    p.add_argument(
        "--url",
        default="http://localhost:8000/events/ingest",
        help="Full ingest endpoint URL (default: http://localhost:8000/events/ingest).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=MAX_BATCH,
        help=f"Events per request, capped at {MAX_BATCH} (default: {MAX_BATCH}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    path = Path(args.file)
    if not path.exists():
        print(f"[ingest_jsonl] ERROR: file not found: {path}", file=sys.stderr)
        return 1
    ingest_file(path, args.url, batch_size=args.batch_size)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

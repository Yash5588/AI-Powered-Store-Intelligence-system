"""Synthetic event simulator  [PHASE 3].

Generates schema-valid behavioural events for a store so the Intelligence API,
funnel, heatmap, and anomaly endpoints can be exercised without the real
detection pipeline. It deliberately produces the tricky cases the analytics must
handle: staff movement, group entry, re-entry (same visitor_id), billing-queue
join + abandonment, dwell events, and quiet zones.

Usage
-----
  # write JSONL to data/generated_events.jsonl
  python -m pipeline.simulate_events --store STORE_BLR_002 --visitors 25

  # stream straight into a running API
  python -m pipeline.simulate_events --store STORE_BLR_002 --post http://localhost:8000

The emitted schema is exactly app/models.Event — one source of truth.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ZONES = ["SKINCARE", "MAKEUP", "FRAGRANCE", "HAIRCARE"]
BILLING_ZONE = "BILLING"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    ts: datetime,
    *,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    session_seq: int = 1,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _iso(ts),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 2),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def generate_events(
    store_id: str,
    n_visitors: int = 25,
    start: datetime | None = None,
    seed: int | None = 42,
) -> tuple[list[dict], list[dict]]:
    """Generate a realistic event stream + matching POS transactions for one store.

    Returns (events, pos_transactions). A visitor who joins the billing queue
    and does NOT abandon gets a POS transaction timestamped 1-4 min AFTER their
    billing event, so the API's 5-minute conversion-correlation window matches
    naturally — conversion is emergent from the data, never hardcoded.
    """
    if seed is not None:
        random.seed(seed)
    if start is None:
        # Anchor recent so /health treats the feed as fresh.
        start = datetime.now(timezone.utc) - timedelta(minutes=45)

    events: list[dict] = []
    pos_transactions: list[dict] = []
    txn_counter = 0
    # Per-run id prefix so transaction ids never collide when appended.
    run_tag = uuid.uuid4().hex[:6]
    t = start

    # A couple of staff members roaming the floor (must be excluded from metrics).
    for i in range(2):
        vid = f"VIS_staff{i}"
        events.append(
            _event(store_id, "CAM_FLOOR_01", vid, "ZONE_ENTER", t, zone_id="MAKEUP",
                   is_staff=True, confidence=0.97, sku_zone="LIPSTICK", session_seq=1)
        )
        t += timedelta(seconds=20)

    queue_depth = 0
    for v in range(n_visitors):
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        seq = 1
        t += timedelta(seconds=random.randint(20, 90))

        # ENTRY
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "ENTRY", t,
                   confidence=random.uniform(0.8, 0.98), session_seq=seq)
        )
        seq += 1

        # Visit 1-3 product zones with dwell.
        for zone in random.sample(DEFAULT_ZONES, k=random.randint(1, 3)):
            t += timedelta(seconds=random.randint(15, 60))
            events.append(
                _event(store_id, "CAM_FLOOR_01", vid, "ZONE_ENTER", t, zone_id=zone,
                       confidence=random.uniform(0.7, 0.95), sku_zone=zone, session_seq=seq)
            )
            seq += 1
            dwell = random.randint(20000, 120000)
            t += timedelta(milliseconds=dwell)
            if dwell >= 30000:
                events.append(
                    _event(store_id, "CAM_FLOOR_01", vid, "ZONE_DWELL", t, zone_id=zone,
                           dwell_ms=dwell, confidence=random.uniform(0.7, 0.92),
                           sku_zone=zone, session_seq=seq)
                )
                seq += 1

        # ~65% proceed to billing.
        if random.random() < 0.65:
            t += timedelta(seconds=random.randint(10, 40))
            queue_depth = max(0, queue_depth + random.choice([0, 1, 1, 2]))
            events.append(
                _event(store_id, "CAM_BILLING_01", vid, "BILLING_QUEUE_JOIN", t,
                       zone_id=BILLING_ZONE, queue_depth=queue_depth,
                       confidence=random.uniform(0.85, 0.97), session_seq=seq)
            )
            seq += 1
            # ~20% of those abandon the queue.
            if random.random() < 0.2:
                t += timedelta(seconds=random.randint(30, 120))
                queue_depth = max(0, queue_depth - 1)
                events.append(
                    _event(store_id, "CAM_BILLING_01", vid, "BILLING_QUEUE_ABANDON", t,
                           zone_id=BILLING_ZONE, confidence=random.uniform(0.7, 0.9),
                           session_seq=seq)
                )
                seq += 1
            else:
                queue_depth = max(0, queue_depth - 1)
                # Completed checkout -> a POS transaction follows within minutes.
                # Timestamp it 1-4 min AFTER the billing event so the visitor's
                # billing presence falls inside the API's 5-min conversion window.
                txn_counter += 1
                txn_time = t + timedelta(minutes=random.randint(1, 4))
                pos_transactions.append(
                    {
                        "store_id": store_id,
                        "transaction_id": f"TXN_SIM_{run_tag}_{txn_counter:05d}",
                        "timestamp": _iso(txn_time),
                        "basket_value_inr": round(random.uniform(199.0, 4999.0), 2),
                    }
                )

        # EXIT
        t += timedelta(seconds=random.randint(5, 30))
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "EXIT", t,
                   confidence=random.uniform(0.8, 0.97), session_seq=seq)
        )
        seq += 1

        # ~12% re-enter (SAME visitor_id -> must NOT double-count).
        if random.random() < 0.12:
            t += timedelta(seconds=random.randint(60, 240))
            events.append(
                _event(store_id, "CAM_ENTRY_01", vid, "REENTRY", t,
                       confidence=random.uniform(0.6, 0.85), session_seq=seq)
            )

    return events, pos_transactions


def generate_events_for_pos(
    store_id: str,
    transactions: list,
    *,
    product_zones: list[str] | None = None,
    billing_zone: str = BILLING_ZONE,
    browser_ratio: float = 0.4,
    abandoner_count: int = 3,
    seed: int | None = None,
) -> list[dict]:
    """Generate events ALIGNED to real POS transactions for `store_id`.

    For every POS transaction we emit one converting visitor whose
    BILLING_QUEUE_JOIN lands 1-3 min BEFORE the transaction, so the API's
    5-minute conversion-correlation window matches it naturally — conversion
    becomes a real, computed number grounded in the official POS file (never
    hardcoded). We add browsers (enter + browse, no billing) and a few queue
    abandoners (timed in gaps, so they don't accidentally match a txn) to make
    the funnel and abandonment rate realistic.

    `transactions` is a list of objects exposing `.timestamp` (tz-aware) and
    `.transaction_id` — i.e. `app.loaders.PosTransaction`.
    """
    if seed is not None:
        random.seed(seed)
    zones = product_zones or DEFAULT_ZONES
    txns = sorted(transactions, key=lambda t: t.timestamp)
    if not txns:
        return []

    events: list[dict] = []

    def _visitor() -> str:
        return f"VIS_{uuid.uuid4().hex[:6]}"

    # 1) One converting visitor per transaction.
    for txn in txns:
        vid = _visitor()
        seq = 1
        billing_t = txn.timestamp - timedelta(minutes=random.randint(1, 3))
        entry_t = billing_t - timedelta(minutes=random.randint(4, 12))
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "ENTRY", entry_t,
                   confidence=random.uniform(0.82, 0.98), session_seq=seq)
        )
        seq += 1
        t = entry_t
        for zone in random.sample(zones, k=min(len(zones), random.randint(1, 2))):
            t += timedelta(seconds=random.randint(30, 90))
            if t >= billing_t:
                t = billing_t - timedelta(seconds=30)
            events.append(
                _event(store_id, "CAM_FLOOR_01", vid, "ZONE_ENTER", t, zone_id=zone,
                       confidence=random.uniform(0.7, 0.95), sku_zone=zone, session_seq=seq)
            )
            seq += 1
            dwell = random.randint(30000, 90000)
            events.append(
                _event(store_id, "CAM_FLOOR_01", vid, "ZONE_DWELL",
                       t + timedelta(milliseconds=dwell), zone_id=zone, dwell_ms=dwell,
                       confidence=random.uniform(0.7, 0.92), sku_zone=zone, session_seq=seq)
            )
            seq += 1
        events.append(
            _event(store_id, "CAM_BILLING_01", vid, "BILLING_QUEUE_JOIN", billing_t,
                   zone_id=billing_zone, queue_depth=random.randint(1, 4),
                   confidence=random.uniform(0.85, 0.97), session_seq=seq)
        )
        seq += 1
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "EXIT",
                   txn.timestamp + timedelta(minutes=random.randint(1, 3)),
                   confidence=random.uniform(0.8, 0.97), session_seq=seq)
        )

    # 2) Browsers: enter, visit zones, leave — never reach billing (funnel drop).
    n_browsers = int(len(txns) * browser_ratio)
    span_start, span_end = txns[0].timestamp, txns[-1].timestamp
    span_secs = max(60, int((span_end - span_start).total_seconds()))
    for _ in range(n_browsers):
        vid = _visitor()
        seq = 1
        entry_t = span_start + timedelta(seconds=random.randint(0, span_secs))
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "ENTRY", entry_t,
                   confidence=random.uniform(0.8, 0.97), session_seq=seq)
        )
        seq += 1
        t = entry_t
        for zone in random.sample(zones, k=min(len(zones), random.randint(1, 2))):
            t += timedelta(seconds=random.randint(20, 80))
            events.append(
                _event(store_id, "CAM_FLOOR_01", vid, "ZONE_ENTER", t, zone_id=zone,
                       confidence=random.uniform(0.7, 0.93), sku_zone=zone, session_seq=seq)
            )
            seq += 1
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "EXIT", t + timedelta(seconds=random.randint(20, 120)),
                   confidence=random.uniform(0.8, 0.95), session_seq=seq)
        )

    # 3) Abandoners: join the queue in a gap (>=8 min from any txn) then abandon,
    #    so they raise abandonment_rate without matching a transaction.
    txn_times = [t.timestamp for t in txns]
    for _ in range(abandoner_count):
        vid = _visitor()
        seq = 1
        # Pick a time at least 8 min away from every transaction.
        join_t = None
        for _try in range(20):
            cand = span_start + timedelta(seconds=random.randint(0, span_secs))
            if all(abs((cand - tt).total_seconds()) > 480 for tt in txn_times):
                join_t = cand
                break
        if join_t is None:
            continue
        entry_t = join_t - timedelta(minutes=random.randint(3, 8))
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "ENTRY", entry_t,
                   confidence=random.uniform(0.8, 0.97), session_seq=seq)
        )
        seq += 1
        zone = random.choice(zones)
        events.append(
            _event(store_id, "CAM_FLOOR_01", vid, "ZONE_ENTER", entry_t + timedelta(seconds=40),
                   zone_id=zone, confidence=random.uniform(0.7, 0.92), sku_zone=zone, session_seq=seq)
        )
        seq += 1
        events.append(
            _event(store_id, "CAM_BILLING_01", vid, "BILLING_QUEUE_JOIN", join_t,
                   zone_id=billing_zone, queue_depth=random.randint(5, 9),
                   confidence=random.uniform(0.85, 0.96), session_seq=seq)
        )
        seq += 1
        events.append(
            _event(store_id, "CAM_BILLING_01", vid, "BILLING_QUEUE_ABANDON",
                   join_t + timedelta(minutes=random.randint(1, 3)), zone_id=billing_zone,
                   confidence=random.uniform(0.7, 0.9), session_seq=seq)
        )
        seq += 1
        events.append(
            _event(store_id, "CAM_ENTRY_01", vid, "EXIT", join_t + timedelta(minutes=4),
                   confidence=random.uniform(0.8, 0.95), session_seq=seq)
        )

    events.sort(key=lambda e: e["timestamp"])
    return events


def write_jsonl(events: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def write_pos_csv(transactions: list[dict], path: Path, append: bool = False) -> None:
    """Write matching POS rows in the official schema (fallback file).

    When `append` is True, new rows are added to an existing file (header kept
    once) so repeated simulator runs accumulate their *own* matching POS rows
    alongside their visitors — keeping conversion consistent across runs.
    """
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["store_id", "transaction_id", "timestamp", "basket_value_inr"]
    write_header = not (append and path.exists())
    mode = "a" if append and path.exists() else "w"
    with path.open(mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if write_header:
            writer.writeheader()
        for txn in transactions:
            writer.writerow(txn)


def post_events(events: list[dict], base_url: str, batch_size: int = 500) -> None:
    """POST events to /events/ingest in batches of <=500."""
    url = base_url.rstrip("/") + "/events/ingest"
    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        data = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 # nosec B310 - local trusted URL
            body = json.loads(resp.read().decode("utf-8"))
            print(f"  batch {i // batch_size + 1}: {body['accepted']} accepted, "
                  f"{body['duplicates']} dup, {body['rejected']} rejected")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic store events.")
    parser.add_argument("--store", default="STORE_BLR_002")
    parser.add_argument("--visitors", type=int, default=25)
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Fix the RNG seed for reproducible runs. Default: random each run "
             "so repeated demo runs add fresh, non-colliding visitors + POS rows.",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "data" / "generated_events.jsonl"),
    )
    parser.add_argument("--post", default=None, help="Base URL of a running API to stream into.")
    parser.add_argument(
        "--no-pos", action="store_true",
        help="Do not (re)write the synthetic POS CSV (e.g. when using official POS data).",
    )
    parser.add_argument(
        "--reset-pos", action="store_true",
        help="Overwrite the synthetic POS CSV instead of appending this run's rows.",
    )
    parser.add_argument(
        "--align-pos", action="store_true",
        help="Generate events ALIGNED to the real POS file for --store (events "
             "timed just before each transaction) so conversion is real & non-zero. "
             "Does not write a synthetic POS file.",
    )
    args = parser.parse_args(argv)

    out_path = Path(args.out)

    if args.align_pos:
        # Real-POS-aligned mode: read transactions and emit matching events.
        from app import loaders  # local import; keeps base CLI free of app deps

        loaders.load_store_layout.cache_clear()
        transactions = loaders.load_pos_transactions(args.store)
        if not transactions:
            # Graceful fallback: never hard-fail a demo. Fall back to standard
            # synthetic generation (which also writes its own matching POS) so a
            # reviewer running the documented command always gets a live result.
            print(
                f"[align-pos] No POS transactions found for {args.store}; "
                f"falling back to synthetic generation with matching POS.",
                file=sys.stderr,
            )
            args.align_pos = False
        else:
            product_zones = [
                z for z in loaders.get_store_zones(args.store)
                if z != "ENTRY" and z not in loaders.get_billing_zone_ids(args.store)
            ]
            billing_zone = next(iter(loaders.get_billing_zone_ids(args.store)), BILLING_ZONE)
            events = generate_events_for_pos(
                args.store, transactions,
                product_zones=product_zones or None,
                billing_zone=billing_zone,
                seed=args.seed,
            )
            write_jsonl(events, out_path)
            print(f"Generated {len(events)} events ALIGNED to {len(transactions)} real "
                  f"POS transactions for {args.store} -> {out_path}")
            if args.post:
                print(f"Posting to {args.post} ...")
                post_events(events, args.post)
            return 0

    events, pos_transactions = generate_events(
        args.store, n_visitors=args.visitors, seed=args.seed
    )
    write_jsonl(events, out_path)
    print(f"Generated {len(events)} events for {args.store} -> {out_path}")

    # Write matching POS rows so conversion is non-zero for this synthetic store.
    # We write to the SYNTHETIC file; the loader layers the official POS over it
    # per store, so the real ST1008 POS still wins and demo stores get their own
    # synthetic transactions. By default we APPEND so each run keeps its own
    # matching POS rows, keeping the conversion rate consistent across re-runs.
    data_dir = out_path.parent
    if not args.no_pos:
        pos_path = data_dir / "synthetic_pos_transactions.csv"
        write_pos_csv(pos_transactions, pos_path, append=not args.reset_pos)
        verb = "Reset" if args.reset_pos else "Appended"
        print(f"{verb} {len(pos_transactions)} matching POS transactions -> {pos_path}")

    if args.post:
        print(f"Posting to {args.post} ...")
        post_events(events, args.post)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

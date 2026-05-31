"""CCTV detection pipeline  [PHASE 4].

Pipeline:  video -> OpenCV frames -> YOLO person detection -> tracking
           -> zone mapping -> behavioural events -> sink (JSONL and/or API).

Usage
-----
  python -m pipeline.detect \
      --video data/clips/store_blr_002_entry.mp4 \
      --store-id STORE_BLR_002 \
      --camera-id CAM_ENTRY_01 \
      --layout data/store_layout.json \
      --output data/generated_events.jsonl \
      [--post http://localhost:8000] [--simulate-realtime]

Design notes
------------
* OpenCV and Ultralytics are imported LAZILY (inside run_detection) so this
  module imports cleanly for unit tests and on machines without those wheels.
* The event-generation logic lives in SessionStateMachine, which is pure Python
  (no video) and therefore unit-tested directly without a clip.
* YOLO confidence is preserved on every event and low-confidence detections are
  NOT silently dropped — they're emitted with their (low) confidence, optionally
  flagged below a threshold, per the problem statement.
* Staff detection is a documented PLACEHOLDER: default is_staff=False, with an
  optional config hook for staff zones. We do not fake a staff classifier.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pipeline.emit import (
    ApiSink,
    JsonlSink,
    MultiSink,
    build_event,
    clip_start_time,
    frame_time,
)
from pipeline.tracker import CentroidTracker
from pipeline.zones import ZoneMap, load_zone_map

# COCO "person" class id in standard YOLO models.
PERSON_CLASS_ID = 0
DWELL_EMIT_INTERVAL_S = 30.0  # emit ZONE_DWELL every 30s of continued dwell
LOW_CONFIDENCE_FLAG = 0.35  # below this, detections are kept but flagged


@dataclass
class _VisitorState:
    visitor_id: str
    session_seq: int = 0
    current_zone: Optional[str] = None
    zone_enter_s: Optional[float] = None
    last_dwell_emit_s: Optional[float] = None
    entered: bool = False
    in_billing: bool = False
    joined_queue: bool = False
    exited: bool = False
    is_staff: bool = False  # sticky: once seen as staff, stays staff for this session
    history_zones: list[str] = field(default_factory=list)


class SessionStateMachine:
    """Turns per-frame (visitor, zone, confidence, time) observations into events.

    This is the heart of the pipeline and is deliberately video-free so it can be
    unit-tested. ``observe`` is called once per tracked visitor per frame; it
    returns a list of event dicts (often empty) to emit.
    """

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        zone_map: ZoneMap,
        is_staff_fn=None,
        dwell_interval_s: float = DWELL_EMIT_INTERVAL_S,
    ):
        self.store_id = store_id
        self.camera_id = camera_id
        self.zone_map = zone_map
        self.dwell_interval_s = dwell_interval_s
        self.is_staff_fn = is_staff_fn or (lambda **_: False)
        self.states: dict[str, _VisitorState] = {}
        self.entry_zones = zone_map.entry_zone_ids
        self.billing_zones = zone_map.billing_zone_ids
        self._queue_depth = 0

    def _state(self, visitor_id: str) -> _VisitorState:
        st = self.states.get(visitor_id)
        if st is None:
            st = _VisitorState(visitor_id=visitor_id)
            self.states[visitor_id] = st
        return st

    def _event(self, st: _VisitorState, etype: str, t: datetime, **kw) -> dict:
        st.session_seq += 1
        return build_event(
            store_id=self.store_id,
            camera_id=self.camera_id,
            visitor_id=st.visitor_id,
            event_type=etype,
            timestamp=t,
            session_seq=st.session_seq,
            **kw,
        )

    def observe(
        self,
        visitor_id: str,
        zone: Optional[str],
        zone_type: Optional[str],
        sku_zone: Optional[str],
        confidence: float,
        t_s: float,
        base_time: datetime,
        is_reentry: bool = False,
    ) -> list[dict]:
        """Process one observation; return events to emit."""
        st = self._state(visitor_id)
        t = base_time + timedelta(seconds=t_s)
        events: list[dict] = []
        is_staff = bool(self.is_staff_fn(zone=zone, visitor_id=visitor_id))
        # Staff status is sticky per session: a visitor classified as staff in any
        # frame stays staff for all their events, incl. the finalize() EXIT.
        st.is_staff = st.is_staff or is_staff
        is_staff = st.is_staff

        # ENTRY / REENTRY on first sight (or first sight in an entry zone).
        if not st.entered:
            st.entered = True
            etype = "REENTRY" if is_reentry else "ENTRY"
            events.append(
                self._event(st, etype, t, zone_id=None, confidence=confidence, is_staff=is_staff)
            )

        # Zone transitions.
        if zone != st.current_zone:
            # Leaving the previous (non-entry) zone.
            if st.current_zone and st.current_zone not in self.entry_zones:
                dwell_ms = self._dwell_ms(st, t_s)
                if st.current_zone in self.billing_zones and st.joined_queue:
                    # Left billing -> abandonment (POS correlation done API-side).
                    events.append(
                        self._event(
                            st, "BILLING_QUEUE_ABANDON", t,
                            zone_id=st.current_zone, confidence=confidence, is_staff=is_staff,
                        )
                    )
                    self._queue_depth = max(0, self._queue_depth - 1)
                events.append(
                    self._event(
                        st, "ZONE_EXIT", t, zone_id=st.current_zone,
                        dwell_ms=dwell_ms, confidence=confidence, is_staff=is_staff,
                    )
                )

            # Entering a new named (non-entry) zone.
            if zone and zone not in self.entry_zones:
                events.append(
                    self._event(
                        st, "ZONE_ENTER", t, zone_id=zone, sku_zone=sku_zone,
                        confidence=confidence, is_staff=is_staff,
                    )
                )
                st.zone_enter_s = t_s
                st.last_dwell_emit_s = t_s
                if zone in self.billing_zones:
                    st.in_billing = True
                    self._queue_depth += 1
                    st.joined_queue = True
                    events.append(
                        self._event(
                            st, "BILLING_QUEUE_JOIN", t, zone_id=zone,
                            queue_depth=self._queue_depth, confidence=confidence,
                            is_staff=is_staff,
                        )
                    )
                else:
                    st.in_billing = False
                st.history_zones.append(zone)

            st.current_zone = zone

        # ZONE_DWELL every dwell_interval_s of continued presence in a named zone.
        elif zone and zone not in self.entry_zones and st.zone_enter_s is not None:
            since_emit = t_s - (st.last_dwell_emit_s or st.zone_enter_s)
            if since_emit >= self.dwell_interval_s:
                dwell_ms = int((t_s - st.zone_enter_s) * 1000)
                events.append(
                    self._event(
                        st, "ZONE_DWELL", t, zone_id=zone, dwell_ms=dwell_ms,
                        sku_zone=sku_zone, confidence=confidence, is_staff=is_staff,
                    )
                )
                st.last_dwell_emit_s = t_s

        return events

    def _dwell_ms(self, st: _VisitorState, t_s: float) -> int:
        if st.zone_enter_s is None:
            return 0
        return max(0, int((t_s - st.zone_enter_s) * 1000))

    def finalize(self, t_s: float, base_time: datetime) -> list[dict]:
        """Emit EXIT for every visitor that hasn't exited (end of clip)."""
        t = base_time + timedelta(seconds=t_s)
        events: list[dict] = []
        for st in self.states.values():
            if st.entered and not st.exited:
                st.exited = True
                events.append(
                    self._event(st, "EXIT", t, zone_id=None, confidence=0.5, is_staff=st.is_staff)
                )
        return events


def make_staff_classifier(staff_zones: Optional[set[str]] = None):
    """Placeholder staff heuristic (see CHOICES.md for the documented limitation).

    Default: nobody is staff. If staff_zones are configured, a person observed in
    one of those zones is flagged is_staff=True. This is intentionally simple — we
    do NOT fabricate a uniform/appearance classifier without labelled data.
    """
    staff_zones = staff_zones or set()

    def _is_staff(zone=None, visitor_id=None) -> bool:
        return bool(zone and zone in staff_zones)

    return _is_staff


# --------------------------------------------------------------------------- #
# Video driver (lazy heavy imports)
# --------------------------------------------------------------------------- #
def run_detection(
    video_path: str,
    store_id: str,
    camera_id: str,
    layout_path: Optional[str],
    output_path: str,
    post_url: Optional[str] = None,
    simulate_realtime: bool = False,
    model_name: str = "yolov8n.pt",
    conf_threshold: float = 0.10,
    staff_zones: Optional[set[str]] = None,
    annotated_video_path: Optional[str] = None,
    latest_frame_path: Optional[str] = None,
    max_frames: Optional[int] = None,
    all_staff: bool = False,
    flush_every_frames: int = 25,
    progress_cb=None,
    visitor_prefix: Optional[str] = None,
) -> int:
    """Process a video file end-to-end and emit events. Returns event count.

    If `annotated_video_path` is set, also writes an MP4 with person boxes +
    visitor ids, the zone polygons, and pop-up labels when notable events fire.
    Annotation is purely additive — event generation is identical with or
    without it.

    The annotated writer and video capture are always released in a ``finally``
    block, including on ``KeyboardInterrupt``, so partial annotated output is
    still finalized on disk.
    """
    import time

    import cv2  # lazy: heavy, optional at import time
    from ultralytics import YOLO  # lazy

    cap = None
    writer = None
    sink = None
    sm = None
    frame_index = 0
    frames_written = 0
    interrupted = False
    fps = 15.0
    base_time = clip_start_time(video_path)

    try:
        zone_map = load_zone_map(store_id, layout_path, camera_id=camera_id)
        # Staff are excluded from visitor analytics. Sources, combined:
        #   * --staff-zone CLI hints, * layout zones typed "staff" (e.g. behind
        #   the billing counter), * --all-staff CLI flag, * a layout camera marked
        #   staff_only:true (e.g. the stockroom). Any of these can force is_staff.
        whole_camera_is_staff = bool(all_staff) or zone_map.staff_only
        effective_staff_zones = set(staff_zones or set()) | zone_map.staff_zone_ids
        if whole_camera_is_staff:
            is_staff_fn = lambda **_: True  # noqa: E731 - non-customer camera
        else:
            is_staff_fn = make_staff_classifier(effective_staff_zones)
        sm = SessionStateMachine(store_id, camera_id, zone_map, is_staff_fn=is_staff_fn)
        if zone_map.staff_only:
            print(
                f"[detect] {camera_id} is staff_only -> all events flagged is_staff=true",
                file=sys.stderr,
            )

        sinks = [JsonlSink(output_path)]
        if post_url:
            sinks.append(ApiSink(post_url, batch_size=25))
        sink = MultiSink(sinks)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"[detect] ERROR: cannot open video {video_path}", file=sys.stderr)
            return 0
        fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920.0
        height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080.0

        if annotated_video_path:
            Path(annotated_video_path).parent.mkdir(parents=True, exist_ok=True)
            fourcc = cv2.VideoWriter_fourcc(*"vp80")
            writer = cv2.VideoWriter(
                annotated_video_path, fourcc, fps, (int(width), int(height))
            )
            if not writer.isOpened():
                print(
                    f"[detect] WARN: cannot open annotated writer at {annotated_video_path}",
                    file=sys.stderr,
                )
                writer = None

        model = YOLO(model_name)
        # Namespace ids by camera so two cameras can't emit the SAME visitor_id
        # for different people (ByteTrack/centroid ids restart per stream). There
        # is no cross-camera appearance Re-ID, so each camera's ids are distinct
        # by construction — documented in DESIGN.md.
        prefix = visitor_prefix or f"{camera_id}_VIS_"
        tracker = CentroidTracker(visitor_prefix=prefix)
        use_bytetrack = True

        try:
            while True:
                if max_frames is not None and frame_index >= max_frames:
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                t_s = frame_index / fps

                # (visitor_id, conf, cx_norm, cy_norm, is_reentry, box_xyxy_or_None)
                detections: list[tuple] = []
                results = model.track(
                    frame, persist=True, classes=[PERSON_CLASS_ID],
                    conf=conf_threshold, tracker="bytetrack.yaml", verbose=False,
                )
                boxes = results[0].boxes if results else None
                if use_bytetrack and boxes is not None and boxes.id is not None:
                    bt_prefix = prefix.replace("VIS_", "bt") if "_VIS_" in prefix else f"{camera_id}_bt"
                    for i in range(len(boxes)):
                        x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                        conf = float(boxes.conf[i])
                        tid = int(boxes.id[i])
                        cx = ((x1 + x2) / 2) / width
                        cy = y2 / height  # feet point -> more stable for floor zones
                        detections.append(
                            (f"{bt_prefix}{tid:06d}", conf, cx, cy, False, (x1, y1, x2, y2))
                        )
                else:
                    centroids = []
                    confs = []
                    raw_boxes = []
                    if boxes is not None:
                        for i in range(len(boxes)):
                            x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                            centroids.append((((x1 + x2) / 2) / width, y2 / height))
                            confs.append(float(boxes.conf[i]))
                            raw_boxes.append((x1, y1, x2, y2))
                    active = tracker.update(centroids, t_s)
                    for tr, conf, rb in zip(active, confs, raw_boxes):
                        detections.append((tr.visitor_id, conf, tr.cx, tr.cy, tr.is_reentry, rb))

                frame_events: list[tuple] = []
                for visitor_id, conf, cx, cy, is_reentry, box in detections:
                    zone = zone_map.classify(cx, cy)
                    events = sm.observe(
                        visitor_id=visitor_id,
                        zone=zone.zone_id if zone else None,
                        zone_type=zone.zone_type if zone else None,
                        sku_zone=zone.sku_zone if zone else None,
                        confidence=conf,
                        t_s=t_s,
                        base_time=base_time,
                        is_reentry=is_reentry,
                    )
                    for ev in events:
                        sink.emit(ev)
                    if writer is not None or latest_frame_path:
                        frame_events.append(
                            (box, visitor_id, conf, zone.zone_id if zone else None,
                             [e["event_type"] for e in events])
                        )

                if writer is not None or latest_frame_path:
                    annotated = _annotate_frame(
                        cv2, frame, zone_map, frame_events, int(width), int(height),
                        store_id, camera_id, t_s,
                    )
                    if writer is not None:
                        writer.write(annotated)
                        frames_written += 1
                    # Save latest annotated frame as JPEG for live preview
                    if latest_frame_path and frame_index % flush_every_frames == 0:
                        try:
                            cv2.imwrite(latest_frame_path, annotated)
                        except Exception:
                            pass

                frame_index += 1
                if frame_index % 100 == 0:
                    print(
                        f"[detect] progress: frames={frame_index} "
                        f"events={sink.count} frames_written={frames_written}"
                    )
                if frame_index % flush_every_frames == 0:
                    if hasattr(sink, 'flush'):
                        sink.flush()
                    if progress_cb is not None:
                        try:
                            progress_cb(frame_index, sink.count, frames_written)
                        except Exception:  # progress reporting must never break detection
                            pass
                if simulate_realtime:
                    time.sleep(1.0 / fps)
        except KeyboardInterrupt:
            interrupted = True
            print("[detect] Interrupted, partial annotated video saved", file=sys.stderr)
    finally:
        if sm is not None and sink is not None:
            for ev in sm.finalize(frame_index / fps, base_time):
                sink.emit(ev)
            sink.close()

        if writer is not None:
            writer.release()
        if cap is not None:
            cap.release()

        if sink is not None:
            print(f"[detect] emitted {sink.count} events -> {output_path}")
        if annotated_video_path and frames_written > 0:
            print(
                f"[detect] annotated video: {frames_written} frames written -> "
                f"{annotated_video_path}"
            )
        elif annotated_video_path and writer is not None and not interrupted:
            print(f"[detect] annotated video: 0 frames written -> {annotated_video_path}")

    return sink.count if sink is not None else 0


# Event types worth flashing on the annotated video.
_NOTABLE_EVENTS = {"ENTRY", "EXIT", "ZONE_ENTER", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"}


def _annotate_frame(cv2, frame, zone_map, frame_events, width, height, store_id, camera_id, t_s):
    """Draw zone polygons, person boxes + ids, and notable event labels."""
    import numpy as np

    overlay = frame.copy()

    # 1) Zone polygons (semi-transparent fill + outline + label).
    zone_colors = {
        "threshold": (255, 180, 0),   # entry/exit — amber
        "billing": (0, 0, 255),       # billing — red
        "product": (0, 200, 0),       # product — green
        "staff": (128, 128, 128),     # staff-only (e.g. stockroom) — grey
        "other": (200, 200, 200),
    }
    for zone in zone_map.zones:
        pts = np.array(
            [[int(x * width), int(y * height)] for (x, y) in zone.polygon], np.int32
        ).reshape((-1, 1, 2))
        color = zone_colors.get(zone.zone_type, (200, 200, 200))
        cv2.fillPoly(overlay, [pts], color)
        cv2.polylines(frame, [pts], True, color, 2)
        # Zone label at the polygon's first vertex.
        lx, ly = int(zone.polygon[0][0] * width) + 4, int(zone.polygon[0][1] * height) + 18
        cv2.putText(frame, zone.zone_id, (lx, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    # Blend zone fills lightly so people remain visible.
    frame = cv2.addWeighted(overlay, 0.18, frame, 0.82, 0)

    # 2) Header (store / camera / time).
    cv2.rectangle(frame, (0, 0), (width, 28), (0, 0, 0), -1)
    cv2.putText(
        frame, f"{store_id} | {camera_id} | t={t_s:5.1f}s", (8, 20),
        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1,
    )

    # 3) Person boxes + visitor ids + notable event flashes.
    for box, visitor_id, conf, zone_id, event_types in frame_events:
        if box is None:
            continue
        x1, y1, x2, y2 = (int(v) for v in box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
        label = f"{visitor_id} {conf:.2f}"
        cv2.putText(frame, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        if zone_id:
            cv2.putText(frame, zone_id, (x1, y2 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        notable = [e for e in event_types if e in _NOTABLE_EVENTS]
        if notable:
            cv2.putText(
                frame, " ".join(notable), (x1, max(26, y1 - 22)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2,
            )
    return frame


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CCTV detection pipeline -> events.")
    p.add_argument("--video", required=True, help="Path to a CCTV clip.")
    p.add_argument("--store-id", required=True)
    p.add_argument("--camera-id", required=True)
    p.add_argument("--layout", default=None, help="store_layout.json (optional).")
    p.add_argument(
        "--output",
        default="data/generated_events.jsonl",
        help="JSONL output path.",
    )
    p.add_argument("--post", default=None, help="Base URL of API to stream into.")
    p.add_argument("--simulate-realtime", action="store_true")
    p.add_argument("--model", default="yolov8n.pt")
    p.add_argument("--conf", type=float, default=0.10)
    p.add_argument(
        "--staff-zone", action="append", default=None,
        help="Zone id(s) to treat as staff-only (placeholder heuristic).",
    )
    p.add_argument(
        "--all-staff", action="store_true",
        help="Treat EVERY person in this camera as staff (use for non-customer "
             "cameras like the stockroom/back office so they don't inflate counts).",
    )
    p.add_argument(
        "--save-annotated-video", default=None,
        help="Optional path to write an annotated MP4 (boxes, ids, zones, event labels).",
    )
    p.add_argument(
        "--max-frames", type=int, default=None,
        help="Stop after processing this many frames (quick testing).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    count = run_detection(
        video_path=args.video,
        store_id=args.store_id,
        camera_id=args.camera_id,
        layout_path=args.layout,
        output_path=args.output,
        post_url=args.post,
        simulate_realtime=args.simulate_realtime,
        model_name=args.model,
        conf_threshold=args.conf,
        staff_zones=set(args.staff_zone) if args.staff_zone else None,
        annotated_video_path=args.save_annotated_video,
        max_frames=args.max_frames,
        all_staff=args.all_staff,
    )
    return 0 if count >= 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

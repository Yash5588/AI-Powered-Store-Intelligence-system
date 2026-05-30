"""Tracking + lightweight Re-ID  [PHASE 4].

Two strategies, chosen at runtime:

1. ByteTrack (preferred) — when YOLO is run with `model.track(..., tracker=...)`
   Ultralytics returns persistent track ids. detect.py uses those directly; this
   module's CentroidTracker is the fallback when ByteTrack ids are unavailable.

2. CentroidTracker (fallback) — a dependency-free greedy nearest-centroid
   tracker. Each detection is matched to the closest existing track within a
   distance gate; unmatched detections become new tracks; tracks unseen for
   `max_missed` frames are retired.

Re-ID / re-entry: when a retired track's last position is near a newly appearing
track's first position within `reentry_seconds`, we reuse the SAME visitor_id and
mark it as a re-entry. This is an approximate, trajectory-distance Re-ID — no
appearance embedding — and its limitations are documented in CHOICES.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Track:
    track_id: int
    visitor_id: str
    cx: float  # last centroid (normalised x)
    cy: float  # last centroid (normalised y)
    first_seen_s: float
    last_seen_s: float
    missed: int = 0
    is_reentry: bool = False
    history: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class _Retired:
    visitor_id: str
    cx: float
    cy: float
    retired_at_s: float


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


class CentroidTracker:
    """Greedy centroid tracker with approximate re-entry detection.

    All coordinates are NORMALISED (0..1) so the distance gate is
    resolution-independent.
    """

    def __init__(
        self,
        max_missed: int = 30,
        max_distance: float = 0.12,
        reentry_seconds: float = 120.0,
        reentry_distance: float = 0.20,
        visitor_prefix: str = "VIS_",
    ):
        self.max_missed = max_missed
        self.max_distance = max_distance
        self.reentry_seconds = reentry_seconds
        self.reentry_distance = reentry_distance
        self.visitor_prefix = visitor_prefix

        self._next_track_id = 1
        self._visitor_counter = 0
        self.tracks: dict[int, Track] = {}
        self._retired: list[_Retired] = []

    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        # Short, stable, uuid-free token (deterministic for reproducible runs).
        return f"{self.visitor_prefix}{self._visitor_counter:06d}"

    def _maybe_reentry(self, cx: float, cy: float, t_s: float) -> Optional[str]:
        """Find a recently-retired visitor near this new appearance."""
        best: Optional[_Retired] = None
        best_d = self.reentry_distance
        for r in self._retired:
            if t_s - r.retired_at_s > self.reentry_seconds:
                continue
            d = _dist(cx, cy, r.cx, r.cy)
            if d <= best_d:
                best_d = d
                best = r
        if best is not None:
            self._retired.remove(best)
            return best.visitor_id
        return None

    def update(self, detections: list[tuple[float, float]], t_s: float) -> list[Track]:
        """Advance the tracker one frame.

        detections: list of normalised centroids (cx, cy) for this frame.
        Returns the list of currently-active tracks (post-update).
        """
        unmatched_tracks = set(self.tracks.keys())
        used_detections: set[int] = set()

        # Greedy match: for each track, take the nearest unused detection in gate.
        for tid, track in self.tracks.items():
            best_j = -1
            best_d = self.max_distance
            for j, (dx, dy) in enumerate(detections):
                if j in used_detections:
                    continue
                d = _dist(track.cx, track.cy, dx, dy)
                if d <= best_d:
                    best_d = d
                    best_j = j
            if best_j >= 0:
                dx, dy = detections[best_j]
                track.cx, track.cy = dx, dy
                track.last_seen_s = t_s
                track.missed = 0
                track.history.append((dx, dy))
                used_detections.add(best_j)
                unmatched_tracks.discard(tid)

        # Unmatched existing tracks: increment miss, retire if stale.
        for tid in list(unmatched_tracks):
            track = self.tracks[tid]
            track.missed += 1
            if track.missed > self.max_missed:
                self._retired.append(
                    _Retired(track.visitor_id, track.cx, track.cy, t_s)
                )
                del self.tracks[tid]

        # New detections -> new tracks (possibly re-entries).
        for j, (dx, dy) in enumerate(detections):
            if j in used_detections:
                continue
            reused = self._maybe_reentry(dx, dy, t_s)
            visitor_id = reused or self._new_visitor_id()
            tid = self._next_track_id
            self._next_track_id += 1
            self.tracks[tid] = Track(
                track_id=tid,
                visitor_id=visitor_id,
                cx=dx,
                cy=dy,
                first_seen_s=t_s,
                last_seen_s=t_s,
                is_reentry=reused is not None,
                history=[(dx, dy)],
            )

        return list(self.tracks.values())

    def finalize(self, t_s: float) -> None:
        """Retire all remaining tracks at end of stream (for EXIT emission)."""
        for track in self.tracks.values():
            self._retired.append(_Retired(track.visitor_id, track.cx, track.cy, t_s))
        self.tracks.clear()

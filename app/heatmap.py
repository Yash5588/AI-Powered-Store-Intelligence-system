"""Zone heatmap  [PHASE 2].

GET /stores/{id}/heatmap -> per-zone visit frequency + average dwell, with a
normalized 0-100 score ready for grid rendering. Sets data_confidence=false when
fewer than MIN_SESSIONS_FOR_CONFIDENCE (default 20) customer sessions are in the
window, so a sparse window isn't mistaken for ground truth.

Normalisation blends visit frequency and average dwell (50/50) so a zone that is
visited often AND held attention scores highest. The busiest/longest zone maps
to 100; everything else is relative to it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import HeatmapCell, HeatmapResponse
from app.sessions import (
    MIN_SESSIONS_FOR_CONFIDENCE,
    customer_sessions,
    load_store_context,
)


def compute_heatmap(db: Session, store_id: str) -> HeatmapResponse:
    """Build the per-zone heatmap for a store."""
    window, _events, all_sessions, billing_zones = load_store_context(db, store_id)
    sessions = customer_sessions(all_sessions)
    session_count = len(sessions)

    if session_count == 0:
        return HeatmapResponse(
            store_id=store_id,
            window_start=window.start,
            window_end=window.end,
            session_count=0,
            data_confidence=False,
            cells=[],
        )

    # Aggregate per zone across customer sessions.
    freq: dict[str, int] = {}
    dwell_sum: dict[str, int] = {}
    dwell_n: dict[str, int] = {}
    for s in sessions:
        for zone in s.visited_zones:
            freq[zone] = freq.get(zone, 0) + 1
        for zone, dwell in s.zone_dwell_ms.items():
            dwell_sum[zone] = dwell_sum.get(zone, 0) + dwell
            dwell_n[zone] = dwell_n.get(zone, 0) + 1

    if not freq:
        return HeatmapResponse(
            store_id=store_id,
            window_start=window.start,
            window_end=window.end,
            session_count=session_count,
            data_confidence=session_count >= MIN_SESSIONS_FOR_CONFIDENCE,
            cells=[],
        )

    avg_dwell = {z: (dwell_sum[z] / dwell_n[z]) if dwell_n.get(z) else 0.0 for z in freq}
    max_freq = max(freq.values()) or 1
    max_dwell = max(avg_dwell.values()) or 1.0

    cells: list[HeatmapCell] = []
    for zone in sorted(freq.keys()):
        freq_norm = freq[zone] / max_freq
        dwell_norm = (avg_dwell[zone] / max_dwell) if max_dwell else 0.0
        score = round((0.5 * freq_norm + 0.5 * dwell_norm) * 100.0, 2)
        cells.append(
            HeatmapCell(
                zone_id=zone,
                visit_frequency=freq[zone],
                avg_dwell_ms=round(avg_dwell[zone], 2),
                avg_dwell_seconds=round(avg_dwell[zone] / 1000.0, 2),
                normalized_score=score,
            )
        )

    # Highest score first — most-attention zones at the top.
    cells.sort(key=lambda c: c.normalized_score, reverse=True)

    return HeatmapResponse(
        store_id=store_id,
        window_start=window.start,
        window_end=window.end,
        session_count=session_count,
        data_confidence=session_count >= MIN_SESSIONS_FOR_CONFIDENCE,
        cells=cells,
    )

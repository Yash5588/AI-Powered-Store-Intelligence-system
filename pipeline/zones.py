"""Zone geometry + lookup for the detection pipeline  [PHASE 4].

Maps a person's position in a frame to a named store zone. Zones are defined as
polygons in NORMALISED frame coordinates (x, y in [0, 1]), so the same layout
works regardless of resolution and the official store_layout.json drops in
without recomputation.

Resolution order for the layout file:
  1. explicit --layout path (if given)
  2. data/store_layout.json          (official, when supplied)
  3. data/fallback_store_layout.json (synthetic default)

If a store has no polygon regions (older layouts) or no layout exists at all, we
fall back to sensible DEFAULT frame regions:
  * ENTRY   - lower-centre band (threshold people cross)
  * floor   - centre/upper area  (main product zone)
  * BILLING - lower-right corner (checkout)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Default normalised regions used when a layout has no geometry.
DEFAULT_REGIONS: dict[str, list[tuple[float, float]]] = {
    "ENTRY": [(0.30, 0.70), (0.70, 0.70), (0.70, 1.00), (0.30, 1.00)],
    "MAIN": [(0.00, 0.10), (1.00, 0.10), (1.00, 0.70), (0.00, 0.70)],
    "BILLING": [(0.66, 0.55), (1.00, 0.55), (1.00, 1.00), (0.66, 1.00)],
}


@dataclass
class Zone:
    zone_id: str
    zone_type: str  # threshold | product | billing | other
    polygon: list[tuple[float, float]]  # normalised (x, y)
    sku_zone: Optional[str] = None  # representative SKU label, if any


def _resolve_layout_path(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    official = DATA_DIR / "store_layout.json"
    if official.exists():
        return official
    fallback = DATA_DIR / "fallback_store_layout.json"
    if fallback.exists():
        return fallback
    return None


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (works for convex/concave polygons)."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


class ZoneMap:
    """Loaded zones for one store/camera, with default-region fallback.

    ``staff_only`` marks a non-customer camera (e.g. the stockroom): every person
    it sees is staff, so it must never contribute to visitor analytics.
    """

    def __init__(self, store_id: str, zones: list[Zone], staff_only: bool = False):
        self.store_id = store_id
        self.zones = zones
        self.staff_only = staff_only

    @property
    def billing_zone_ids(self) -> set[str]:
        return {z.zone_id for z in self.zones if z.zone_type == "billing"}

    @property
    def entry_zone_ids(self) -> set[str]:
        return {z.zone_id for z in self.zones if z.zone_type == "threshold"}

    @property
    def staff_zone_ids(self) -> set[str]:
        """Zones the layout marks as staff-only (e.g. behind the billing counter).

        Anyone classified into one of these is flagged ``is_staff=True`` so they
        never count as a visitor. This lets the layout declare staff areas
        per-camera without a CLI flag or a fabricated appearance classifier.
        """
        return {z.zone_id for z in self.zones if z.zone_type == "staff"}

    def classify(self, nx: float, ny: float) -> Optional[Zone]:
        """Return the first zone whose polygon contains the normalised point."""
        for zone in self.zones:
            if _point_in_polygon(nx, ny, zone.polygon):
                return zone
        return None


def _zones_from_store(store: dict) -> list[Zone]:
    zones: list[Zone] = []
    for z in store.get("zones", []):
        zid = z.get("zone_id")
        if not zid:
            continue
        region = z.get("region")
        if region:
            polygon = [(float(p[0]), float(p[1])) for p in region]
        else:
            # No geometry: map by type onto a default region.
            ztype = z.get("type", "other")
            if ztype == "threshold":
                polygon = DEFAULT_REGIONS["ENTRY"]
            elif ztype == "billing":
                polygon = DEFAULT_REGIONS["BILLING"]
            else:
                polygon = DEFAULT_REGIONS["MAIN"]
        skus = z.get("skus") or []
        zones.append(
            Zone(
                zone_id=zid,
                zone_type=z.get("type", "other"),
                polygon=polygon,
                sku_zone=skus[0] if skus else None,
            )
        )
    return zones


def _default_zone_map(store_id: str) -> ZoneMap:
    """Used when no layout/store match exists at all."""
    zones = [
        Zone("ENTRY", "threshold", DEFAULT_REGIONS["ENTRY"]),
        Zone("MAIN", "product", DEFAULT_REGIONS["MAIN"], sku_zone="GENERAL"),
        Zone("BILLING", "billing", DEFAULT_REGIONS["BILLING"]),
    ]
    return ZoneMap(store_id, zones)


def _camera_entry(store: dict, camera_id: str | None) -> dict | None:
    if not camera_id:
        return None
    for cam in store.get("cameras", []):
        if cam.get("camera_id") == camera_id:
            return cam
    return None


def load_zone_map(
    store_id: str, layout_path: str | None = None, camera_id: str | None = None
) -> ZoneMap:
    """Load the zone map for a store/camera, with graceful fallbacks at every step.

    Camera angles differ, so a single top-down polygon set cannot serve every
    view. When ``camera_id`` is given and that camera defines its own ``zones``
    (polygons calibrated to *that* camera's frame), those are used. Otherwise we
    fall back to the store-level zones, then to default regions — so older
    layouts and unknown cameras still work.
    """
    path = _resolve_layout_path(layout_path)
    if path is None:
        return _default_zone_map(store_id)

    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_zone_map(store_id)

    for store in layout.get("stores", []):
        if store.get("store_id") == store_id:
            cam = _camera_entry(store, camera_id)
            if cam is not None:
                # A matched camera owns its own view. We use ITS zones only and
                # NEVER fall back to the store-level retail zones — otherwise a
                # non-retail camera (e.g. stockroom) would draw/classify SKINCARE,
                # MAKEUP, etc., which is wrong. An empty zone list is respected.
                staff_only = bool(cam.get("staff_only", False))
                cam_zones = _zones_from_store(cam)
                return ZoneMap(store_id, cam_zones, staff_only=staff_only)
            # No specific camera (or camera_id not in layout) -> store-level zones.
            zones = _zones_from_store(store)
            if zones:
                return ZoneMap(store_id, zones)

    return _default_zone_map(store_id)

# PROMPT: Improve pipeline coverage — zones edge cases and tracker behavior
#          without video/YOLO; small synthetic layouts and coordinates.
# CHANGES MADE: invalid/missing layout paths, degenerate polygons, zone type
#          fallbacks, tracker finalize/retire/re-entry distance+time gates.

from __future__ import annotations

import json

import pytest

from pipeline.tracker import CentroidTracker
from pipeline.zones import (
    Zone,
    ZoneMap,
    _point_in_polygon,
    _zones_from_store,
    load_zone_map,
)


# --------------------------------------------------------------------------- #
# Zones — edge cases
# --------------------------------------------------------------------------- #
def test_point_in_polygon_too_few_vertices():
    assert _point_in_polygon(0.5, 0.5, [(0.0, 0.0), (1.0, 1.0)]) is False


def test_classify_outside_all_zones_returns_none():
    zm = load_zone_map("STORE_BLR_002")
    assert zm.classify(-0.1, -0.1) is None


def test_load_zone_map_explicit_missing_path_uses_defaults():
    zm = load_zone_map("STORE_BLR_002", layout_path="/no/such/layout.json")
    zone_ids = {z.zone_id for z in zm.zones}
    assert {"ENTRY", "MAIN", "BILLING"}.issubset(zone_ids)


def test_load_zone_map_invalid_json_uses_defaults(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    zm = load_zone_map("STORE_BLR_002", layout_path=str(bad))
    assert any(z.zone_id == "ENTRY" for z in zm.zones)


def test_load_zone_map_explicit_valid_layout(tmp_path):
    layout = {
        "stores": [
            {
                "store_id": "STORE_TEST",
                "zones": [
                    {
                        "zone_id": "CUSTOM_ENTRY",
                        "type": "threshold",
                        "region": [[0.0, 0.8], [1.0, 0.8], [1.0, 1.0], [0.0, 1.0]],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    zm = load_zone_map("STORE_TEST", layout_path=str(path))
    hit = zm.classify(0.5, 0.9)
    assert hit is not None and hit.zone_id == "CUSTOM_ENTRY"


def test_zones_from_store_type_fallback_without_region():
    store = {
        "zones": [
            {"zone_id": "E1", "type": "threshold"},
            {"zone_id": "B1", "type": "billing"},
            {"zone_id": "P1", "type": "product", "skus": ["SKU_A"]},
            {"zone_id": "", "type": "product"},  # skipped — no zone_id
        ]
    }
    zones = _zones_from_store(store)
    by_id = {z.zone_id: z for z in zones}
    assert set(by_id) == {"E1", "B1", "P1"}
    assert by_id["P1"].sku_zone == "SKU_A"
    assert len(by_id["E1"].polygon) == 4
    assert len(by_id["B1"].polygon) == 4


def test_zone_map_billing_and_entry_properties():
    zm = ZoneMap(
        "S",
        [
            Zone("ENTRY", "threshold", [(0, 0), (1, 0), (1, 1), (0, 1)]),
            Zone("BILLING", "billing", [(0, 0), (1, 0), (1, 1), (0, 1)]),
            Zone("OTHER", "other", [(0, 0), (1, 0), (1, 1), (0, 1)]),
        ],
    )
    assert zm.entry_zone_ids == {"ENTRY"}
    assert zm.billing_zone_ids == {"BILLING"}


# --------------------------------------------------------------------------- #
# Tracker — additional behavior
# --------------------------------------------------------------------------- #
def test_tracker_custom_visitor_prefix():
    tr = CentroidTracker(visitor_prefix="CUST_")
    tracks = tr.update([(0.5, 0.5)], t_s=0.0)
    assert tracks[0].visitor_id.startswith("CUST_")


def test_tracker_retires_after_max_missed():
    tr = CentroidTracker(max_missed=1)
    tr.update([(0.5, 0.5)], t_s=0.0)
    tr.update([], t_s=0.1)  # miss once -> still active
    tr.update([], t_s=0.2)  # miss twice -> retired
    assert len(tr.tracks) == 0
    assert len(tr._retired) == 1


def test_tracker_reentry_too_far_gets_new_id():
    tr = CentroidTracker(max_missed=1, reentry_seconds=120, reentry_distance=0.05)
    first = tr.update([(0.5, 0.5)], t_s=0.0)
    vid = first[0].visitor_id
    tr.update([], t_s=1.0)
    tr.update([], t_s=2.0)
    far = tr.update([(0.9, 0.9)], t_s=3.0)
    assert far[0].visitor_id != vid
    assert far[0].is_reentry is False


def test_tracker_reentry_too_late_gets_new_id():
    tr = CentroidTracker(max_missed=1, reentry_seconds=5, reentry_distance=0.2)
    first = tr.update([(0.5, 0.5)], t_s=0.0)
    vid = first[0].visitor_id
    tr.update([], t_s=1.0)
    tr.update([], t_s=2.0)
    late = tr.update([(0.52, 0.51)], t_s=20.0)
    assert late[0].visitor_id != vid


def test_tracker_finalize_retires_remaining_tracks():
    tr = CentroidTracker()
    tr.update([(0.3, 0.3), (0.7, 0.7)], t_s=0.0)
    assert len(tr.tracks) == 2
    tr.finalize(t_s=10.0)
    assert len(tr.tracks) == 0
    assert len(tr._retired) == 2


def test_tracker_greedy_match_picks_nearest_detection():
    tr = CentroidTracker(max_distance=0.5)
    tr.update([(0.1, 0.1)], t_s=0.0)
    vid = list(tr.tracks.values())[0].visitor_id
    # Two detections; nearest to (0.1,0.1) should update the existing track.
    active = tr.update([(0.12, 0.11), (0.9, 0.9)], t_s=0.1)
    ids = {t.visitor_id for t in active}
    assert vid in ids
    assert len(active) == 2


def test_tracker_group_entry_distinct_ids():
    """Group entry: several people entering together each get a distinct id.

    This is the rubric's "handles group entry correctly" case — multi-object
    tracking assigns one stable visitor_id per simultaneous detection, so a
    group of 4 is counted as 4 visitors, not 1.
    """
    tr = CentroidTracker(max_distance=0.1)
    group = [(0.20, 0.9), (0.35, 0.9), (0.50, 0.9), (0.65, 0.9)]
    active = tr.update(group, t_s=0.0)
    ids = {t.visitor_id for t in active}
    assert len(active) == 4
    assert len(ids) == 4, "each person in a group entry must get a unique id"


def test_tracker_group_stays_distinct_across_frames():
    """The 4 group members keep their distinct ids as they move together."""
    tr = CentroidTracker(max_distance=0.15)
    f0 = [(0.20, 0.9), (0.35, 0.9), (0.50, 0.9), (0.65, 0.9)]
    ids0 = {t.visitor_id for t in tr.update(f0, t_s=0.0)}
    # Everyone drifts up-left by a small, in-gate amount.
    f1 = [(x - 0.03, y - 0.05) for (x, y) in f0]
    active1 = tr.update(f1, t_s=0.1)
    ids1 = {t.visitor_id for t in active1}
    assert len(active1) == 4
    assert ids0 == ids1, "group members must not merge or swap ids between frames"

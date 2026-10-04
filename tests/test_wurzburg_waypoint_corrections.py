"""User-approved integer arrival anchors; upstream annotations stay immutable."""

import hashlib
import json
import unittest
from decimal import ROUND_HALF_UP, Decimal

from eval.navigation.schema import MAPS_ROOT, load_tasks, load_waypoints, resolved_task


CANDIDATES = {
    "WUR-WP-01": ("-45.2", "-13", "145.3"),
    "WUR-WP-02": ("32.5", "-13", "176.9"),
    "WUR-WP-13": ("-211.8", "-13", "522.2"),
    "WUR-WP-44": ("483.5", "-3", "492.3"),
    "WUR-WP-69": ("498.4", "-1", "643.5"),
    "WUR-WP-59": ("693.0", "-6", "486.7"),
    "WUR-WP-77": ("-218.1", "-13", "456.7"),
}
EXPECTED = {
    "WUR-WP-01": {"x": -45, "y": -13, "z": 145},
    "WUR-WP-02": {"x": 33, "y": -13, "z": 177},
    "WUR-WP-13": {"x": -212, "y": -13, "z": 522},
    "WUR-WP-44": {"x": 484, "y": -3, "z": 492},
    "WUR-WP-69": {"x": 498, "y": -1, "z": 644},
    "WUR-WP-59": {"x": 693, "y": -6, "z": 487},
    "WUR-WP-77": {"x": -218, "y": -13, "z": 457},
}


class WurzburgWaypointCorrectionsTest(unittest.TestCase):
    def test_approved_candidates_are_rounded_to_integers(self):
        waypoints = load_waypoints("wurzburg")
        for waypoint_id, candidate in CANDIDATES.items():
            with self.subTest(waypoint_id=waypoint_id):
                rounded = {
                    axis: int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    for axis, value in zip(("x", "y", "z"), candidate)
                }
                self.assertEqual(rounded, EXPECTED[waypoint_id])
                self.assertEqual(waypoints[waypoint_id]["position"], rounded)
                self.assertTrue(all(
                    type(value) is int
                    for value in waypoints[waypoint_id]["position"].values()
                ))

    def test_original_xaero_annotation_and_correction_provenance_are_preserved(self):
        directory = MAPS_ROOT / "wurzburg"
        payload = json.loads((directory / "waypoints.json").read_text())
        original_bytes = (directory / "waypoints.xaero.txt").read_bytes()
        self.assertEqual(
            hashlib.sha256(original_bytes).hexdigest(),
            payload["annotation_source"]["sha256"],
        )
        originals = {}
        for line in original_bytes.decode("utf-8").splitlines():
            if line.startswith("waypoint:"):
                fields = line.split(":")
                originals[fields[1]] = dict(zip(("x", "y", "z"), map(int, fields[3:6])))
        corrections = payload["local_position_corrections"]
        self.assertEqual(corrections["rounding"], "nearest_integer_half_away_from_zero")
        self.assertEqual(set(corrections["original_positions"]), set(EXPECTED))
        waypoints = load_waypoints("wurzburg")
        for waypoint_id in EXPECTED:
            self.assertEqual(
                corrections["original_positions"][waypoint_id],
                originals[waypoints[waypoint_id]["name"]],
            )

    def test_all_six_affected_tasks_resolve_the_new_anchors(self):
        affected = {}
        for task in load_tasks("wurzburg"):
            resolved = resolved_task("wurzburg", task)
            endpoints = [resolved["start"], *resolved["required_waypoints"], resolved["target"]]
            for endpoint in endpoints:
                waypoint_id = endpoint["waypoint_id"]
                if waypoint_id in EXPECTED:
                    self.assertEqual(endpoint["position"], EXPECTED[waypoint_id])
                    affected.setdefault(task["id"], []).append(waypoint_id)
        self.assertEqual(affected, {
            "wurzburg-001": ["WUR-WP-01", "WUR-WP-77", "WUR-WP-13"],
            "wurzburg-002": ["WUR-WP-02"],
            "wurzburg-004": ["WUR-WP-44"],
            "wurzburg-007": ["WUR-WP-69"],
            "wurzburg-009": ["WUR-WP-59"],
            "wurzburg-010": ["WUR-WP-69"],
        })

    def test_other_waypoints_on_the_same_route_are_unchanged(self):
        waypoints = load_waypoints("wurzburg")
        self.assertEqual(waypoints["WUR-WP-11"]["position"], {"x": -103, "y": -15, "z": 267})
        self.assertEqual(waypoints["WUR-WP-12"]["position"], {"x": -113, "y": -12, "z": 362})
        self.assertEqual(waypoints["WUR-WP-23"]["position"], {"x": -123, "y": -13, "z": 392})


if __name__ == "__main__":
    unittest.main()

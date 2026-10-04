"""Approved integer anchors preserve raw annotations and affect three tasks."""
import hashlib
import json
import unittest
from decimal import Decimal, ROUND_HALF_UP

from eval.navigation.schema import MAPS_ROOT, load_tasks, load_waypoints, resolved_task


class AdditionalWaypointCorrectionsTest(unittest.TestCase):
    def test_rounding_provenance_and_resolved_references(self):
        cases = [
            ("innopolis", "INN-WP-14", {"x": 6287502, "y": 197, "z": -5355648},
             ["innopolis-006", "innopolis-008"]),
            ("mr-beast-1000-harbor-city", "HBC-WP-45",
             {"x": -8523480, "y": 30, "z": -6020985},
             ["mr-beast-1000-harbor-city-002"]),
        ]
        for map_id, waypoint_id, expected, task_ids in cases:
            with self.subTest(map_id=map_id):
                directory = MAPS_ROOT / map_id
                payload = json.loads((directory / "waypoints.json").read_text())
                raw = (directory / "waypoints.xaero.txt").read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),
                                 payload["annotation_source"]["sha256"])
                correction = payload["local_position_corrections"]
                rounded = {axis: int(Decimal(str(value)).quantize(Decimal("1"),
                           rounding=ROUND_HALF_UP)) for axis, value in
                           correction["suggested_positions"][waypoint_id].items()}
                self.assertEqual(rounded, expected)
                self.assertEqual(load_waypoints(map_id)[waypoint_id]["position"], expected)
                affected = []
                for task in load_tasks(map_id):
                    resolved = resolved_task(map_id, task)
                    endpoints = [resolved["start"], *resolved["required_waypoints"],
                                 resolved["target"]]
                    matches = [p for p in endpoints if p["waypoint_id"] == waypoint_id]
                    if matches:
                        affected.append(task["id"])
                    for endpoint in matches:
                        self.assertEqual(endpoint["position"], expected)
                self.assertEqual(affected, task_ids)


if __name__ == "__main__":
    unittest.main()

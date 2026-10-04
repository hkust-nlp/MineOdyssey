from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "eval" / "set_ground_navigation_route.py"
SPEC = importlib.util.spec_from_file_location("set_ground_navigation_route", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
ground_navigation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ground_navigation)


class GroundNavigationRouteTest(unittest.TestCase):
    def test_materialized_run_omits_start_and_preserves_route_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_path = Path(tmp) / "run.json"
            run_path.write_text(
                json.dumps(
                    {
                        "task": {
                            "start": {
                                "waypoint_id": "START",
                                "position": {"x": 1, "y": 2, "z": 3},
                            },
                            "required_waypoints": [
                                {
                                    "waypoint_id": "CHECK",
                                    "position": {"x": 4, "y": 5, "z": 6},
                                }
                            ],
                            "target": {
                                "waypoint_id": "TARGET",
                                "position": {"x": 7, "y": 8, "z": 9},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                ground_navigation.run_points(run_path),
                [
                    {"id": "CHECK", "x": 4, "y": 5, "z": 6},
                    {"id": "TARGET", "x": 7, "y": 8, "z": 9},
                ],
            )

    def test_unified_task_omits_start_and_resolves_remaining_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = root / "tasks.json"
            waypoints = root / "waypoints.json"
            tasks.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "tasks": [
                            {
                                "task_id": "map-a-001",
                                "map_id": "map-a",
                                "waypoints": [
                                    {"id": "WP-START"},
                                    {"id": "WP-CHECKPOINT"},
                                    {"id": "WP-TARGET"},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            waypoints.write_text(
                json.dumps(
                    {
                        "waypoints": [
                            {"id": "WP-START", "position": {"x": 1, "y": 2, "z": 3}},
                            {"id": "WP-CHECKPOINT", "position": {"x": 4, "y": 5, "z": 6}},
                            {"id": "WP-TARGET", "position": {"x": 7, "y": 8, "z": 9}},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            points = ground_navigation.task_points(
                tasks,
                waypoints,
                "map-a-001",
                expected_map_id="map-a",
            )

            self.assertEqual(
                points,
                [
                    {"id": "WP-CHECKPOINT", "x": 4, "y": 5, "z": 6},
                    {"id": "WP-TARGET", "x": 7, "y": 8, "z": 9},
                ],
            )

    def test_map_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = root / "tasks.json"
            waypoints = root / "waypoints.json"
            tasks.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "map-a-001",
                                "map_id": "map-a",
                                "waypoints": ["WP-1", "WP-2"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            waypoints.write_text(
                json.dumps(
                    {
                        "waypoints": [
                            {"id": "WP-1", "position": {"x": 0, "y": 0, "z": 0}},
                            {"id": "WP-2", "position": {"x": 1, "y": 0, "z": 0}},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "belongs to map"):
                ground_navigation.task_points(
                    tasks,
                    waypoints,
                    "map-a-001",
                    expected_map_id="map-b",
                )

    def test_status_requires_guide_only_and_locked_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "request_id": "request-1",
                        "state": "planning",
                        "guide_only": True,
                        "baritone_controls_locked": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "did not lock"):
                ground_navigation.wait_for_status(status_path, "request-1", 0.1)


if __name__ == "__main__":
    unittest.main()

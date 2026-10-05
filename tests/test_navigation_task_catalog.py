import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

from eval.navigation.schema import SchemaError, find_task, load_benchmark, resolved_task


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = REPO_ROOT / "eval/navigation/tasks.json"
MAPS_ROOT = REPO_ROOT / "eval/navigation/maps"


class NavigationTaskCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        cls.tasks = cls.document["tasks"]

    def test_catalog_has_complete_prompt_portfolio(self):
        self.assertEqual(self.document["schema_version"], 2)
        self.assertEqual(len(self.tasks), 180)
        self.assertEqual(len({task["map_id"] for task in self.tasks}), 30)
        self.assertEqual(len({task["task_id"] for task in self.tasks}), 180)

        counts = Counter(task["map_id"] for task in self.tasks)
        self.assertEqual(counts["shun-lee"], 4)
        self.assertEqual(counts["innopolis"], 9)
        self.assertEqual(counts["wurzburg"], 10)
        self.assertEqual(counts["mr-beast-1000-harbor-city"], 11)
        self.assertEqual(counts["buckingham-palace"], 5)
        self.assertEqual(counts["hagia-sophia"], 3)
        self.assertEqual(counts["notre-dame"], 4)
        self.assertEqual(counts["plaza-hotel"], 6)
        self.assertEqual(counts["reichstag"], 5)
        self.assertEqual(counts["rms-queen-mary"], 10)
        self.assertEqual(counts["rms-titanic"], 6)
        self.assertEqual(counts["sofi-stadium"], 6)
        self.assertEqual(counts["versailles"], 3)
        self.assertEqual(counts["white-house"], 7)

        buckingham_routes = {
            task["metadata"]["source_route_id"]
            for task in self.tasks
            if task["map_id"] == "buckingham-palace"
        }
        self.assertEqual(
            buckingham_routes,
            {
                "bph-route-04",
                "bph-route-07",
                "bph-route-09",
                "bph-route-11",
                "bph-route-12",
            },
        )

        white_house_routes = {
            task["metadata"]["source_route_id"]
            for task in self.tasks
            if task["map_id"] == "white-house"
        }
        self.assertEqual(
            white_house_routes,
            {
                "whi-route-08",
                "whi-route-09",
                "whi-route-10",
                "whi-route-11",
                "whi-route-12",
                "whi-route-14",
                "whi-route-15",
            },
        )

        indoor_route_sets = {
            "hagia-sophia": {
                "hag-route-05", "hag-route-07", "hag-route-10",
            },
            "notre-dame": {
                "ntd-route-08-09",
                "ntd-route-11", "ntd-route-12", "ntd-route-13",
            },
            "plaza-hotel": {
                "plh-route-01", "plh-route-04", "plh-route-06",
                "plh-route-07", "plh-route-11", "plh-route-14",
            },
            "reichstag": {
                "rei-route-07", "rei-route-10", "rei-route-11",
                "rei-route-13", "rei-route-14",
            },
            "rms-queen-mary": {
                "qmr-route-01", "qmr-route-04", "qmr-route-07",
                "qmr-route-13", "qmr-route-14", "qmr-route-15",
                "qmr-route-16", "qmr-route-17", "qmr-route-18",
                "qmr-route-19",
            },
            "rms-titanic": {
                "tit-route-02", "tit-route-08",
                "tit-route-10", "tit-route-11", "tit-route-12",
                "tit-route-14",
            },
            "sofi-stadium": {
                "sofi-route-02", "sofi-route-05", "sofi-route-08",
                "sofi-route-13", "sofi-route-14", "sofi-route-15",
            },
            "versailles": {
                "ver-route-06-07", "ver-route-08-09",
                "ver-route-13",
            },
        }
        for map_id, expected_routes in indoor_route_sets.items():
            with self.subTest(map_id=map_id):
                actual_routes = {
                    task["metadata"]["source_route_id"]
                    for task in self.tasks
                    if task["map_id"] == map_id
                }
                self.assertEqual(actual_routes, expected_routes)

        task_by_route = {
            task["metadata"]["source_route_id"]: task
            for task in self.tasks
            if task["map_id"] in indoor_route_sets
        }
        self.assertEqual(
            [point["id"] for point in task_by_route["ntd-route-08-09"]["waypoints"]],
            [
                "NTD-WP-14", "NTD-WP-06", "NTD-WP-05", "NTD-WP-04",
                "NTD-WP-03", "NTD-WP-02", "NTD-WP-07", "NTD-WP-09",
                "NTD-WP-10", "NTD-WP-11", "NTD-WP-12", "NTD-WP-13",
            ],
        )
        self.assertEqual(
            task_by_route["sofi-route-15"]["waypoints"][-1]["id"],
            "SOFI-WP-08",
        )
        self.assertEqual(
            [point["id"] for point in task_by_route["ver-route-06-07"]["waypoints"]],
            [
                "VER-WP-07", "VER-WP-17", "VER-WP-10", "VER-WP-16",
                "VER-WP-18", "VER-WP-19",
            ],
        )
        self.assertEqual(
            [point["id"] for point in task_by_route["ver-route-08-09"]["waypoints"]],
            [
                "VER-WP-02", "VER-WP-01", "VER-WP-08", "VER-WP-05",
                "VER-WP-04", "VER-WP-03",
            ],
        )

        benchmark = load_benchmark("finalpool-navigation-v1")
        self.assertEqual(
            benchmark["task_admission"],
            {
                "policy": "benchmark_catalog_owner_approved",
                "reference_required": False,
                "validation_receipt_required": False,
            },
        )

    def test_catalog_matches_frozen_main_benchmark_ids(self):
        # Independent frozen main-benchmark inventory, not a count-only assertion.
        ids = sorted(task["task_id"] for task in self.tasks)
        digest = hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()
        self.assertEqual(
            digest,
            "d29ee013ba6cd62278d06c79351d4c13eb59559079f3e14d7561f2ef035c93d9",
        )

    def test_retired_tasks_cannot_be_selected(self):
        benchmark = load_benchmark("finalpool-navigation-v1")
        for task_id in (
            "buckingham-palace-001",
            "copacabana-waterfront-003",
            "copacabana-waterfront-005",
            "miljacka-riverside-003",
            "notre-dame-001",
            "notre-dame-003",
            "nyc-911-memorials-006",
            "rms-titanic-001",
            "shun-lee-003",
            "torrey-mall-005",
            "torrey-mall-007",
            "versailles-003",
            "white-house-001",
            "white-house-002",
        ):
            with self.subTest(task_id=task_id), self.assertRaises(SchemaError):
                find_task(task_id, benchmark["maps"])

    def test_every_task_has_local_prompt_and_chinese_translation(self):
        for task in self.tasks:
            with self.subTest(task_id=task["task_id"]):
                self.assertNotEqual(task["prompt"], "default")
                self.assertTrue(task["prompt"].strip())
                self.assertEqual(task["eval_setting"], {})
                metadata = task["metadata"]
                for key in (
                    "source_route_id",
                    "title_local",
                    "topic_local",
                    "prompt_language",
                    "prompt_zh",
                    "prompt_style",
                ):
                    self.assertTrue(str(metadata[key]).strip())
                self.assertEqual(
                    metadata["prompt_style"],
                    "natural_situational_local_language",
                )

    def test_task_waypoints_resolve_to_map_catalogs(self):
        waypoint_ids_by_map = {}
        for map_id in {task["map_id"] for task in self.tasks}:
            payload = json.loads(
                (MAPS_ROOT / map_id / "waypoints.json").read_text(encoding="utf-8")
            )
            waypoint_ids_by_map[map_id] = {
                waypoint["id"] for waypoint in payload["waypoints"]
            }

        for task in self.tasks:
            with self.subTest(task_id=task["task_id"]):
                self.assertGreaterEqual(len(task["waypoints"]), 2)
                for waypoint in task["waypoints"]:
                    self.assertEqual(set(waypoint), {"id", "name", "caption"})
                    self.assertIn(
                        waypoint["id"], waypoint_ids_by_map[task["map_id"]]
                    )
                    self.assertTrue(waypoint["name"].strip())
                    self.assertTrue(waypoint["caption"].strip())

    def test_shun_lee_internal_navigation_points_are_not_checkpoints(self):
        payload = json.loads(
            (MAPS_ROOT / "shun-lee" / "waypoints.json").read_text(
                encoding="utf-8"
            )
        )
        internal_ids = {
            waypoint["id"]
            for waypoint in payload["waypoints"]
            if waypoint.get("navigation_only")
        }
        self.assertEqual(internal_ids, {"SLH-N01", "SLH-N02"})
        used_ids = {
            waypoint["id"]
            for task in self.tasks
            if task["map_id"] == "shun-lee"
            for waypoint in task["waypoints"]
        }
        self.assertTrue(internal_ids.isdisjoint(used_ids))

    def test_loop_routes_require_intermediate_points_before_shared_target(self):
        benchmark = load_benchmark("finalpool-navigation-v1")
        for task_id in (
            "innopolis-002",
            "mr-beast-1000-harbor-city-005",
        ):
            with self.subTest(task_id=task_id):
                map_id, task = find_task(task_id, benchmark["maps"])
                resolved = resolved_task(map_id, task)
                self.assertEqual(
                    resolved["start"]["waypoint_id"],
                    resolved["target"]["waypoint_id"],
                )
                self.assertGreaterEqual(len(resolved["required_waypoints"]), 1)


if __name__ == "__main__":
    unittest.main()

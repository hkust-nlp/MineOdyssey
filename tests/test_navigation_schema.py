import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.navigation.schema import (
    MAPS_ROOT,
    SchemaError,
    digest_json,
    load_benchmark,
    load_map,
    load_profile,
    load_routes,
    load_setting,
    load_tasks,
    load_waypoints,
    reference_digest,
    resolve_task_eval_setting,
    task_digest,
    validate_receipt,
)


class NavigationSchemaTest(unittest.TestCase):
    def test_map_specific_out_of_bounds_floors_are_exact_and_waypoint_safe(self):
        expected = {
            "chain-bridge": -14,
            "dublin-departments-district": -14,
            "entrup": -14,
            "hofburg": -14,
            "miljacka-riverside": -14,
            "nyc-911-memorials": -14,
            "ohrid": -14,
            "plaza-de-mayo": 1,
            "plaza-hotel": -60,
            "reichstag": 52,
            "shun-lee": -14,
            "torrey-mall": -14,
            "ueno-park": -14,
            "wurzburg": -37,
            "zurich": -14,
        }
        configured = {}
        for map_path in MAPS_ROOT.glob("*/map.json"):
            map_id = map_path.parent.name
            payload = load_map(map_id)
            if "out_of_bounds" not in payload:
                continue
            policy = payload["out_of_bounds"]
            configured[map_id] = policy["at_or_below_y"]
            self.assertEqual(policy["duration_sec"], 15)
            self.assertTrue(
                all(
                    waypoint["position"]["y"] > policy["at_or_below_y"]
                    for waypoint in load_waypoints(map_id).values()
                ),
                map_id,
            )
        self.assertEqual(configured, expected)

    def test_latest_reviewed_annotations_are_pinned_exactly(self):
        waypoint_payload = json.loads(
            (MAPS_ROOT / "shun-lee" / "waypoints.json").read_text(encoding="utf-8")
        )
        route_payload = json.loads(
            (MAPS_ROOT / "shun-lee" / "routes.json").read_text(encoding="utf-8")
        )
        expected_source = {
            "repository": "https://example.invalid/anonymous-source",
            "git_commit": "0000000000000000000000000000000000000000",
            "path": "route-viewer-baritone/assets/shun-lee-current-5-routes.json",
            "sha256": "cbff9918f83f356387577a6d001e872f5d7e0c8f9490b50e4a761332d5efd219",
        }
        self.assertEqual(waypoint_payload["annotation_source"], expected_source)
        self.assertEqual(route_payload["annotation_source"], expected_source)
        self.assertEqual(
            digest_json(waypoint_payload["waypoints"]),
            "e03ddcf6c8c404ae85c1b6c6acbf0442704b68705cb8a29aaab1ee686afac4ec",
        )
        self.assertEqual(
            digest_json(route_payload["routes"]),
            "f9790ac1ea8917730ea956b16ac155523bdf6e9b41ab52b52bbc42be473aa476",
        )

    def test_all_required_waypoints_are_visible_in_prompts(self):
        for map_id in ("shun-lee",):
            waypoints = load_waypoints(map_id)
            for task in load_tasks(map_id):
                required_ids = [
                    waypoint["waypoint_id"]
                    for waypoint in task["required_waypoints"]
                ]
                self.assertEqual(required_ids, task["source"]["intermediate_ids"])
                for waypoint_id in required_ids:
                    self.assertIn(waypoints[waypoint_id]["name"], task["prompt"])

    def test_final_benchmark_has_exact_task_inventory(self):
        benchmark = load_benchmark("finalpool-navigation-v1")
        tasks = {
            map_id: load_tasks(map_id)
            for map_id in benchmark["maps"]
        }
        self.assertEqual(len(benchmark["maps"]), 30)
        self.assertEqual(sum(map(len, tasks.values())), 194)
        self.assertEqual(len(tasks["buckingham-palace"]), 6)
        self.assertEqual(len(tasks["hagia-sophia"]), 3)
        self.assertEqual(len(tasks["notre-dame"]), 6)
        self.assertEqual(len(tasks["plaza-hotel"]), 6)
        self.assertEqual(len(tasks["reichstag"]), 5)
        self.assertEqual(len(tasks["rms-queen-mary"]), 10)
        self.assertEqual(len(tasks["rms-titanic"]), 7)
        self.assertEqual(len(tasks["sofi-stadium"]), 6)
        self.assertEqual(len(tasks["versailles"]), 4)
        self.assertEqual(len(tasks["white-house"]), 9)
        self.assertEqual(len(tasks["shun-lee"]), 5)
        self.assertEqual(
            [task["id"] for task in tasks["shun-lee"]],
            [f"shun-lee-{index:03d}" for index in range(1, 6)],
        )

    def test_final_setting_pins_long_run_and_summary_policy(self):
        setting = load_setting("final-navigation-v1")
        self.assertEqual(
            setting["limits"],
            {"watchdog_timeout_sec": 21600, "max_assistant_steps": 500, "max_deaths": 1},
        )
        self.assertEqual(setting["agent"]["auto_summarize_turn_threshold"], 100)
        self.assertEqual(
            setting["agent"]["auto_summarize_token_threshold"],
            200000,
        )
        self.assertEqual(setting["agent"]["max_images_in_context"], 100)
        self.assertEqual(setting["agent"]["llm_timeout_sec"], 600)
        self.assertEqual(setting["agent"]["bash_timeout_sec"], 300)
        self.assertEqual(setting["agent"]["llm_max_retries"], 0)
        self.assertEqual(setting["agent"]["max_consecutive_llm_failures"], 3)
        self.assertFalse(setting["agent"]["six_view_enabled"])
        self.assertEqual(setting["agent"]["max_conversation_rounds"], 0)
        self.assertFalse(setting["agent"]["default_periodic_observe"])
        self.assertEqual(setting["runtime"]["time"], {"value": 6000, "freeze": True})
        self.assertEqual(
            setting["runtime"]["weather"],
            {"value": "clear", "freeze": True},
        )
        self.assertEqual(setting["runtime"]["waypoint_in_world_max_distance"], 32)
        self.assertEqual(setting["arrival"]["radius_3d"], 3.5)
        self.assertEqual(setting["completion"]["claim_attempt_limit"], 3)
        self.assertEqual(
            setting["task_eval_defaults"],
            resolve_task_eval_setting({}),
        )

    def test_per_task_eval_setting_defaults_and_overrides(self):
        self.assertEqual(
            resolve_task_eval_setting({}),
            {
                "time": "noon",
                "weather": "clear",
                "six_view_enabled": False,
                "player_scale": 1.0,
                "third_person": False,
                "hud_enabled": False,
                "navigation_hints_enabled": False,
                "coordinate_lock_enabled": True,
                "guideline": False,
                "resource_pack_and_shader": False,
            },
        )
        self.assertEqual(
            resolve_task_eval_setting(
                {
                    "time": "midnight",
                    "weather": "rain",
                    "six_view_enabled": True,
                    "player_scale": 0.5,
                    "third_person": True,
                    "hud_enabled": False,
                    "navigation_hints_enabled": True,
                    "guideline": True,
                    "resource_pack_and_shader": True,
                }
            )["player_scale"],
            0.5,
        )
        with self.assertRaises(SchemaError):
            resolve_task_eval_setting({"weather": "thunder"})
        with self.assertRaises(SchemaError):
            resolve_task_eval_setting({"player_scale": 0})
        with self.assertRaises(SchemaError):
            resolve_task_eval_setting({"unknown": False})

    def test_environment_and_claim_options_are_configurable(self):
        payload = json.loads(
            (
                Path("eval/navigation/settings/final-navigation-v1.json")
            ).read_text(encoding="utf-8")
        )
        payload["runtime"]["time"] = {"value": 18000, "freeze": False}
        payload["runtime"]["weather"] = {"value": "rain", "freeze": False}
        payload["agent"]["six_view_enabled"] = True
        payload["completion"]["claim_attempt_limit"] = 5
        with (
            patch("eval.navigation.schema.load_json", return_value=payload),
            patch("eval.navigation.schema.load_profile"),
        ):
            loaded = load_setting("final-navigation-v1")
        self.assertEqual(loaded["runtime"]["time"]["value"], 18000)
        self.assertTrue(loaded["agent"]["six_view_enabled"])
        self.assertEqual(loaded["completion"]["claim_attempt_limit"], 5)

    def test_all_waypoint_links_and_task_digests_are_valid(self):
        benchmark = load_benchmark("finalpool-navigation-v1")
        for map_id in benchmark["maps"]:
            waypoints = load_waypoints(map_id)
            tasks = load_tasks(map_id)
            for task in tasks:
                self.assertIn(task["start"]["waypoint_id"], waypoints)
                self.assertIn(task["target"]["waypoint_id"], waypoints)
                for waypoint in task["required_waypoints"]:
                    self.assertIn(waypoint["waypoint_id"], waypoints)
                self.assertEqual(len(task_digest(map_id, task)), 64)

    def test_profile_and_maps_pin_1_21_11(self):
        profile = load_profile("minecraft-1.21.11")
        self.assertEqual(profile["minecraft"]["version"], "1.21.11")
        self.assertEqual(profile["minecraft"]["data_version"], 4671)
        self.assertEqual(profile["neoforge"]["version"], "21.11.44")
        self.assertEqual(
            {row["mod_id"] for row in profile["client_mods"]["required"]},
            {
                "agentbridge",
                "bocchud",
                "lambdynlights",
                "mafglib",
                "xaerominimap",
                "xaeroworldmap",
            },
        )
        self.assertEqual(
            {
                row["mod_id"]
                for row in profile["client_mods"]["optional_profiles"]["guideline"]
            },
            {"ground_navigation", "baritoe"},
        )
        self.assertEqual(profile["client_configs"][0]["artifact_name"], "minihud.json")
        for map_id in ("shun-lee", "zurich"):
            world = load_map(map_id)["world"]
            self.assertEqual(world["minecraft_version"], "1.21.11")
            self.assertEqual(world["data_version"], 4671)
            self.assertEqual(len(world["expected_source_fingerprint"]), 64)
            self.assertEqual(len(world["expected_prepared_fingerprint"]), 64)
            preparation = world["preparation"]
            self.assertEqual(
                preparation["id"],
                "heights-datapack-minecraft-1.21.11-v1",
            )
            replacement = preparation["replacement_datapack"]
            self.assertEqual(
                replacement["data_pack_format"],
                {"major": 94, "minor": 1},
            )
            self.assertEqual(len(replacement["manifest_sha256"]), 64)
            self.assertEqual(
                replacement["source_path"],
                "eval/navigation/compatibility/heights-1.21.11",
            )

    def test_indoor_candidates_share_the_1_21_11_snapshot_contract(self):
        candidates = {
            "alcatraz",
            "bismarck",
            "buckingham-palace",
            "dkm-tirpitz",
            "grand-budapest-hotel",
            "hagia-sophia",
            "notre-dame",
            "plaza-hotel",
            "rms-queen-mary",
            "rms-titanic",
            "reichstag",
            "sofi-stadium",
            "versailles",
            "white-house",
        }
        for map_id in candidates:
            payload = load_map(map_id)
            self.assertEqual(payload["environment"], "indoor")
            self.assertEqual(payload["world"]["minecraft_version"], "1.21.11")
            self.assertEqual(payload["world"]["data_version"], 4671)
            self.assertLess(payload["world"]["source_version"]["data_version"], 4671)
            self.assertEqual(
                payload["world"]["preparation"]["id"],
                "minecraft-server-force-upgrade-1.21.11-v1",
            )

    def test_factory_collection_has_a_verified_upgrade_contract(self):
        payload = load_map("factory-collection")
        self.assertEqual(payload["environment"], "mixed")
        self.assertEqual(
            payload["source"]["provenance"]["git_commit"],
            "0000000000000000000000000000000000000000",
        )
        self.assertEqual(
            payload["world"]["source_version"],
            {"version_name": "1.20.1", "version_id": 3465, "data_version": 3465},
        )
        self.assertEqual(payload["world"]["minecraft_version"], "1.21.11")
        self.assertEqual(payload["world"]["data_version"], 4671)
        self.assertEqual(
            payload["world"]["preparation"]["id"],
            "minecraft-server-force-upgrade-1.21.11-v1",
        )
        self.assertEqual(
            payload["compatibility"]["status"],
            "server_start_verified_visual_review_required",
        )

    def test_navigation_v1_candidate_waypoints_and_routes_are_linked(self):
        expected = {
            "buckingham-palace": (18, 6),
            "cape-town": (73, 9),
            "chain-bridge": (7, 1),
            "copacabana-waterfront": (20, 5),
            "dublin-departments-district": (37, 7),
            "entrup": (34, 6),
            "hagia-sophia": (10, 3),
            "hofburg": (36, 7),
            "innopolis": (35, 9),
            "memorial-hall-park": (6, 1),
            "miljacka-riverside": (14, 5),
            "mr-beast-1000-harbor-city": (53, 11),
            "notre-dame": (27, 6),
            "nyc-911-memorials": (24, 7),
            "ohrid": (33, 5),
            "plaza-de-mayo": (39, 8),
            "plaza-hotel": (72, 6),
            "reichstag": (20, 5),
            "rms-queen-mary": (79, 10),
            "rms-titanic": (70, 7),
            "santa-lucia-hill": (22, 5),
            "shun-lee": (20, 5),
            "sofi-stadium": (25, 6),
            "sviyazhsk": (34, 6),
            "torrey-mall": (19, 7),
            "ueno-park": (72, 12),
            "versailles": (23, 4),
            "white-house": (40, 9),
            "wurzburg": (78, 10),
            "zurich": (19, 6),
        }
        for map_id, (waypoint_count, route_count) in expected.items():
            map_payload = load_map(map_id)
            self.assertEqual(map_payload["world"]["data_version"], 4671)
            self.assertEqual(len(load_waypoints(map_id)), waypoint_count)
            self.assertEqual(len(load_routes(map_id)), route_count)

    def test_latest_collaborator_navigation_packages_are_pinned(self):
        expected_commit = "0000000000000000000000000000000000000000"
        reviewed_update_commit = "0000000000000000000000000000000000000000"
        reviewed_three_maps_commit = "0000000000000000000000000000000000000000"
        for map_id in (
            "cape-town",
            "copacabana-waterfront",
            "dublin-departments-district",
            "hofburg",
            "memorial-hall-park",
            "miljacka-riverside",
            "nyc-911-memorials",
            "ohrid",
            "plaza-de-mayo",
            "santa-lucia-hill",
            "sviyazhsk",
            "torrey-mall",
            "ueno-park",
        ):
            waypoint_payload = json.loads(
                (MAPS_ROOT / map_id / "waypoints.json").read_text(
                    encoding="utf-8"
                )
            )
            route_payload = json.loads(
                (MAPS_ROOT / map_id / "routes.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                waypoint_payload["annotation_source"]["git_commit"],
                (
                    reviewed_three_maps_commit
                    if map_id in {"ohrid", "sviyazhsk"}
                    else reviewed_update_commit
                    if map_id in {"torrey-mall", "ueno-park"}
                    else expected_commit
                ),
            )
            self.assertEqual(
                route_payload["annotation_source"]["git_commit"],
                (
                    reviewed_three_maps_commit
                    if map_id in {"ohrid", "sviyazhsk"}
                    else reviewed_update_commit
                    if map_id in {
                        "dublin-departments-district",
                        "torrey-mall",
                        "ueno-park",
                    }
                    else expected_commit
                ),
            )

        chain_waypoints = json.loads(
            (MAPS_ROOT / "chain-bridge" / "waypoints.json").read_text(
                encoding="utf-8"
            )
        )
        chain_routes = json.loads(
            (MAPS_ROOT / "chain-bridge" / "routes.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            chain_waypoints["annotation_source"]["git_commit"],
            "0000000000000000000000000000000000000000",
        )
        self.assertEqual(
            chain_routes["annotation_source"]["git_commit"],
            "0000000000000000000000000000000000000000",
        )
        self.assertEqual(
            [point["waypoint_id"] for point in chain_routes["routes"][0]["points"]],
            ["CBR-04", "CBR-02", "CBR-01", "CHAIN-BRIDGE-WP-07"],
        )

        entrup_waypoints = json.loads(
            (MAPS_ROOT / "entrup" / "waypoints.json").read_text(encoding="utf-8")
        )
        entrup_routes = json.loads(
            (MAPS_ROOT / "entrup" / "routes.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            entrup_waypoints["annotation_source"]["git_commit"],
            reviewed_three_maps_commit,
        )
        self.assertEqual(
            entrup_routes["annotation_source"]["git_commit"],
            reviewed_three_maps_commit,
        )
        self.assertEqual(
            [route["id"] for route in entrup_routes["routes"]],
            [f"entrup-v3-{index:02d}" for index in range(1, 7)],
        )

        zurich_waypoints = json.loads(
            (MAPS_ROOT / "zurich" / "waypoints.json").read_text(encoding="utf-8")
        )
        zurich_routes = json.loads(
            (MAPS_ROOT / "zurich" / "routes.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            zurich_waypoints["annotation_source"]["git_commit"], expected_commit
        )
        self.assertEqual(
            zurich_routes["annotation_source"]["git_commit"], expected_commit
        )

    def test_indoor_labeled_waypoints_are_pinned_and_normalized(self):
        expected = {
            "buckingham-palace": (
                18,
                "0000000000000000000000000000000000000000",
            ),
            "hagia-sophia": (10, "0000000000000000000000000000000000000000"),
            "notre-dame": (
                27,
                "0000000000000000000000000000000000000000",
            ),
            "reichstag": (20, "0000000000000000000000000000000000000000"),
            "rms-titanic": (
                70,
                "0000000000000000000000000000000000000000",
            ),
            "rms-queen-mary": (
                79,
                "0000000000000000000000000000000000000000",
            ),
            "plaza-hotel": (
                72,
                "0000000000000000000000000000000000000000",
            ),
            "sofi-stadium": (
                25,
                "0000000000000000000000000000000000000000",
            ),
            "versailles": (
                23,
                "0000000000000000000000000000000000000000",
            ),
            "white-house": (40, "0000000000000000000000000000000000000000"),
        }
        for map_id, (waypoint_count, expected_commit) in expected.items():
            payload = json.loads(
                (MAPS_ROOT / map_id / "waypoints.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                payload["annotation_source"]["git_commit"],
                expected_commit,
            )
            waypoints = load_waypoints(map_id)
            self.assertEqual(len(waypoints), waypoint_count)
            if map_id == "sofi-stadium":
                route_ids = {route["id"] for route in load_routes(map_id)}
                self.assertEqual(len(route_ids), 6)
                self.assertEqual(
                    {
                        route_id
                        for waypoint in waypoints.values()
                        for route_id in waypoint["route_ids"]
                    },
                    route_ids,
                )
            else:
                self.assertTrue(
                    all(
                        not waypoint["route_ids"]
                        for waypoint in waypoints.values()
                    )
                )

    def test_ohrid_uses_the_latest_reviewed_route_set(self):
        waypoint_payload = json.loads(
            (MAPS_ROOT / "ohrid" / "waypoints.json").read_text(encoding="utf-8")
        )
        route_payload = json.loads(
            (MAPS_ROOT / "ohrid" / "routes.json").read_text(encoding="utf-8")
        )
        expected_commit = "0000000000000000000000000000000000000000"
        self.assertEqual(
            waypoint_payload["annotation_source"]["git_commit"], expected_commit
        )
        self.assertEqual(
            route_payload["annotation_source"]["git_commit"], expected_commit
        )
        self.assertEqual(
            [route["id"] for route in route_payload["routes"]],
            [f"ohrid-reviewed-route-{index:02d}" for index in range(1, 6)],
        )
        statuses = [route["validation_status"] for route in route_payload["routes"]]
        self.assertEqual(
            statuses,
            ["pending_baritone_validation_after_route_redesign"] * 5,
        )

    def test_wurzburg_manual_waypoints_are_pinned_and_normalized(self):
        payload = json.loads(
            (MAPS_ROOT / "wurzburg" / "waypoints.json").read_text(
                encoding="utf-8"
            )
        )
        source = MAPS_ROOT / "wurzburg" / "waypoints.xaero.txt"
        self.assertEqual(
            payload["annotation_source"]["sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual(payload["source_record_count"], 78)
        self.assertEqual(payload["waypoint_count"], 78)
        self.assertEqual(payload["excluded_waypoints"], [])
        waypoints = load_waypoints("wurzburg")
        self.assertEqual(len(waypoints), 78)
        self.assertEqual(
            {
                waypoint_id: waypoints[waypoint_id]["name"]
                for waypoint_id in (
                    "WUR-WP-20",
                    "WUR-WP-51",
                    "WUR-WP-62",
                    "WUR-WP-66",
                    "WUR-WP-76",
                    "WUR-WP-77",
                    "WUR-WP-78",
                )
            },
            {
                "WUR-WP-20": "Haltestelle Sozialgericht",
                "WUR-WP-51": "SiemensAG Niederlassung Würzburg",
                "WUR-WP-62": "Häuschen 0",
                "WUR-WP-66": "Aussegnungshalle",
                "WUR-WP-76": "Berliner Pl. 5A",
                "WUR-WP-77": "Ludwigstraße 10",
                "WUR-WP-78": "Kapuzinerstraße 23",
            },
        )
        route_plan = MAPS_ROOT / "wurzburg" / "route-plan-v1.json"
        route_payload = json.loads(
            (MAPS_ROOT / "wurzburg" / "routes.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            route_payload["annotation_source"]["sha256"],
            hashlib.sha256(route_plan.read_bytes()).hexdigest(),
        )
        routes = load_routes("wurzburg")
        self.assertEqual(len(routes), 10)
        self.assertTrue(
            all(
                route["status"] == "review_plan_only_not_certified"
                for route in routes
            )
        )
        expected_route_ids = {waypoint_id: [] for waypoint_id in waypoints}
        for route in routes:
            for point in route["points"]:
                expected_route_ids[point["waypoint_id"]].append(route["id"])
        self.assertEqual(
            {
                waypoint_id: waypoint["route_ids"]
                for waypoint_id, waypoint in waypoints.items()
            },
            expected_route_ids,
        )
        self.assertEqual(
            {
                waypoint_id
                for waypoint_id, route_ids in expected_route_ids.items()
                if not route_ids
            },
            {
                "WUR-WP-05",
                "WUR-WP-14",
                "WUR-WP-15",
                "WUR-WP-18",
                "WUR-WP-20",
                "WUR-WP-24",
                "WUR-WP-25",
                "WUR-WP-26",
                "WUR-WP-27",
                "WUR-WP-28",
                "WUR-WP-38",
                "WUR-WP-48",
                "WUR-WP-51",
                "WUR-WP-52",
                "WUR-WP-57",
                "WUR-WP-63",
                "WUR-WP-64",
                "WUR-WP-68",
                "WUR-WP-70",
                "WUR-WP-71",
                "WUR-WP-74",
            },
        )

    def test_innopolis_manual_waypoints_are_pinned_and_normalized(self):
        map_id = "innopolis"
        payload = json.loads(
            (MAPS_ROOT / map_id / "waypoints.json").read_text(encoding="utf-8")
        )
        source = MAPS_ROOT / map_id / "waypoints.xaero.txt"
        self.assertEqual(
            payload["annotation_source"]["git_commit"],
            "0000000000000000000000000000000000000000",
        )
        self.assertEqual(
            payload["annotation_source"]["sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual(payload["source_record_count"], 35)
        self.assertEqual(payload["waypoint_count"], 35)
        self.assertEqual(payload["excluded_waypoints"], [])
        waypoints = load_waypoints(map_id)
        self.assertEqual(len(waypoints), 35)
        self.assertEqual(len({waypoint["name"] for waypoint in waypoints.values()}), 35)
        self.assertEqual(
            {
                waypoint_id: waypoints[waypoint_id]["name"]
                for waypoint_id in (
                    "INN-WP-02",
                    "INN-WP-11",
                    "INN-WP-12",
                    "INN-WP-30",
                    "INN-WP-31",
                )
            },
            {
                "INN-WP-02": "Остановка 8",
                "INN-WP-11": "ИнноПарк",
                "INN-WP-12": "Теннисный корт",
                "INN-WP-30": "Спортивная улица, 110",
                "INN-WP-31": "Спортивная улица, 100",
            },
        )
        route_ids = {route["id"] for route in load_routes(map_id)}
        self.assertEqual(
            {
                route_id
                for waypoint in waypoints.values()
                for route_id in waypoint["route_ids"]
            },
            route_ids,
        )

    def test_harbor_city_manual_waypoints_are_pinned_and_normalized(self):
        map_id = "mr-beast-1000-harbor-city"
        payload = json.loads(
            (MAPS_ROOT / map_id / "waypoints.json").read_text(encoding="utf-8")
        )
        source = MAPS_ROOT / map_id / "waypoints.xaero.txt"
        self.assertEqual(
            payload["annotation_source"]["git_commit"],
            "0000000000000000000000000000000000000000",
        )
        self.assertEqual(
            payload["annotation_source"]["sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual(payload["source_record_count"], 53)
        self.assertEqual(payload["waypoint_count"], 53)
        self.assertEqual(payload["excluded_waypoints"], [])
        waypoints = load_waypoints(map_id)
        self.assertEqual(len(waypoints), 53)
        self.assertEqual(len({waypoint["name"] for waypoint in waypoints.values()}), 53)
        self.assertEqual(
            {
                waypoint_id: waypoints[waypoint_id]["name"]
                for waypoint_id in (
                    "HBC-WP-04",
                    "HBC-WP-07",
                    "HBC-WP-12",
                    "HBC-WP-30",
                    "HBC-WP-39",
                    "HBC-WP-42",
                    "HBC-WP-53",
                )
            },
            {
                "HBC-WP-04": "Smithery & Forge Shop",
                "HBC-WP-07": "Yard Garage & Auto Repair Shop",
                "HBC-WP-12": "Construction & Repair Storehous",
                "HBC-WP-30": "Motor Generator & Substation 6",
                "HBC-WP-39": "Dry Dock 5 Boat Head",
                "HBC-WP-42": "Machine Shop & Erecting Shop II",
                "HBC-WP-53": "Machine Shop & Erecting Shop I",
            },
        )
        routes = load_routes(map_id)
        self.assertEqual(len(routes), 11)
        routes_by_id = {route["id"]: route for route in routes}
        self.assertEqual(
            routes_by_id["hbc-route-04"]["points"][-2]["waypoint_id"],
            "HBC-WP-39",
        )
        self.assertEqual(
            routes_by_id["hbc-route-04"]["points"][-1]["waypoint_id"],
            "HBC-WP-38",
        )
        self.assertEqual(
            routes_by_id["hbc-route-07"]["points"][-1]["waypoint_id"],
            "HBC-WP-25",
        )
        route_ids = {route["id"] for route in routes}
        self.assertEqual(
            {
                route_id
                for waypoint in waypoints.values()
                for route_id in waypoint["route_ids"]
            },
            route_ids,
        )

    def test_receipt_is_bound_to_task_reference_and_setting(self):
        map_id = "shun-lee"
        task = load_tasks(map_id)[0]
        reference = {
            "task_id": task["id"],
            "status": "reachable",
            "task_digest": task_digest(map_id, task),
            "length_blocks": 100.0,
        }
        setting = load_setting("final-navigation-v1")
        map_payload = load_map(map_id)
        receipt = {
            "schema_version": 1,
            "artifact_kind": "navigation-manual-validation-receipt",
            "status": "verified",
            "map_id": map_id,
            "task_id": task["id"],
            "map_fingerprint": map_payload["world"][
                "expected_prepared_fingerprint"
            ],
            "task_digest": task_digest(map_id, task),
            "reference_digest": reference_digest(reference),
            "setting_digest": digest_json(
                {
                    "arrival": setting["arrival"],
                    "completion": setting["completion"],
                    "runtime": setting["runtime"],
                }
            ),
            "minecraft_version": "1.21.11",
            "data_version": 4671,
            "profile_id": "minecraft-1.21.11",
            "setting_id": "final-navigation-v1",
            "reviewer": "unit-test",
            "reviewed_at_utc": "2026-07-29T00:00:00Z",
            "evidence": "manual adventure walkthrough",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_text(
                __import__("json").dumps(receipt),
                encoding="utf-8",
            )
            with patch(
                "eval.navigation.schema.validation_receipt_path",
                return_value=path,
            ):
                validated = validate_receipt(
                    map_id=map_id,
                    task=task,
                    reference=reference,
                    setting=setting,
                    map_payload=map_payload,
                )
                self.assertEqual(validated["status"], "verified")
                changed_reference = copy.deepcopy(reference)
                changed_reference["length_blocks"] += 1
                with self.assertRaisesRegex(SchemaError, "stale"):
                    validate_receipt(
                        map_id=map_id,
                        task=task,
                        reference=changed_reference,
                        setting=setting,
                        map_payload=map_payload,
                    )


if __name__ == "__main__":
    unittest.main()

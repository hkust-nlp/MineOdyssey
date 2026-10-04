import json
import tempfile
import unittest
from pathlib import Path

from eval.navigation.runner import (
    _activate_optional_client_profile,
    _allocate_ports,
    _clone_tree,
    _configure_client_target,
    _effective_task_settings,
    _remove_runtime_history,
    _task_waypoint_rows,
    _write_guideline_baritone_settings,
)
from eval.navigation.schema import load_setting
from eval.navigation.snapshots import sha256_file


class NavigationRunnerTest(unittest.TestCase):
    def test_guideline_profile_is_activated_only_on_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Path(temporary)
            mods = client / "game" / "mods"
            optional = client / "optional-mods" / "guideline"
            mods.mkdir(parents=True)
            optional.mkdir(parents=True)
            rows = []
            for name, content in (("ground.jar", b"ground"), ("planner.jar", b"planner")):
                source = optional / name
                source.write_bytes(content)
                rows.append({"artifact_name": name, "sha256": sha256_file(source)})
            profile = {"client_mods": {"optional_profiles": {"guideline": rows}}}

            self.assertEqual(list(mods.glob("*.jar")), [])
            activated = _activate_optional_client_profile(client, profile, "guideline")
            self.assertEqual(
                set(activated),
                {"ground.jar", "planner.jar"},
            )
            settings = _write_guideline_baritone_settings(client)
            text = settings.read_text(encoding="utf-8")
            self.assertIn("chatControl false", text)
            self.assertIn("prefixControl false", text)
            self.assertIn("allowBreak false", text)
            self.assertIn("allowPlace false", text)

    def test_task_waypoints_include_start_and_deduplicate_loop_target(self):
        catalog = {
            waypoint_id: {
                "id": waypoint_id,
                "name": waypoint_id,
                "position": {"x": index, "y": 0, "z": 0},
            }
            for index, waypoint_id in enumerate(("start", "via-a", "via-b"))
        }
        selected = _task_waypoint_rows(
            {
                "start": {"waypoint_id": "start"},
                "required_waypoints": [
                    {"waypoint_id": "via-a"},
                    {"waypoint_id": "via-b"},
                ],
                "target": {"waypoint_id": "start"},
            },
            catalog,
        )
        self.assertEqual(
            [waypoint["id"] for waypoint in selected],
            ["start", "via-a", "via-b"],
        )

    def test_task_eval_setting_controls_effective_runtime(self):
        setting = load_setting("final-navigation-v1")
        task_eval, runtime, agent = _effective_task_settings(
            setting,
            {
                "eval_setting": {
                    "time": "midnight",
                    "weather": "rain",
                    "six_view_enabled": True,
                    "player_scale": 0.5,
                    "third_person": True,
                    "hud_enabled": False,
                    "navigation_hints_enabled": True,
                }
            },
        )
        self.assertEqual(runtime["time"], {"value": 18000, "freeze": True})
        self.assertEqual(runtime["weather"], {"value": "rain", "freeze": True})
        self.assertTrue(agent["six_view_enabled"])
        self.assertEqual(task_eval["player_scale"], 0.5)
        self.assertTrue(task_eval["third_person"])
        self.assertFalse(task_eval["hud_enabled"])
        self.assertTrue(task_eval["navigation_hints_enabled"])
        self.assertFalse(task_eval["guideline"])
        self.assertFalse(task_eval["resource_pack_and_shader"])

    def test_fleet_eval_overrides_win_over_task_values(self):
        setting = load_setting("final-navigation-v1")
        task_eval, runtime, agent = _effective_task_settings(
            setting,
            {"eval_setting": {"time": "midnight", "hud_enabled": False}},
            {"time": "noon", "hud_enabled": True, "six_view_enabled": True},
        )
        self.assertEqual(task_eval["time"], "noon")
        self.assertTrue(task_eval["hud_enabled"])
        self.assertTrue(task_eval["six_view_enabled"])
        self.assertEqual(runtime["time"], {"value": 6000, "freeze": True})
        self.assertTrue(agent["six_view_enabled"])

    def test_port_allocation_is_unique(self):
        ports = _allocate_ports(("server", "rcon", "bridge", "bash"))
        self.assertEqual(len(set(ports.values())), 4)

    def test_world_copy_is_not_a_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            original = source / "level.dat"
            original.write_bytes(b"source")
            _clone_tree(source, destination)
            copied = destination / "level.dat"
            self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
            copied.write_bytes(b"changed")
            self.assertEqual(original.read_bytes(), b"source")

    def test_only_runtime_history_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            world = Path(temporary)
            (world / "session.lock").write_bytes(b"lock")
            for name in ("playerdata", "stats", "advancements"):
                directory = world / name
                directory.mkdir()
                (directory / "value").write_text("history", encoding="utf-8")
            region = world / "region"
            region.mkdir()
            (region / "r.0.0.mca").write_bytes(b"world")
            removed = _remove_runtime_history(world)
            self.assertEqual(
                set(removed),
                {"session.lock", "playerdata/", "stats/", "advancements/"},
            )
            self.assertEqual((region / "r.0.0.mca").read_bytes(), b"world")

    def test_xaero_config_contains_only_current_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Path(temporary)
            minihud = client / "game" / "config" / "minihud.json"
            minihud.parent.mkdir(parents=True)
            minihud.write_text(
                '{"Generic":{"mainRenderingToggle":{"enabled":true}}}',
                encoding="utf-8",
            )
            stale = (
                client
                / "game"
                / "xaero"
                / "world-map"
                / "Multiplayer_127.0.0.1"
                / "stale-map-tile.zip"
            )
            stale.parent.mkdir(parents=True)
            stale.write_text("old target", encoding="utf-8")
            backup = client / "game" / "XaeroWaypoints_BACKUP"
            backup.mkdir(parents=True)
            (backup / "stale.txt").write_text("old target", encoding="utf-8")
            old_profile = (
                client
                / "game"
                / "config"
                / "xaero"
                / "minimap"
                / "profiles"
                / "default.cfg"
            )
            old_profile.parent.mkdir(parents=True)
            old_profile.write_text("waypoints_in_world = false\n", encoding="utf-8")
            legacy = client / "game" / "config" / "xaerominimap.txt"
            legacy.write_text("legacy=true\n", encoding="utf-8")
            target = _configure_client_target(
                client,
                selected_waypoints=[
                    {
                        "name": "Destination",
                        "initials": "D",
                        "position": {"x": 1, "y": 2, "z": 3},
                        "color": 17,
                        "xaero": {
                            "disabled": False,
                            "type": 0,
                            "set": "gui.xaero_default",
                            "rotate_on_tp": False,
                            "tp_yaw": 0,
                            "visibility_type": 0,
                            "destination": False,
                        },
                    }
                ],
                hud_enabled=False,
            )
            self.assertFalse(stale.exists())
            waypoint_files = list((client / "game" / "xaero").rglob("*.txt"))
            self.assertEqual(waypoint_files, [target])
            text = target.read_text(encoding="utf-8")
            self.assertIn("waypoint:Destination:D:1:2:3", text)
            self.assertIn(":17:false:0:gui.xaero_default:false:0:0:false", text)
            self.assertFalse(backup.exists())
            self.assertFalse(legacy.exists())

            config = client / "game" / "config" / "xaero"
            minimap = config / "minimap" / "profiles" / "default.cfg"
            world_map = config / "world-map" / "profiles" / "default.cfg"
            minimap_text = minimap.read_text(encoding="utf-8")
            world_map_text = world_map.read_text(encoding="utf-8")
            self.assertIn("waypoints_in_world = true", minimap_text)
            self.assertIn("waypoint_max_distance = 32", minimap_text)
            self.assertIn("display_minimap = false", minimap_text)
            self.assertIn("waypoints_on_minimap = true", minimap_text)
            self.assertIn("hide_waypoint_coordinates = true", minimap_text)
            self.assertIn("waypoint_teleport_cross_dimension = false", minimap_text)
            self.assertNotIn("waypoints_in_world = false", minimap_text)
            self.assertIn("render_waypoints = true", world_map_text)
            self.assertIn("display_coordinates = false", world_map_text)
            self.assertIn("map_teleport_allowed = false", world_map_text)
            self.assertFalse(
                json.loads(minihud.read_text(encoding="utf-8"))["Generic"][
                    "mainRenderingToggle"
                ]["enabled"]
            )
            self.assertTrue(
                (
                    config
                    / "minimap"
                    / "profiles"
                    / "info_display_config"
                    / "default.cfg.txt"
                ).is_file()
            )

    def test_xaero_config_defaults_optional_display_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Path(temporary)
            minihud = client / "game" / "config" / "minihud.json"
            minihud.parent.mkdir(parents=True)
            minihud.write_text(
                '{"Generic":{"mainRenderingToggle":{"enabled":true}}}',
                encoding="utf-8",
            )
            target = _configure_client_target(
                client,
                selected_waypoints=[
                    {
                        "name": "Lánchíd budai hídfője",
                        "initials": None,
                        "position": {"x": -1021, "y": 2, "z": -1663},
                        "color": None,
                    }
                ],
            )
            text = target.read_text(encoding="utf-8")
            self.assertIn(
                "waypoint:Lánchíd budai hídfője:L:-1021:2:-1663:0:"
                "false:0:gui.xaero_default:false:0:0:false",
                text,
            )

    def test_hud_enables_unlimited_first_person_waypoint_distance(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Path(temporary)
            minihud = client / "game" / "config" / "minihud.json"
            minihud.parent.mkdir(parents=True)
            minihud.write_text(
                '{"Generic":{"mainRenderingToggle":{"enabled":false}}}',
                encoding="utf-8",
            )
            _configure_client_target(
                client,
                selected_waypoints=[
                    {
                        "name": "Destination",
                        "initials": "D",
                        "position": {"x": 1, "y": 2, "z": 3},
                    }
                ],
                hud_enabled=True,
            )
            minimap = (
                client
                / "game"
                / "config"
                / "xaero"
                / "minimap"
                / "profiles"
                / "default.cfg"
            )
            minimap_text = minimap.read_text(encoding="utf-8")
            self.assertIn("waypoint_max_distance = 0", minimap_text)
            self.assertIn("display_minimap = true", minimap_text)
            self.assertTrue(
                json.loads(minihud.read_text(encoding="utf-8"))["Generic"][
                    "mainRenderingToggle"
                ]["enabled"]
            )


if __name__ == "__main__":
    unittest.main()

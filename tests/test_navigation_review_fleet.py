import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from eval.navigation.snapshots import NbtReader


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/eval/run-navigation-review-fleet.py"
SPEC = importlib.util.spec_from_file_location("navigation_review_fleet", SCRIPT)
assert SPEC and SPEC.loader
FLEET = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FLEET
SPEC.loader.exec_module(FLEET)


def map_server(
    map_id: str,
    waypoint_count: int,
    route_count: int,
    *,
    index: int = 1,
):
    return FLEET.MapServer(
        index=index,
        map_id=map_id,
        slug=map_id,
        name=map_id,
        archive=Path("unused.zip"),
        archive_bytes=1,
        archive_sha256="0" * 64,
        world_root=f"{map_id}/",
        spawn=(0, 0, 0),
        waypoint_count=waypoint_count,
        route_count=route_count,
    )


class NavigationReviewFleetTest(unittest.TestCase):
    def test_launcher_profile_prefers_ipv4(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("-Djava.net.preferIPv4Stack=true", source)

    def test_servers_dat_uses_unique_localhost_names_and_availability(self):
        maps = [
            map_server("shun-lee", 20, 5, index=1),
            map_server("zurich", 52, 20, index=2),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "servers.dat"
            FLEET.write_servers_dat(maps, path)
            servers = NbtReader(path.read_bytes()).root()["servers"]
        addresses = [row["ip"] for row in servers]
        self.assertEqual(
            addresses,
            ["shun-lee.localhost:25565", "zurich.localhost:25566"],
        )
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertTrue(
            all("Waypoint:YES Route:YES" in row["name"] for row in servers)
        )

    def test_annotation_counts_include_candidate_routes_and_formal_tasks(self):
        self.assertEqual(FLEET.annotation_counts("ohrid"), (33, 5))
        self.assertEqual(FLEET.annotation_counts("shun-lee"), (20, 5))
        self.assertEqual(FLEET.annotation_counts("entrup"), (34, 6))
        self.assertEqual(FLEET.annotation_counts("memorial-hall-park"), (6, 1))
        self.assertEqual(FLEET.annotation_counts("factory-collection"), (0, 0))

    def test_availability_label_is_derived_from_nonempty_inventories(self):
        self.assertEqual(
            FLEET.availability_label(map_server("yes", 2, 3)),
            "Waypoint:YES Route:YES",
        )
        self.assertEqual(
            FLEET.availability_label(map_server("no", 0, 0)),
            "Waypoint:NO Route:NO",
        )

    def test_xaero_install_writes_points_and_removes_stale_empty_map_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            original_client_root = FLEET.CLIENT_ROOT
            FLEET.CLIENT_ROOT = Path(temporary)
            try:
                marked = map_server("ohrid", 33, 5)
                empty = map_server("factory-collection", 0, 0)
                empty_dir = (
                    FLEET.CLIENT_ROOT
                    / "xaero/minimap/Multiplayer_factory-collection.localhost/dim%0"
                )
                empty_dir.mkdir(parents=True)
                stale = empty_dir / "mw$default_1.txt"
                stale.write_text("stale\n", encoding="utf-8")
                map_count, point_count = FLEET.install_xaero_waypoints(
                    [marked, empty]
                )
                self.assertEqual((map_count, point_count), (1, 33))
                generated = (
                    FLEET.CLIENT_ROOT
                    / "xaero/minimap/Multiplayer_ohrid.localhost/dim%0/mw$default_1.txt"
                )
                self.assertEqual(
                    sum(line.startswith("waypoint:") for line in generated.read_text(encoding="utf-8").splitlines()),
                    33,
                )
                self.assertFalse(stale.exists())
            finally:
                FLEET.CLIENT_ROOT = original_client_root


if __name__ == "__main__":
    unittest.main()

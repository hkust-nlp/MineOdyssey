import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    REPO_ROOT
    / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
)


class NavigationReleaseManifestTest(unittest.TestCase):
    def test_unified_release_has_43_unique_1_21_11_maps(self):
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            payload["artifact_kind"],
            "navigation-map-release-manifest",
        )
        self.assertEqual(payload["release_tag"], "navigation-maps-1.21.11-v1")
        self.assertEqual(payload["minecraft_version"], "1.21.11")
        self.assertEqual(payload["data_version"], 4671)
        self.assertEqual(payload["asset_count"], 43)
        self.assertEqual(len(payload["assets"]), 43)

        map_ids = [row["map_id"] for row in payload["assets"]]
        asset_names = [row["asset_name"] for row in payload["assets"]]
        self.assertEqual(len(map_ids), len(set(map_ids)))
        self.assertEqual(len(asset_names), len(set(asset_names)))
        self.assertEqual(
            sum(row["environment"] == "indoor" for row in payload["assets"]),
            14,
        )
        self.assertEqual(
            sum(row["environment"] == "outdoor" for row in payload["assets"]),
            28,
        )
        self.assertEqual(
            sum(row["environment"] == "mixed" for row in payload["assets"]),
            1,
        )
        for row in payload["assets"]:
            self.assertEqual(row["minecraft_version"], "1.21.11")
            self.assertEqual(row["data_version"], 4671)
            self.assertEqual(row["world_root"], row["map_id"])
            self.assertEqual(
                row["asset_name"],
                f"navigation-1.21.11-{row['map_id']}.zip",
            )
            self.assertGreater(row["asset_bytes"], 0)
            self.assertEqual(len(row["asset_sha256"]), 64)
            self.assertEqual(len(row["world_fingerprint"]["value"]), 64)


if __name__ == "__main__":
    unittest.main()

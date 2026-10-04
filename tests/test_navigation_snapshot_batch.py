import argparse
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "snapshot"
    / "prepare-navigation-snapshot.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_navigation_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def snapshot_result(map_id: str) -> dict[str, object]:
    fingerprint = {"algorithm": "sha256", "value": map_id * 4}
    return {
        "level": {"version_name": "1.21.11", "data_version": 4671},
        "world_dir": f"/cache/{map_id}/prepared/world",
        "fingerprint": fingerprint,
        "receipt": {
            "preparation_id": "identity-minecraft-1.21.11-v1",
            "runtime_profile": "minecraft-1.21.11",
        },
        "source": {
            "world_dir": f"/cache/{map_id}/world",
            "fingerprint": fingerprint,
        },
    }


class NavigationSnapshotBatchTest(unittest.TestCase):
    def test_default_run_reuses_valid_and_repairs_invalid_before_preflight(self):
        args = argparse.Namespace(
            map_ids=None,
            downloads_dir=Path("downloads"),
            download_missing=False,
            replace=False,
            verify_only=False,
        )
        verify_calls = {"map-a": 0, "map-b": 0}

        def verify(payload):
            map_id = payload["map_id"]
            verify_calls[map_id] += 1
            if map_id == "map-b" and verify_calls[map_id] == 1:
                raise RuntimeError("stale")
            return snapshot_result(map_id)

        with (
            patch.object(MODULE, "parse_args", return_value=args),
            patch.object(
                MODULE,
                "load_benchmark",
                return_value={
                    "benchmark_id": "finalpool-navigation-v1",
                    "maps": ["map-a", "map-b"],
                },
            ),
            patch.object(MODULE, "load_map", side_effect=lambda value: {"map_id": value}),
            patch.object(MODULE, "verify_snapshot", side_effect=verify),
            patch.object(
                MODULE,
                "prepare_snapshot",
                side_effect=lambda payload, **_kwargs: snapshot_result(payload["map_id"]),
            ) as prepare,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = MODULE.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(return_code, 0)
        self.assertTrue(payload["preflight"]["ready"])
        self.assertEqual(payload["preflight"]["ready_maps"], 2)
        self.assertEqual(
            [row["action"] for row in payload["preparation"]],
            ["reused", "repaired"],
        )
        self.assertEqual(prepare.call_count, 1)
        self.assertTrue(prepare.call_args.kwargs["replace"])

    def test_failure_is_reported_after_other_maps_are_processed(self):
        args = argparse.Namespace(
            map_ids=["map-a", "map-b"],
            downloads_dir=Path("downloads"),
            download_missing=False,
            replace=True,
            verify_only=False,
        )

        def prepare(payload, **_kwargs):
            if payload["map_id"] == "map-a":
                raise RuntimeError("broken archive")
            return snapshot_result(payload["map_id"])

        def verify(payload):
            if payload["map_id"] == "map-a":
                raise RuntimeError("not ready")
            return snapshot_result(payload["map_id"])

        with (
            patch.object(MODULE, "parse_args", return_value=args),
            patch.object(
                MODULE,
                "load_benchmark",
                return_value={
                    "benchmark_id": "finalpool-navigation-v1",
                    "maps": ["map-a", "map-b"],
                },
            ),
            patch.object(MODULE, "load_map", side_effect=lambda value: {"map_id": value}),
            patch.object(MODULE, "verify_snapshot", side_effect=verify),
            patch.object(MODULE, "prepare_snapshot", side_effect=prepare),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = MODULE.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(return_code, 3)
        self.assertFalse(payload["preflight"]["ready"])
        self.assertEqual(payload["preflight"]["ready_maps"], 1)
        self.assertEqual(payload["preparation"][0]["map_id"], "map-b")
        self.assertEqual(payload["preparation_errors"][0]["map_id"], "map-a")


if __name__ == "__main__":
    unittest.main()

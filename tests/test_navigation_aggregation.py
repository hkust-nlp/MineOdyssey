import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from eval.navigation.schema import (
    SchemaError,
    digest_json,
    load_map,
    load_profile,
    load_setting,
    load_tasks,
    map_preparation_digest,
    task_digest,
)


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "eval"
    / "aggregate-navigation-results.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("navigation_aggregate_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class NavigationAggregationTest(unittest.TestCase):
    def _result(
        self,
        root: Path,
        *,
        task_id: str,
        success: bool,
        infrastructure_error: bool,
        model_id: str = "test-model",
        runtime_receipt_digest: str = "a" * 64,
    ) -> None:
        profile = load_profile("minecraft-1.21.11")
        setting = load_setting("final-navigation-v1")
        task_payload = next(
            row for row in load_tasks("shun-lee") if row["id"] == task_id
        )
        task = root / "run" / task_id
        map_payload = load_map("shun-lee")
        run = {
            "mode": "formal",
            "formal_eligible": True,
            "run_id": "run",
            "task_id": task_id,
            "map_id": "shun-lee",
            "benchmark_id": "finalpool-navigation-v1",
            "profile_id": profile["profile_id"],
            "profile_digest": digest_json(profile),
            "runtime_receipt_digest": runtime_receipt_digest,
            "setting_id": setting["setting_id"],
            "setting_digest": digest_json(setting),
            "map_fingerprint": map_payload["world"][
                "expected_prepared_fingerprint"
            ],
            "source_map_fingerprint": map_payload["world"][
                "expected_source_fingerprint"
            ],
            "snapshot_preparation_digest": map_preparation_digest(map_payload),
            "task_digest": task_digest("shun-lee", task_payload),
            "reference_digest": None,
            "reference_length_blocks": None,
            "validation_receipt_digest": None,
            "model_parameters": {"model_id": model_id, "temperature": 0},
        }
        completion = {
            "mode": "formal",
            "success": success,
            "infrastructure_error": infrastructure_error,
            "terminal_reason": "claim_done_arrived" if success else "server_crash",
            "oracle_arrived": success,
            "claim_count": 1 if success else 0,
        }
        metrics = {
            "success": success,
            "duration_sec": 10,
            "path_length_xz": 12,
            "path_length_3d": 13,
            "progress_ratio_xz": 1 if success else 0,
            "best_progress_ratio_xz": 1 if success else 0,
            "static_spl": 0.8 if success else 0,
            "checkpoints": {"ordered_reached": 1, "ordered_total": 2},
        }
        supervisor = {
            "success": success,
            "infrastructure_error": infrastructure_error,
        }
        _write(task / "run.json", run)
        _write(task / "completion.json", completion)
        _write(task / "metrics.json", metrics)
        _write(task / "supervisor.json", supervisor)
        _write(task / "agent-result.json", {"decision_count": 3})
        _write(
            task / "snapshot-after-run.json",
            {
                "artifact_kind": "navigation-post-run-snapshot-check",
                "verified": True,
                "run_id": "run",
                "task_id": task_id,
                "map_id": "shun-lee",
                "expected_source_fingerprint": run["source_map_fingerprint"],
                "actual_source_fingerprint": run["source_map_fingerprint"],
                "expected_prepared_fingerprint": run["map_fingerprint"],
                "actual_prepared_fingerprint": run["map_fingerprint"],
                "expected_preparation_digest": run[
                    "snapshot_preparation_digest"
                ],
                "actual_preparation_digest": run[
                    "snapshot_preparation_digest"
                ],
            },
        )
        if not infrastructure_error:
            _write(
                task / "runtime-readback.json",
                {
                    "success": True,
                    "minecraft_version": "1.21.11",
                    "neoforge_version": "21.11.44",
                },
            )

    def test_infrastructure_errors_are_not_task_failures(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._result(
                root,
                task_id="shun-lee-001",
                success=True,
                infrastructure_error=False,
            )
            self._result(
                root,
                task_id="shun-lee-002",
                success=False,
                infrastructure_error=True,
            )
            module.RESULTS_ROOT = root
            aggregate = module.aggregate("run", require_all=False)
            self.assertEqual(aggregate["summary"]["task_runs"], 2)
            self.assertEqual(aggregate["summary"]["infrastructure_errors"], 1)
            self.assertEqual(aggregate["summary"]["scored_task_runs"], 1)
            self.assertEqual(aggregate["summary"]["success_rate"], 1)

    def test_mixed_model_parameters_are_rejected(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._result(
                root,
                task_id="shun-lee-001",
                success=True,
                infrastructure_error=False,
                model_id="first",
            )
            self._result(
                root,
                task_id="shun-lee-002",
                success=True,
                infrastructure_error=False,
                model_id="second",
            )
            module.RESULTS_ROOT = root
            with self.assertRaisesRegex(SchemaError, "mixed model"):
                module.aggregate("run", require_all=False)

    def test_mixed_runtime_receipts_are_rejected(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._result(
                root,
                task_id="shun-lee-001",
                success=True,
                infrastructure_error=False,
                runtime_receipt_digest="a" * 64,
            )
            self._result(
                root,
                task_id="shun-lee-002",
                success=True,
                infrastructure_error=False,
                runtime_receipt_digest="b" * 64,
            )
            module.RESULTS_ROOT = root
            with self.assertRaisesRegex(SchemaError, "mixed runtime_receipt"):
                module.aggregate("run", require_all=False)


if __name__ == "__main__":
    unittest.main()

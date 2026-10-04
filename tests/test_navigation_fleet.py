from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "eval" / "run-navigation-fleet.py"
SPEC = importlib.util.spec_from_file_location("run_navigation_fleet", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


class NavigationFleetTest(unittest.TestCase):
    def _write_result(
        self,
        task_dir: Path,
        *,
        infrastructure_error: bool,
        success: bool,
    ) -> None:
        task_dir.mkdir(parents=True)
        payloads = {
            "run.json": {
                "mode": "formal",
                "model_parameters": {
                    "model_id": "example/model",
                    "api_protocol": "responses",
                },
                "record_video": False,
                "worker_resources": {"cpus": 4.0, "memory": "8g"},
                "eval_setting_overrides": {},
            },
            "completion.json": {
                "success": success,
                "infrastructure_error": infrastructure_error,
            },
            "metrics.json": {},
            "supervisor.json": {},
            "agent-result.json": {},
            "snapshot-after-run.json": {"verified": True},
        }
        for name, payload in payloads.items():
            (task_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    def _state(self, task_dir: Path) -> str:
        return fleet._result_state(
            task_dir,
            mode="formal",
            expected_model_parameters={
                "model_id": "example/model",
                "api_protocol": "responses",
            },
            record_video=False,
            worker_resources={"cpus": 4.0, "memory": "8g"},
            eval_setting_overrides={},
        )

    def test_normal_model_failure_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            self._write_result(task_dir, infrastructure_error=False, success=False)
            self.assertEqual(self._state(task_dir), "complete")

    def test_infrastructure_error_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            self._write_result(task_dir, infrastructure_error=True, success=False)
            self.assertEqual(self._state(task_dir), "infrastructure_error")

    def test_missing_artifact_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            self._write_result(task_dir, infrastructure_error=False, success=True)
            (task_dir / "metrics.json").unlink()
            self.assertEqual(self._state(task_dir), "incomplete")

    def test_container_command_enforces_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(
                cpus_per_task=4.0,
                memory_per_task="8g",
                shm_size_per_task="2g",
                results_root=root / "results",
                runtime_root=root / "runtime",
                runtime_template_root=REPO_ROOT / "eval" / "templates" / "_local" / "navigation",
                base_url="https://example.test/v1",
                image="mcbots-navigation:1.21.11",
                mode="formal",
                model_id="example/model",
                api_protocol="responses",
                model_parameters_json="{}",
                eval_setting_overrides_json="{}",
                record_video=True,
            )
            _name, command = fleet._container_command(
                args,
                engine="podman",
                run_id="run-001",
                task_id="task-001",
                attempt=1,
            )
            self.assertIn("--cpus", command)
            self.assertEqual(command[command.index("--cpus") + 1], "4.0")
            self.assertEqual(command[command.index("--memory") + 1], "8g")
            self.assertIn("--cap-add=ALL", command)
            self.assertIn("--record-video", command)
            self.assertIn("responses", command)

    def test_gpu_slot_plan_is_even_and_interleaved(self) -> None:
        plan = fleet._gpu_slot_plan(30, ["0", "1", "2", "3", "4", "5"])
        self.assertEqual(len(plan), 30)
        self.assertEqual(plan[:6], ["0", "1", "2", "3", "4", "5"])
        self.assertEqual({device: plan.count(device) for device in set(plan)}, {
            "0": 5,
            "1": 5,
            "2": 5,
            "3": 5,
            "4": 5,
            "5": 5,
        })

    def test_gpu_slot_plan_differs_by_at_most_one(self) -> None:
        plan = fleet._gpu_slot_plan(10, ["2", "4", "7"])
        self.assertEqual([plan.count(device) for device in ("2", "4", "7")], [4, 3, 3])

    def test_gpu_device_parser_normalizes_and_rejects_duplicates(self) -> None:
        self.assertEqual(fleet._parse_gpu_devices("0, 02,7"), ["0", "2", "7"])
        with self.assertRaises(SystemExit):
            fleet._parse_gpu_devices("1,01")
        with self.assertRaises(SystemExit):
            fleet._parse_gpu_devices("0,gpu1")

    def test_xorg_bus_id_conversion(self) -> None:
        self.assertEqual(fleet._xorg_bus_id("00000000:41:00.0"), "PCI:65:0:0")

    def test_gpu_container_command_uses_one_cdi_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(
                cpus_per_task=4.0,
                memory_per_task="8g",
                shm_size_per_task="2g",
                results_root=root / "results",
                runtime_root=root / "runtime",
                runtime_template_root=REPO_ROOT / "eval" / "templates" / "_local" / "navigation",
                base_url="https://example.test/v1",
                image="mcbots-navigation-gpu:1.21.11",
                mode="formal",
                model_id="example/model",
                api_protocol="responses",
                model_parameters_json="{}",
                eval_setting_overrides_json="{}",
                record_video=False,
                gpu_bus_ids={"5": "PCI:65:0:0"},
            )
            _name, command = fleet._container_command(
                args,
                engine="podman",
                run_id="run-001",
                task_id="task-001",
                attempt=1,
                gpu_device="5",
            )
            self.assertIn("nvidia.com/gpu=5", command)
            self.assertIn("GPU_PCI_BUSID=PCI:65:0:0", command)
            self.assertIn("DISPLAY_INDEX=0", command)
            self.assertIn("MCBOTS_NAV_RUNTIME_BACKEND=linux-container-gpu", command)
            self.assertEqual(command[command.index("--allocated-gpu") + 1], "5")

    def test_missing_task_list_selects_full_benchmark(self) -> None:
        selected = fleet._selected_tasks(argparse.Namespace(task_list_file=None))
        self.assertEqual(len(selected), 194)
        self.assertEqual(len(selected), len(set(selected)))

    def test_task_list_file_preserves_requested_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_ids": ["cape-town-002", "cape-town-001"],
                    }
                ),
                encoding="utf-8",
            )
            selected = fleet._selected_tasks(
                argparse.Namespace(task_list_file=path)
            )
            self.assertEqual(selected, ["cape-town-002", "cape-town-001"])


if __name__ == "__main__":
    unittest.main()

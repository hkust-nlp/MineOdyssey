import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import tarfile
import tomllib
import unittest

from eval.harbor_agents.instructions import ROOT, TASK, check_instruction, task_definition
from eval.navigation.schema import load_map


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


exporter = module("harbor_catalog_exporter", ROOT / "scripts/eval/export-harbor-navigation.py")


class HarborCatalogExportTests(unittest.TestCase):
    def test_main_image_contains_original_agent_without_evaluator_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "agent-source.tar.gz"
            exporter.export_agent_source(archive_path)
            with tarfile.open(archive_path) as archive:
                names = set(archive.getnames())
                self.assertIn("agent/main.py", names)
                self.assertIn("eval/harbor_agents/container.py", names)
                self.assertIn("uv.lock", names)
                self.assertFalse(any(name.startswith(("eval/navigation/", "config/", "configs/")) for name in names))
                self.assertNotIn("eval/harbor_agents/original.py", names)
                for name in names:
                    self.assertEqual(archive.extractfile(name).read(), (ROOT / name).read_bytes())

    def test_identical_inputs_produce_identical_archive_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.txt"
            source.write_text("same source\n")
            first, second = root / "first.tar.gz", root / "second.tar.gz"
            exporter.archive_tree(first, [(source, "input.txt")])
            os.utime(source, (123456, 123456))
            exporter.archive_tree(second, [(source, "input.txt")])
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first) as archive:
                self.assertEqual(archive.extractfile("input.txt").read(), b"same source\n")

    def test_selected_tasks_bind_instruction_world_and_separate_verifier(self):
        for task_id in ("innopolis-008", "wurzburg-007", "plaza-hotel-002", "hofburg-003"):
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / task_id
                shutil.copytree(TASK, output, ignore=shutil.ignore_patterns("__pycache__"))
                map_id, task = task_definition(task_id)
                exporter.configure_task(output, task_id, load_map(map_id), task)
                check_instruction(output)
                self.assertTrue((output / "instruction.md").read_text().startswith(task["prompt"]))
                config = tomllib.loads((output / "task.toml").read_text())
                self.assertEqual(config["metadata"]["source_task_id"], task_id)
                self.assertEqual(config["verifier"]["environment_mode"], "separate")
                world = module("exported_world", output / "environment/world/gateway.py")
                verifier = module("exported_verifier", output / "tests/verify.py")
                self.assertEqual(world.TASK_ID, task_id)
                self.assertEqual(world.RESULT.parent.name, task_id)
                self.assertEqual(verifier.TASK_ID, task_id)
                done = dict(task_id=task_id, terminal=True, success=True,
                            terminal_reason="claim_done_arrived", infrastructure_error=False)
                self.assertEqual(verifier.reward(done), 1)
                self.assertEqual(verifier.reward({**done, "task_id": "innopolis-006"}), 0)
                docker = (output / "environment/world/Dockerfile").read_text()
                self.assertIn("ARG NAVIGATION_MAP_ID=" + map_id, docker)
                self.assertEqual(json.loads((output / "validation.json").read_text())["status"],
                                 "exported_not_live_validated")

    def test_unknown_or_path_task_rejected(self):
        for task_id in ("missing-001", "../innopolis-006"):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                task_definition(task_id)


if __name__ == "__main__":
    unittest.main()

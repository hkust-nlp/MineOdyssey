import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.env import Environment


class EnvironmentPanoramaTest(unittest.TestCase):
    def _make_runtime(self, root: Path) -> tuple[Path, Path, Path]:
        workspace = root / "game"
        workspace.mkdir()
        runtime_config = root / ".mcbots_runtime.json"
        runtime_config.write_text("{}", encoding="utf-8")
        mcapi = root / "scripts" / "runtime" / "mcapi"
        mcapi.parent.mkdir(parents=True)
        mcapi.write_text("#!/bin/sh\n", encoding="utf-8")
        panorama = (
            root
            / "scripts"
            / "analysis"
            / "capture-practical-panorama-inside.sh"
        )
        panorama.parent.mkdir(parents=True)
        panorama.write_text("#!/bin/sh\n", encoding="utf-8")
        return workspace, runtime_config, panorama

    def test_runtime_state_prefers_local_mcapi(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, runtime_config, _panorama = self._make_runtime(root)
            env = Environment(container_name="test-bot")
            self.addCleanup(env.http.close)
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"data": {"gui_open": False}}),
                stderr="",
            )
            with patch.dict(
                os.environ,
                {
                    "MCBOTS_PROJECT_ROOT": str(root),
                    "MCBOTS_WORKSPACE_ROOT": str(workspace),
                    "MCBOTS_RUNTIME_CONFIG": str(runtime_config),
                },
            ), patch("agent.env.subprocess.run", return_value=completed) as run:
                self.assertEqual(env.fetch_runtime_state(), {"gui_open": False})
            self.assertEqual(run.call_args.args[0][-1], "state")

    def test_panorama_prefers_local_capture_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, runtime_config, panorama = self._make_runtime(root)
            env = Environment(container_name="test-bot", player_name="client")
            self.addCleanup(env.http.close)

            def run_capture(command, **_kwargs):
                output = Path(command[command.index("--out") + 1])
                output.write_bytes(b"six-view-jpeg")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.dict(
                os.environ,
                {
                    "MCBOTS_PROJECT_ROOT": str(root),
                    "MCBOTS_WORKSPACE_ROOT": str(workspace),
                    "MCBOTS_RUNTIME_CONFIG": str(runtime_config),
                },
            ), patch("agent.env.subprocess.run", side_effect=run_capture) as run:
                self.assertEqual(env.capture_panorama(), b"six-view-jpeg")
            self.assertEqual(Path(run.call_args.args[0][0]), panorama)


if __name__ == "__main__":
    unittest.main()

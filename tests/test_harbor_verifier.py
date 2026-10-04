"""Exercise Harbor's actual verifier parser with isolated execution at its boundary."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from harbor.models.task.config import TaskOS, VerifierEnvironmentMode
from harbor.models.task.task import Task
from harbor.models.trial.config import VerifierConfig
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.factory import VerifierFactory
from harbor.verifier.verifier import RewardFileNotFoundError

from eval.harbor_agents.verifier import NavigationInfrastructureError, NavigationVerifier
from tests.test_harbor_navigation import TASK, verifier


class NavigationVerifierTests(unittest.IsolatedAsyncioTestCase):
    def completion(self):
        return dict(task_id="innopolis-006", terminal=True, success=False,
                    infrastructure_error=True, terminal_reason="wall_clock_watchdog")

    def adapter(self, directory, completion):
        paths = TrialPaths(trial_dir=Path(directory))
        paths.verifier_dir.mkdir(parents=True)

        async def execute(command, **kwargs):
            if command.startswith("chmod "):
                return SimpleNamespace(return_code=0)
            try:
                if completion is not None:
                    verifier.write_result(completion, paths.verifier_dir)
            except SystemExit:
                return SimpleNamespace(return_code=1)
            return SimpleNamespace(return_code=0)

        environment = SimpleNamespace(os=TaskOS.LINUX,
            capabilities=SimpleNamespace(mounted=True), exec=AsyncMock(side_effect=execute))
        instance = VerifierFactory.create_verifier_from_config(
            VerifierConfig(import_path="eval.harbor_agents.verifier:NavigationVerifier"),
            task=Task(TASK), trial_paths=paths, environment=environment, skip_tests_upload=True)
        return instance, paths

    async def test_watchdog_and_provider_failure_keep_real_reason_and_no_score(self):
        for reason in ("wall_clock_watchdog", "llm_failure_limit", "agent_crash"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                completion = {**self.completion(), "terminal_reason": reason}
                instance, paths = self.adapter(directory, completion)
                with self.assertRaisesRegex(NavigationInfrastructureError, reason + "; unscored"):
                    await instance.verify()
                self.assertFalse(paths.reward_text_path.exists())
                self.assertFalse(paths.reward_json_path.exists())
                self.assertEqual(json.loads((paths.verifier_dir / "completion.json").read_text()), completion)

    async def test_scored_success_and_failure_are_unchanged(self):
        for success, reason in ((True, "claim_done_arrived"), (False, "death")):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as directory:
                completion = {**self.completion(), "infrastructure_error": False,
                              "success": success, "terminal_reason": reason}
                instance, _ = self.adapter(directory, completion)
                result = await instance.verify()
                self.assertEqual(result.rewards, {"reward": int(success)})

    async def test_missing_or_mismatched_evidence_keeps_missing_reward_error(self):
        cases = [None, {**self.completion(), "task_id": "other-001"},
                 {**self.completion(), "terminal": False},
                 {**self.completion(), "infrastructure_error": "true"},
                 {**self.completion(), "terminal_reason": None}]
        for completion in cases:
            with self.subTest(completion=completion), tempfile.TemporaryDirectory() as directory:
                instance, _ = self.adapter(directory, completion)
                with self.assertRaises(RewardFileNotFoundError):
                    await instance.verify()

    def test_shared_agent_environment_cannot_supply_infrastructure_evidence(self):
        task = Task(TASK)
        task.config.verifier.environment_mode = VerifierEnvironmentMode.SHARED
        with self.assertRaisesRegex(ValueError, "separate trusted verifier"):
            NavigationVerifier(task=task, trial_paths=None, environment=None)


if __name__ == "__main__":
    unittest.main()

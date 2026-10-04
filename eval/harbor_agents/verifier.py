"""Report trusted unscored navigation outcomes through Harbor's verifier API."""
import json
import re

from harbor.models.task.config import VerifierEnvironmentMode
from harbor.verifier.verifier import RewardFileNotFoundError, Verifier


class NavigationInfrastructureError(RuntimeError):
    """The original evaluator ended the trial without a navigation score."""


class NavigationVerifier(Verifier):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.task.config.verifier.environment_mode != VerifierEnvironmentMode.SEPARATE:
            raise ValueError("NavigationVerifier requires the separate trusted verifier container")
        # Harbor's custom-verifier factory does not forward skip_tests_upload.
        # Navigation tasks bake /tests into the separate verifier image.
        self._skip_tests_upload = True

    async def verify(self):
        try:
            return await super().verify()
        except RewardFileNotFoundError as error:
            # Only interpret output collected by Harbor from the trusted verifier.
            # Missing/malformed evidence remains a missing-reward error.
            try:
                completion = json.loads(
                    (self.trial_paths.verifier_dir / "completion.json").read_text())
            except (OSError, ValueError):
                raise error
            task_id = self.task.config.metadata.get("source_task_id")
            if not isinstance(completion, dict) or not task_id:
                raise error
            reason = completion.get("terminal_reason")
            if (completion.get("task_id") != task_id
                    or completion.get("terminal") is not True
                    or completion.get("infrastructure_error") is not True
                    or not isinstance(reason, str)
                    or not re.fullmatch(r"[a-z_]{1,100}", reason)):
                raise error
            raise NavigationInfrastructureError(
                f"Navigation trial ended: {reason}; unscored infrastructure outcome. "
                "See verifier/completion.json for the original evaluator result."
            ) from error

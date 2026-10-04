"""Compare effective prompts with hashes extracted from the initial submission."""
import hashlib
import json
from pathlib import Path
import unittest

from agent.navigation_prompt import BASELINE_COMMIT, build_navigation_system_prompt

BASELINE = json.loads((Path(__file__).parent / "fixtures/navigation_prompt_baseline.json").read_text())


class NavigationPromptParityTests(unittest.TestCase):
    def test_xml_prompt_matches_initial_submission_for_every_optional_section(self):
        self.assertEqual(BASELINE_COMMIT, BASELINE["source_commit"])
        for hints in (False, True):
            for panorama in (False, True):
                with self.subTest(hints=hints, panorama=panorama):
                    prompt = build_navigation_system_prompt(hints_enabled=hints, panorama_enabled=panorama)
                    digest = hashlib.sha256(prompt.encode()).hexdigest()
                    self.assertEqual(digest, BASELINE["xml_prompt_sha256"][
                        f"hints={int(hints)},panorama={int(panorama)}"])




if __name__ == "__main__":
    unittest.main()

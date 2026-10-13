"""Preserve historical prompt bytes apart from the explicit yaw correction."""
import hashlib
import json
from pathlib import Path
import unittest

from agent.navigation_prompt import BASELINE_COMMIT, build_navigation_system_prompt

BASELINE = json.loads((Path(__file__).parent / "fixtures/navigation_prompt_baseline.json").read_text())


class NavigationPromptParityTests(unittest.TestCase):
    def test_xml_prompt_only_changes_yaw_directions_for_every_optional_section(self):
        self.assertEqual(BASELINE_COMMIT, BASELINE["source_commit"])
        for hints in (False, True):
            for panorama in (False, True):
                with self.subTest(hints=hints, panorama=panorama):
                    prompt = build_navigation_system_prompt(hints_enabled=hints, panorama_enabled=panorama)
                    corrected = "- Positive yaw turns right.\n- Negative yaw turns left."
                    self.assertEqual(prompt.count(corrected), 1)
                    historical = prompt.replace(corrected,
                        "- Positive yaw turns left.\n- Negative yaw turns right.")
                    digest = hashlib.sha256(historical.encode()).hexdigest()
                    self.assertEqual(digest, BASELINE["xml_prompt_sha256"][
                        f"hints={int(hints)},panorama={int(panorama)}"])




if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from eval.navigation.live_dashboard import build_state


class NavigationLiveDashboardTests(unittest.TestCase):
    def test_build_state_reads_assistant_turns_and_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "runtime"
            results_dir = root / "results"
            (run_dir / "control").mkdir(parents=True)
            (results_dir / "agent-record").mkdir(parents=True)
            (run_dir / "control" / "run.json").write_text(
                json.dumps({"results_dir": str(results_dir), "task": {"prompt": "go"}}),
                encoding="utf-8",
            )
            (results_dir / "agent-record" / "messages.json").write_text(
                json.dumps(
                    [
                        {"role": "user", "content": "go"},
                        {
                            "role": "assistant",
                            "reasoning_content": "look around",
                            "content": "<action><type>skip</type></action>",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            (run_dir / "control" / "evaluator-events.jsonl").write_text(
                '{"event":"checkpoint"}\n', encoding="utf-8"
            )
            state = build_state(run_dir, "http://127.0.0.1:6080/vnc.html")
            self.assertEqual(state["turns"][0]["reasoning"], "look around")
            self.assertIn("skip", state["turns"][0]["content"])
            self.assertEqual(state["events"][0]["event"], "checkpoint")
            self.assertIn("Minecraft Navigation Agent", state["system_prompt"])


if __name__ == "__main__":
    unittest.main()

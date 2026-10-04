import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent.agent import Agent
from scripts.analysis.finalize_agent_messages import (
    MessageJournalError,
    finalize_agent_messages,
)
from scripts.analysis.trajectory_viewer import (
    find_message_files_under,
    load_messages_html,
)


def _envelope(sequence: int, message: dict) -> str:
    return json.dumps(
        {"schema_version": 1, "seq": sequence, "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class AgentMessageJournalTests(unittest.TestCase):
    def test_agent_appends_complete_ordered_journal_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            stale_final = record_dir / "messages.json"
            stale_final.write_text("stale", encoding="utf-8")
            agent = Agent.__new__(Agent)
            agent.record_dir = record_dir
            agent.screenshot_dir = record_dir / "screenshots"
            agent.messages_file = stale_final
            agent.messages_journal_file = None
            agent._messages_journal_handle = None
            agent._messages_journal_next_seq = 0
            agent.full_message_history = []
            agent.messages_lock = threading.RLock()

            agent._initialize_message_journal()
            with agent.messages_lock:
                agent._append_full_history_locked({"role": "user", "content": "去车站"})
                agent._append_full_history_locked(
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]}
                )
            agent._save_messages()
            agent._messages_journal_handle.close()

            self.assertFalse(stale_final.exists())
            rows = [
                json.loads(line)
                for line in (record_dir / "messages.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual([row["seq"] for row in rows], [0, 1])
            self.assertEqual(
                [row["message"] for row in rows], agent.full_message_history
            )

    def test_finalize_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            messages = [
                {"role": "system", "content": "导航"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            ]
            (record_dir / "messages.jsonl").write_text(
                "".join(f"{_envelope(index, message)}\n" for index, message in enumerate(messages)),
                encoding="utf-8",
            )

            first = finalize_agent_messages(record_dir)
            first_bytes = (record_dir / "messages.json").read_bytes()
            second = finalize_agent_messages(record_dir)

            self.assertEqual(first.message_count, 2)
            self.assertFalse(first.dropped_incomplete_tail)
            self.assertEqual(json.loads(first_bytes), messages)
            self.assertEqual((record_dir / "messages.json").read_bytes(), first_bytes)
            self.assertEqual(second.message_count, 2)

    def test_finalize_drops_only_an_unterminated_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            committed = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]
            journal = "".join(
                f"{_envelope(index, message)}\n"
                for index, message in enumerate(committed)
            )
            journal += '{"schema_version":1,"seq":2,"message":'
            (record_dir / "messages.jsonl").write_text(journal, encoding="utf-8")

            result = finalize_agent_messages(record_dir)

            self.assertTrue(result.dropped_incomplete_tail)
            self.assertEqual(
                json.loads((record_dir / "messages.json").read_text(encoding="utf-8")),
                committed,
            )

    def test_malformed_committed_line_preserves_existing_final(self):
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            final_path = record_dir / "messages.json"
            original = b'["previous"]\n'
            final_path.write_bytes(original)
            (record_dir / "messages.jsonl").write_text(
                f'{_envelope(0, {"role": "user", "content": "ok"})}\nnot-json\n',
                encoding="utf-8",
            )

            with self.assertRaises(MessageJournalError):
                finalize_agent_messages(record_dir)

            self.assertEqual(final_path.read_bytes(), original)
            self.assertEqual(list(record_dir.glob(".messages.*.json.tmp")), [])

    def test_trajectory_viewer_uses_live_journal_then_prefers_final_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_dir = root / "eval" / "run" / "agent-record"
            record_dir.mkdir(parents=True)
            message = {"role": "assistant", "content": "live journal action"}
            (record_dir / "messages.jsonl").write_text(
                _envelope(0, message) + "\n", encoding="utf-8"
            )

            self.assertEqual(
                find_message_files_under(str(root), "eval"),
                ["eval/run/agent-record/messages.jsonl"],
            )
            self.assertIn(
                "live journal action",
                load_messages_html(str(record_dir / "messages.jsonl")),
            )

            (record_dir / "messages.json").write_text(
                json.dumps([{"role": "assistant", "content": "final action"}]),
                encoding="utf-8",
            )
            self.assertEqual(
                find_message_files_under(str(root), "eval"),
                ["eval/run/agent-record/messages.json"],
            )

    def test_trajectory_viewer_renders_native_tool_call_linkage(self):
        with tempfile.TemporaryDirectory() as temporary:
            messages_path = Path(temporary) / "messages.json"
            messages_path.write_text(
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "content": "Checking the current state.",
                            "tool_calls": [
                                {
                                    "id": "call_00_example",
                                    "type": "function",
                                    "function": {
                                        "name": "minecraft_action",
                                        "arguments": json.dumps(
                                            {
                                                "type": "exec",
                                                "content": "mcapi state",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "content": '{"status":"received"}',
                            "tool_call_id": "call_00_example",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            rendered = load_messages_html(str(messages_path))

            self.assertIn("tool_calls", rendered)
            self.assertIn("minecraft_action", rendered)
            self.assertIn("mcapi state", rendered)
            self.assertIn("tool_call_id", rendered)
            self.assertEqual(rendered.count("call_00_example"), 2)


if __name__ == "__main__":
    unittest.main()

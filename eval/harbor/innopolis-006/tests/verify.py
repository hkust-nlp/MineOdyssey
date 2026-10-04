"""Read evaluator-owned completion; never trust an agent-written result file."""
import http.client
import json
from pathlib import Path
import socket

TASK_ID = json.loads(Path(__file__).with_name("task-spec.json").read_text())["task_id"]


def finish():
    # Collected directly from world after Harbor stops the agent container.
    return json.loads(Path("/trusted/completion.json").read_text())


def reward(completion):
    return int(
        completion.get("task_id") == TASK_ID
        and completion.get("terminal") is True
        and completion.get("success") is True
        and completion.get("terminal_reason") == "claim_done_arrived"
        and completion.get("infrastructure_error") is False
    )


def write_result(completion, output):
    (output / "completion.json").write_text(json.dumps(completion, indent=2) + "\n")
    if completion.get("infrastructure_error"):
        raise SystemExit(
            f"Minecraft trial ended: {completion.get('terminal_reason', 'unknown')}; "
            "unscored infrastructure outcome. Inspect completion.json.")

    (output / "reward.txt").write_text(str(reward(completion)) + "\n")


if __name__ == "__main__":
    write_result(finish(), Path("/logs/verifier"))

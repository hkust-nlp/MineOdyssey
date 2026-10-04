"""Render Harbor's task text from the shared navigation policy and task catalog."""
import argparse
import json
from pathlib import Path

from agent.navigation_prompt import build_navigation_system_prompt
from eval.navigation.schema import TASK_CATALOG_PATH, find_task

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "eval/harbor/innopolis-006"

INTERFACE = """## Harbor interface

Start with `nav start`; initial startup can take several minutes.

Run the `mcapi`, `xdo`, and shell commands described below inside the game
workspace using `nav exec 'COMMAND' --timeout 120`. This command waits for the
result. The timeout defaults to 30 seconds and accepts values greater than zero
up to 120 seconds; larger values are rejected. Action-workspace files persist during this trial; that workspace is
separate from `/workspace` in your agent container.

- `nav screenshot /workspace/view.png`: save the current game view for your image-viewing tool.
- `nav read-image /tmp/crop.png /workspace/crop.png`: retrieve a PNG/JPEG from the action workspace for your image-viewing tool.
- `nav state`: inspect the current player state.
- `nav exec 'mcapi --help' --timeout 120`: inspect the game command interface.
- `nav claim-done`: submit the completion claim described above.
- `nav result`: read the outcome when the task has ended.
"""


def render_instruction(task_prompt: str) -> str:
    prompt = build_navigation_system_prompt(action_protocol="tool_calls")
    # Preserve navigation policy verbatim. Only the completion command name
    # changes for the generic Harbor CLI. Its agent owns its action protocol;
    # OriginalNavigationAgent instead uses the complete formal native-tool prompt.
    policy = prompt.split("## Objective\n\n", 1)[1].split("## Actions\n\n", 1)[0]
    policy = policy.replace("`claim_done`", "`nav claim-done`")
    controls = "## Minecraft Coordinates\n\n" + prompt.split("## Minecraft Coordinates\n\n", 1)[1]
    return (task_prompt.strip() + "\n\n## Objective\n\n" + policy.rstrip()
            + "\n\n" + INTERFACE.rstrip() + "\n\n" + controls.strip() + "\n")


def task_definition(task_id: str):
    rows = json.loads(TASK_CATALOG_PATH.read_text())["tasks"]
    maps = {row["map_id"] for row in rows if row["task_id"] == task_id}
    return find_task(task_id, maps)


def expected_instruction(task_id: str = "innopolis-006") -> str:
    _, task = task_definition(task_id)
    return render_instruction(task["prompt"])


def check_instruction(directory: Path = TASK) -> None:
    task_id = json.loads((directory / "environment/world/task-spec.json").read_text())["task_id"]
    if (directory / "instruction.md").read_text() != expected_instruction(task_id):
        raise ValueError("Harbor instruction is stale; run python -m eval.harbor_agents.instructions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_instruction()
    else:
        (TASK / "instruction.md").write_text(expected_instruction())


if __name__ == "__main__":
    main()

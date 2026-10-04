from __future__ import annotations

import argparse
from typing import Any


def build_eval_arg_parser(spec: Any) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=spec.parser_description)
    parser.add_argument("--task-config", default=spec.default_task_config)
    parser.add_argument("--task-name", default=spec.default_task_name)

    parser.add_argument("--template-world-dir", default="eval/templates/template_1_21_1/world")
    parser.add_argument("--server-world-dir", default="eval/runtime/server-data/world")

    parser.add_argument("--server-data-dir", default="eval/runtime/server-data")
    parser.add_argument("--server-mods-dir", default="eval/runtime/server-mods")
    parser.add_argument("--server-entrypoint", default="scripts/entrypoints/server-entrypoint.sh")
    parser.add_argument("--server-log-file", default="eval/runtime/server-data/eval_server.log")
    parser.add_argument("--server-pid-file", default="eval/runtime/server-data/eval_server.pid")
    parser.add_argument("--mc-version", default="1.21.1")
    parser.add_argument("--neoforge-version", default="21.1.217")
    parser.add_argument("--server-memory-min", default="2G")
    parser.add_argument("--server-memory-max", default="8G")
    parser.add_argument("--server-port", type=int, default=25585)

    parser.add_argument("--player-name", default="BotCPU")
    parser.add_argument("--rcon-host", default="127.0.0.1")
    parser.add_argument("--rcon-port", type=int, default=25595)
    parser.add_argument("--rcon-password", default="minecraft")

    parser.add_argument("--task-timeout-sec", type=float, default=120.0)
    parser.add_argument("--judge-interval-sec", type=float, default=0.5)
    parser.add_argument("--wait-rcon-timeout-sec", type=float, default=180.0)
    parser.add_argument("--wait-player-timeout-sec", type=float, default=120.0)
    parser.add_argument(
        "--post-player-online-wait-sec",
        type=float,
        default=10.0,
        help="Extra wait after player is online and before init commands (default: 10s).",
    )
    parser.add_argument(
        "--post-success-wait-sec",
        type=float,
        default=5.0,
        help="Extra wait after task success before stopping agent/server (default: 5s).",
    )
    parser.add_argument(
        "--init-weather",
        default="keep",
        choices=["keep", "clear", "rain", "thunder"],
        help="Initial weather before task start. 'keep' means do not change.",
    )
    parser.add_argument(
        "--init-mob-spawning",
        default="keep",
        choices=["keep", "true", "false"],
        help="Initial gamerule doMobSpawning. 'keep' means do not change.",
    )
    parser.add_argument(
        "--init-time",
        default="",
        help="Initial time value for 'time set <value>' (e.g. day/night/noon/6000). Empty means keep.",
    )
    parser.add_argument(
        "--init-clear-existing-hostiles",
        default="false",
        choices=["true", "false"],
        help="Whether to clear common hostile mobs via RCON kill commands before player comes online.",
    )
    parser.add_argument(
        "--init-equip-distraction-mode",
        default="off",
        choices=["off", "fixed", "random"],
        help="Initial equipment distraction mode. First version supports head slot (e.g. carved_pumpkin).",
    )
    parser.add_argument(
        "--init-equip-distraction-fixed-json",
        default="",
        help='JSON object for fixed mode, e.g. {"head":"carved_pumpkin"}.',
    )
    parser.add_argument(
        "--init-equip-distraction-random-head-candidates",
        default="",
        help="Comma-separated head-slot candidates override for random mode. Empty uses OpenHA head pool.",
    )
    parser.add_argument(
        "--init-equip-distraction-level",
        default="normal",
        choices=["difficulty", "zero", "one", "easy", "middle", "hard", "normal"],
        help="Equip distraction level used when --init-equip-distraction-mode=random.",
    )
    parser.add_argument(
        "--init-inventory-distraction-mode",
        default="off",
        choices=["off", "random"],
        help="Initial inventory distraction mode (OpenHA-style random filler items in inventory slots).",
    )
    parser.add_argument(
        "--init-inventory-distraction-level",
        default="normal",
        choices=["difficulty", "zero", "one", "easy", "middle", "hard", "normal"],
        help="Inventory distraction level used when --init-inventory-distraction-mode=random.",
    )

    parser.add_argument(
        "--agent-cmd",
        default="",
        help="Command for mcbots agent loop process, e.g. 'python -m agent.main'",
    )
    parser.add_argument(
        "--agent-instruction",
        default="",
        help="Initial user instruction injected into agent conversation.",
    )
    parser.add_argument(
        "--task-instruction-file",
        default="eval/openha_assets/instructions.json",
        help="Task instruction mapping JSON (task_name -> [instruction1, ...]).",
    )
    parser.add_argument(
        "--task-instruction-mode",
        default="first",
        choices=["first", "random"],
        help="How to pick instruction from task-instruction-file when --agent-instruction is empty.",
    )
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--no-server-restart", action="store_true")
    parser.add_argument("--skip-world-restore", action="store_true")
    parser.add_argument("--skip-player-wait", action="store_true")
    parser.add_argument(
        "--openha-init-action-replay",
        default="auto",
        choices=["off", "auto"],
        help=(
            "Replay OpenHA task init_actions locally via xdo before agent start. "
            "'auto' logs and skips on replay errors."
        ),
    )
    parser.add_argument(
        "--openha-init-action-step-sleep-sec",
        type=float,
        default=0.1,
        help="Sleep between replayed OpenHA init action steps (matches OpenHA env_init pacing).",
    )
    parser.add_argument(
        "--openha-init-action-camera-pixels-per-unit",
        type=float,
        default=4.0,
        help="Approximate xdo mouse pixels per OpenHA camera action unit when replaying init_actions.",
    )

    parser.add_argument("--output-dir", default="eval/results")
    parser.add_argument("--dry-run", action="store_true")
    return parser

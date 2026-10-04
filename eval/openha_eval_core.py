from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


LogFn = Callable[[str], None]


@dataclass(frozen=True)
class PreparedTaskRuntime:
    task_cfg_all: Dict[str, Any]
    task_cfg: Dict[str, Any]
    seed_cfg: Dict[str, Any]
    spawn_pos: List[int]
    seed: int
    init_tool: str
    task_commands: List[str]
    task_overrides: Dict[str, bool]


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def prepare_task_runtime(
    *,
    task_config_file: Path,
    task_name: str,
    pick_init_tool_fn: Callable[[Dict[str, Any]], str],
    load_task_commands_fn: Callable[[Dict[str, Any]], List[str]],
    detect_task_command_overrides_fn: Callable[[List[str]], Dict[str, bool]],
    load_json_fn: Callable[[Path], Dict[str, Any]] = load_json,
) -> PreparedTaskRuntime:
    task_cfg_all = load_json_fn(task_config_file)
    if task_name not in task_cfg_all:
        raise KeyError(f"Task not found in config: {task_name}")
    task_cfg = task_cfg_all[task_name]
    if not isinstance(task_cfg, dict):
        raise RuntimeError(f"Invalid task config for {task_name}: expected object")

    seeds = task_cfg.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise RuntimeError(f"Task has no seeds[]: {task_name}")
    seed_cfg = seeds[0]
    if not isinstance(seed_cfg, dict):
        raise RuntimeError(f"Invalid seeds[0] for task {task_name}")
    if "position" not in seed_cfg:
        raise RuntimeError(f"Task seed missing position: {task_name}")
    if "seed" not in seed_cfg:
        raise RuntimeError(f"Task seed missing seed: {task_name}")
    spawn_pos = seed_cfg["position"]
    if not isinstance(spawn_pos, list) or len(spawn_pos) < 3:
        raise RuntimeError(f"Task seed position must be [x,y,z]: {task_name}")
    seed_val = seed_cfg["seed"]
    try:
        seed = int(seed_val)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Task seed is not int-like: {task_name} seed={seed_val!r}") from e

    init_tool = pick_init_tool_fn(task_cfg)
    task_commands = load_task_commands_fn(task_cfg)
    task_overrides = detect_task_command_overrides_fn(task_commands)
    return PreparedTaskRuntime(
        task_cfg_all=task_cfg_all,
        task_cfg=task_cfg,
        seed_cfg=seed_cfg,
        spawn_pos=spawn_pos,
        seed=seed,
        init_tool=init_tool,
        task_commands=task_commands,
        task_overrides=task_overrides,
    )


def load_task_instruction_candidates(
    *,
    task_name: str,
    instruction_file: Path,
    log_fn: LogFn,
) -> List[str]:
    if not instruction_file.exists():
        log_fn(f"task instruction file not found: {instruction_file}")
        return []
    try:
        data = load_json(instruction_file)
    except Exception as e:  # noqa: BLE001
        log_fn(f"failed to load task instruction file: {instruction_file} error={e}")
        return []
    alias_candidates = [task_name]
    if task_name.startswith("smelt_item:"):
        alias_candidates.append("craft_item:" + task_name.split(":", 1)[1])
    if task_name.startswith("craft_item:"):
        alias_candidates.append("craft item " + task_name.split(":", 1)[1])

    raw = None
    matched_key = None
    for key in alias_candidates:
        candidate = data.get(key)
        if isinstance(candidate, list):
            raw = candidate
            matched_key = key
            break
    if not isinstance(raw, list):
        log_fn(f"task instruction not found in file: task={task_name} file={instruction_file}")
        return []
    if matched_key and matched_key != task_name:
        log_fn(f"task instruction alias matched: task={task_name} alias={matched_key}")
    candidates = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
    if not candidates:
        log_fn(f"task instruction list is empty: task={task_name} file={instruction_file}")
    return candidates


def choose_task_instruction(
    *,
    task_name: str,
    instruction_file: Path,
    mode: str,
    log_fn: LogFn,
) -> Optional[str]:
    candidates = load_task_instruction_candidates(
        task_name=task_name,
        instruction_file=instruction_file,
        log_fn=log_fn,
    )
    if not candidates:
        return None
    if mode == "random":
        return random.choice(candidates)
    return candidates[0]


def _humanize_suffix(text: str) -> str:
    return text.replace("_", " ").strip()


def synthesize_fallback_task_instruction(task_name: str) -> str:
    # Minimal fallback used only when OpenHA instruction assets lack an entry.
    if task_name.startswith("kill_entity:"):
        target = _humanize_suffix(task_name.split(":", 1)[1])
        return f"Task objective: kill 1 {target} as fast as possible."
    if task_name.startswith("mine_block:"):
        target = _humanize_suffix(task_name.split(":", 1)[1])
        return f"Task objective: mine 1 {target} block as fast as possible."
    if task_name.startswith("craft_item:"):
        target = _humanize_suffix(task_name.split(":", 1)[1])
        return f"Task objective: craft 1 {target} as fast as possible."
    if task_name.startswith("smelt_item:"):
        target = _humanize_suffix(task_name.split(":", 1)[1])
        return f"Task objective: smelt 1 {target} as fast as possible."
    if task_name.startswith("custom:interact_with_"):
        target = _humanize_suffix(task_name.split("custom:interact_with_", 1)[1])
        return f"Task objective: interact with a {target} once as fast as possible."
    return f"Task objective: complete task '{task_name}' as fast as possible."


def resolve_agent_instruction(
    *,
    explicit_instruction: str,
    task_name: str,
    instruction_file: Path,
    instruction_mode: str,
    log_fn: LogFn,
) -> str:
    explicit = explicit_instruction.strip()
    if explicit:
        log_fn("agent instruction source: explicit --agent-instruction")
        log_fn(f"agent instruction selected: {explicit}")
        return explicit

    selected_instruction = choose_task_instruction(
        task_name=task_name,
        instruction_file=instruction_file,
        mode=instruction_mode,
        log_fn=log_fn,
    )
    if selected_instruction is None:
        fallback = synthesize_fallback_task_instruction(task_name)
        log_fn(
            "task instruction missing in file; using synthesized fallback. "
            f"task={task_name} file={instruction_file}"
        )
        log_fn(f"agent instruction selected (fallback): {fallback}")
        return fallback
    log_fn(f"agent instruction source: file={instruction_file} mode={instruction_mode}")
    log_fn(f"agent instruction selected: {selected_instruction}")
    return selected_instruction


def run_init_commands(
    *,
    rcon: Any,
    commands: List[str],
    dry_run: bool,
    log_fn: LogFn,
    execution_dimension: Optional[str] = None,
) -> None:
    dim = str(execution_dimension or "").strip().lower()
    dim_aliases = {
        "overworld": "minecraft:overworld",
        "the_nether": "minecraft:the_nether",
        "nether": "minecraft:the_nether",
        "the_end": "minecraft:the_end",
        "end": "minecraft:the_end",
    }
    mc_dimension = dim_aliases.get(dim)

    def _with_dimension(cmd: str) -> str:
        if not mc_dimension or mc_dimension == "minecraft:overworld":
            return cmd
        stripped = cmd.lstrip().lower()
        if stripped.startswith("execute in "):
            return cmd
        return f"execute in {mc_dimension} run {cmd}"

    log_fn(f"run init commands: count={len(commands)}")
    for cmd in commands:
        effective_cmd = _with_dimension(cmd)
        if dry_run:
            print(f"[dry-run] rcon: {effective_cmd}")
        else:
            log_fn(f"rcon init> {effective_cmd}")
            rcon.run(effective_cmd, timeout_sec=10.0)


def run_judge_loop(
    *,
    rcon: Any,
    player_name: str,
    objective: str,
    base_score: int,
    death_objective: str | None,
    base_death_score: int,
    timeout_sec: float,
    interval_sec: float,
    dry_run: bool,
    agent_proc: Any,
    success_reason: str,
    log_fn: LogFn,
) -> Tuple[bool, str, int]:
    log_fn(f"start judge loop: timeout={timeout_sec}s interval={interval_sec}s base_score={base_score}")
    if dry_run:
        print(
            f"[dry-run] judge loop: objective={objective} base_score={base_score} "
            f"death_objective={death_objective or '<none>'} base_death_score={base_death_score} "
            f"timeout={timeout_sec}s interval={interval_sec}s"
        )
        return False, "dry_run", base_score

    deadline = time.time() + timeout_sec
    current_score = base_score
    current_death_score = base_death_score
    while time.time() < deadline:
        if agent_proc is not None and agent_proc.poll() is not None:
            return False, "agent_exited", current_score
        current_score = rcon.get_score(player_name=player_name, objective=objective)
        if death_objective:
            current_death_score = rcon.get_score(player_name=player_name, objective=death_objective)
            if current_death_score > base_death_score:
                log_fn(
                    "judge fail (player died): "
                    f"death_base={base_death_score} death_current={current_death_score} "
                    f"score_base={base_score} score_current={current_score}"
                )
                return False, "player_died", current_score
        if current_score - base_score >= 1:
            log_fn(f"judge success: base={base_score} current={current_score}")
            return True, success_reason, current_score
        time.sleep(interval_sec)
    log_fn(f"judge timeout: base={base_score} current={current_score}")
    return False, "timeout", current_score

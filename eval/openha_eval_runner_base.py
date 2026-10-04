#!/usr/bin/env python3
"""
Minimal OpenHA-style mine block evaluator for mcbots.

Scope:
- Single task (initial port target): mine_block:dirt
- Uses mcbots agent loop command as an external process
- Uses local RCON for reset/init/judge
- Keeps logic simple and explicit
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openha_eval_cli import build_eval_arg_parser
from openha_eval_core import (
    choose_task_instruction as core_choose_task_instruction,
    load_task_instruction_candidates as core_load_task_instruction_candidates,
    prepare_task_runtime as core_prepare_task_runtime,
    resolve_agent_instruction as core_resolve_agent_instruction,
    run_init_commands as core_run_init_commands,
    run_judge_loop as core_run_judge_loop,
)
from openha_eval_family import (
    EvalFamilySpec,
    build_family_extra_init_commands,
    build_player_init_commands as family_build_player_init_commands,
    build_pre_player_init_commands as family_build_pre_player_init_commands,
    detect_task_command_overrides as family_detect_task_command_overrides,
    load_task_commands as family_load_task_commands,
    pick_init_tool as family_pick_init_tool,
    resolve_distraction_level_for_task,
    resolve_openha_init_actions_for_task,
    resolve_score_criterion_for_task,
    resolve_summon_entity_for_task,
    sample_family_spawn_offset,
)
from openha_eval_rcon import RconClient


def log_info(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][eval] {message}", flush=True)


INTERNAL_DEATH_OBJECTIVE = "oha_deaths"
INTERNAL_DEATH_CRITERION = "deathCount"
EVAL_SUCCESS_FORCE_KEEP_MARKER = "__MCBOTS_EVAL_SUCCESS_FORCE_KEEP_5__"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_compact_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_json(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return "<log missing>"
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _truthy_openha_action_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _resolve_local_xdo_command() -> List[str]:
    xdo_in_path = shutil.which("xdo")
    if xdo_in_path:
        return [xdo_in_path]
    repo_root = Path(__file__).resolve().parents[1]
    fallback = repo_root / "scripts" / "runtime" / "xdo"
    return [str(fallback)]


def _resolve_init_action_exec_cwd() -> Optional[Path]:
    workspace_root = os.environ.get("MCBOTS_WORKSPACE_ROOT", "").strip()
    if workspace_root:
        p = Path(workspace_root)
        if p.exists():
            return p
    runtime_cfg = os.environ.get("MCBOTS_RUNTIME_CONFIG", "").strip()
    if runtime_cfg:
        p = Path(runtime_cfg).expanduser().resolve()
        if p.exists():
            return p.parent
    return None


def _run_local_cli(
    *,
    argv: List[str],
    dry_run: bool,
    cwd: Optional[Path],
) -> None:
    if dry_run:
        cwd_suffix = f" (cwd={cwd})" if cwd is not None else ""
        print(f"[dry-run] local cli:{cwd_suffix} {' '.join(shlex.quote(x) for x in argv)}")
        return
    proc = subprocess.run(argv, cwd=str(cwd) if cwd is not None else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Local CLI command failed for OpenHA init_actions replay: "
            f"argv={argv!r} cwd={str(cwd) if cwd else None!r} rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def replay_openha_init_actions_via_xdo(
    *,
    actions: List[Dict[str, Any]],
    dry_run: bool,
    step_sleep_sec: float,
    camera_pixels_per_unit: float,
) -> None:
    if not actions:
        return
    xdo = _resolve_local_xdo_command()
    cwd = _resolve_init_action_exec_cwd()
    key_map = {
        "forward": "w",
        "back": "s",
        "left": "a",
        "right": "d",
        "jump": "space",
        "sneak": "Shift_L",
        "sprint": "Control_L",
        "inventory": "e",
        "drop": "q",
    }

    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            continue

        # Replay in a deterministic order that approximates the OpenHA raw-action frame:
        # slot selection -> movement/meta keys -> camera -> clicks.
        step_cmds: List[List[str]] = []

        for slot_idx in range(1, 10):
            key = f"hotbar.{slot_idx}"
            if _truthy_openha_action_flag(action.get(key)):
                step_cmds.append([*xdo, "key", str(slot_idx)])

        for field, key_name in key_map.items():
            if _truthy_openha_action_flag(action.get(field)):
                step_cmds.append([*xdo, "key", key_name])

        raw_camera = action.get("camera")
        if isinstance(raw_camera, (list, tuple)) and len(raw_camera) >= 2:
            try:
                camera_x = float(raw_camera[0])
                camera_y = float(raw_camera[1])
            except Exception:
                camera_x = 0.0
                camera_y = 0.0
            dx = int(round(camera_x * camera_pixels_per_unit))
            dy = int(round(camera_y * camera_pixels_per_unit))
            if dx != 0 or dy != 0:
                step_cmds.append([*xdo, "mousemove_relative", "--", str(dx), str(dy)])

        if _truthy_openha_action_flag(action.get("attack")):
            step_cmds.append([*xdo, "click", "1"])
        if _truthy_openha_action_flag(action.get("use")):
            step_cmds.append([*xdo, "click", "3"])

        for argv in step_cmds:
            _run_local_cli(argv=argv, dry_run=dry_run, cwd=cwd)

        # OpenHA env_init sleeps 0.1s before each step; we use a post-step sleep for equivalent pacing.
        if step_sleep_sec > 0 and idx < len(actions) - 1:
            if dry_run:
                print(f"[dry-run] sleep {step_sleep_sec:.3f}s between OpenHA init action steps")
            else:
                time.sleep(step_sleep_sec)


@dataclass
class EpisodeResult:
    task_name: str
    player_name: str
    success: bool
    reason: str
    start_time: str
    end_time: str
    duration_sec: float
    base_score: int
    final_score: int
    delta_score: int
    timeout_sec: float
    judge_interval_sec: float
    dry_run: bool
    template_world_dir: str
    server_world_dir: str
    seed: Optional[int] = None
    spawn_position: Optional[List[int]] = None
    init_tool: Optional[str] = None
    agent_cmd: Optional[str] = None
    server_entrypoint: Optional[str] = None
    server_pid: Optional[int] = None
    server_log_file: Optional[str] = None


def parse_args(spec: EvalFamilySpec) -> argparse.Namespace:
    return build_eval_arg_parser(spec).parse_args()


def read_pid(pid_file: Path) -> Optional[int]:
    if not pid_file.exists():
        return None
    text = pid_file.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def restore_world(template_world_dir: Path, server_world_dir: Path, dry_run: bool) -> None:
    log_info(f"restore world: {template_world_dir} -> {server_world_dir}")
    if dry_run:
        print(f"[dry-run] restore world: {template_world_dir} -> {server_world_dir}")
        return
    if not template_world_dir.exists():
        raise FileNotFoundError(f"Template world not found: {template_world_dir}")

    if server_world_dir.exists():
        last_err: Optional[Exception] = None
        for _ in range(5):
            try:
                shutil.rmtree(server_world_dir)
                last_err = None
                break
            except FileNotFoundError:
                last_err = None
                break
            except OSError as e:
                last_err = e
                time.sleep(0.3)
        if last_err is not None:
            raise RuntimeError(
                "Failed to clear existing world directory. "
                f"path={server_world_dir} error={last_err}. "
                "This usually means another server process is using the same directory. "
                "Use an isolated server-data path for eval."
            ) from last_err

    shutil.copytree(template_world_dir, server_world_dir)


def stop_server(pid_file: Path, dry_run: bool, grace_sec: float = 20.0) -> None:
    pid = read_pid(pid_file)
    if pid is None:
        log_info(f"stop server: no pid file ({pid_file}), skip")
        return

    log_info(f"stop server: pid={pid}")
    if dry_run:
        print(f"[dry-run] stop server pid={pid} from {pid_file}")
        return

    if not is_pid_running(pid):
        pid_file.unlink(missing_ok=True)
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    except Exception:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        if not is_pid_running(pid):
            pid_file.unlink(missing_ok=True)
            return
        time.sleep(0.5)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
    log_info(f"stop server: force-killed pid={pid}")


def ensure_server_layout(server_data_dir: Path, server_mods_dir: Path, dry_run: bool) -> None:
    log_info(f"ensure server layout: data={server_data_dir} mods={server_mods_dir}")
    server_properties = server_data_dir / "server.properties"
    mods_path = server_data_dir / "mods"

    if dry_run:
        print(f"[dry-run] ensure server dirs: {server_data_dir} {server_mods_dir}")
        if not server_properties.exists():
            print(f"[dry-run] would copy config/server.properties -> {server_properties}")
        if not mods_path.exists() and not mods_path.is_symlink():
            print(f"[dry-run] would symlink mods dir: {mods_path} -> {server_mods_dir}")
        return

    server_data_dir.mkdir(parents=True, exist_ok=True)
    server_mods_dir.mkdir(parents=True, exist_ok=True)

    if not server_properties.exists():
        template = Path("config/server.properties")
        if template.exists():
            shutil.copy(template, server_properties)

    if not mods_path.exists() and not mods_path.is_symlink():
        mods_path.symlink_to(server_mods_dir.resolve(), target_is_directory=True)


def update_server_properties(
    server_properties: Path,
    server_port: int,
    rcon_port: int,
    rcon_password: str,
    dry_run: bool,
) -> None:
    log_info(
        "update server.properties: "
        f"path={server_properties} server-port={server_port} rcon.port={rcon_port}"
    )
    if dry_run:
        print(
            "[dry-run] update server.properties: "
            f"server-port={server_port} enable-rcon=true rcon.port={rcon_port}"
        )
        return

    updates = {
        "server-port": str(server_port),
        "enable-rcon": "true",
        "rcon.port": str(rcon_port),
        "rcon.password": rcon_password,
    }

    lines: List[str] = []
    if server_properties.exists():
        lines = server_properties.read_text(encoding="utf-8", errors="replace").splitlines()

    output: List[str] = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _value = line.split("=", 1)
        key = key.strip()
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    server_properties.parent.mkdir(parents=True, exist_ok=True)
    server_properties.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def start_server(
    server_entrypoint: Path,
    server_data_dir: Path,
    server_log_file: Path,
    server_pid_file: Path,
    mc_version: str,
    neoforge_version: str,
    memory_min: str,
    memory_max: str,
    server_port: int,
    rcon_port: int,
    rcon_password: str,
    dry_run: bool,
) -> None:
    log_info(
        "start server process: "
        f"entrypoint={server_entrypoint} cwd={server_data_dir}"
    )
    if dry_run:
        print(
            "[dry-run] start server: "
            f"entrypoint={server_entrypoint} cwd={server_data_dir} log={server_log_file}"
        )
        return

    if not server_entrypoint.exists():
        raise FileNotFoundError(f"Server entrypoint not found: {server_entrypoint}")

    server_log_file.parent.mkdir(parents=True, exist_ok=True)
    server_pid_file.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "MC_VERSION": mc_version,
            "NEOFORGE_VERSION": neoforge_version,
            "MEMORY_MIN": memory_min,
            "MEMORY_MAX": memory_max,
            "SERVER_PORT": str(server_port),
            "RCON_PORT": str(rcon_port),
            "ENABLE_RCON": "true",
            "RCON_PASSWORD": rcon_password,
        }
    )

    with server_log_file.open("a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            ["bash", str(server_entrypoint.resolve())],
            cwd=server_data_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    server_pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    log_info(f"server started pid={proc.pid} log={server_log_file}")
    time.sleep(1.0)
    if proc.poll() is not None:
        tail = read_tail(server_log_file, lines=80)
        raise RuntimeError(f"Server exited immediately. log_tail:\n{tail}")


def start_agent(
    agent_cmd: str,
    dry_run: bool,
    extra_env: Optional[Dict[str, str]] = None,
) -> Optional[subprocess.Popen]:
    if not agent_cmd:
        return None
    if dry_run:
        print(f"[dry-run] start agent: {agent_cmd}")
        if extra_env and extra_env.get("MCBOTS_INITIAL_USER_INPUT"):
            print(f"[dry-run] agent initial user input: {extra_env['MCBOTS_INITIAL_USER_INPUT']}")
        return None
    args = shlex.split(agent_cmd)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(args, env=env, start_new_session=True)


def stop_agent(agent_proc: Optional[subprocess.Popen], dry_run: bool) -> None:
    if agent_proc is None:
        return
    if dry_run:
        print("[dry-run] stop agent process")
        return
    if agent_proc.poll() is not None:
        return
    try:
        agent_proc.terminate()
    except Exception:
        pass
    try:
        agent_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(agent_proc.pid)
        except Exception:
            pgid = None
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
            try:
                agent_proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
        else:
            agent_proc.kill()
        agent_proc.wait(timeout=5)


def pick_init_tool(task_cfg: Dict, spec: EvalFamilySpec) -> str:
    return family_pick_init_tool(task_cfg, fallback_tool=spec.default_tool_fallback)


def load_task_commands(task_cfg: Dict) -> List[str]:
    return family_load_task_commands(task_cfg)


def detect_task_command_overrides(task_commands: List[str]) -> Dict[str, bool]:
    return family_detect_task_command_overrides(task_commands)


def build_pre_player_init_commands(
    init_weather: str,
    init_mob_spawning: str,
    init_time: str,
    init_clear_existing_hostiles: str,
    task_overrides: Dict[str, bool],
) -> List[str]:
    return family_build_pre_player_init_commands(
        init_weather=init_weather,
        init_mob_spawning=init_mob_spawning,
        init_time=init_time,
        init_clear_existing_hostiles=init_clear_existing_hostiles,
        task_overrides=task_overrides,
    )


def build_init_commands(
    spec: EvalFamilySpec,
    player_name: str,
    spawn_pos: List[int],
    init_tool: str,
    init_equip_distraction_mode: str = "off",
    init_equip_distraction_level: str = "normal",
    init_equip_distraction_fixed_json: str = "",
    init_equip_distraction_random_head_candidates: str = "",
    init_inventory_distraction_mode: str = "off",
    init_inventory_distraction_level: str = "normal",
    summon_offset: Optional[Tuple[float, float]] = None,
    summon_entity_override: Optional[str] = None,
) -> List[str]:
    return family_build_player_init_commands(
        spec=spec,
        player_name=player_name,
        spawn_pos=spawn_pos,
        init_tool=init_tool,
        equip_distraction_mode=init_equip_distraction_mode,
        equip_distraction_level=init_equip_distraction_level,
        equip_distraction_fixed_json=init_equip_distraction_fixed_json,
        equip_distraction_random_head_candidates=init_equip_distraction_random_head_candidates,
        inventory_distraction_mode=init_inventory_distraction_mode,
        inventory_distraction_level=init_inventory_distraction_level,
        summon_offset=summon_offset,
        summon_entity_override=summon_entity_override,
    )


def load_task_instruction_candidates(task_name: str, instruction_file: Path) -> List[str]:
    return core_load_task_instruction_candidates(
        task_name=task_name,
        instruction_file=instruction_file,
        log_fn=log_info,
    )


def choose_task_instruction(
    task_name: str,
    instruction_file: Path,
    mode: str,
) -> Optional[str]:
    return core_choose_task_instruction(
        task_name=task_name,
        instruction_file=instruction_file,
        mode=mode,
        log_fn=log_info,
    )


def run_init_commands(
    rcon: RconClient,
    commands: List[str],
    dry_run: bool,
    execution_dimension: Optional[str] = None,
) -> None:
    core_run_init_commands(
        rcon=rcon,
        commands=commands,
        dry_run=dry_run,
        log_fn=log_info,
        execution_dimension=execution_dimension,
    )


def run_judge_loop(
    rcon: RconClient,
    player_name: str,
    objective: str,
    success_reason: str,
    base_score: int,
    death_objective: Optional[str],
    base_death_score: int,
    timeout_sec: float,
    interval_sec: float,
    dry_run: bool,
    agent_proc: Optional[subprocess.Popen],
) -> Tuple[bool, str, int]:
    return core_run_judge_loop(
        rcon=rcon,
        player_name=player_name,
        objective=objective,
        base_score=base_score,
        death_objective=death_objective,
        base_death_score=base_death_score,
        timeout_sec=timeout_sec,
        interval_sec=interval_sec,
        dry_run=dry_run,
        agent_proc=agent_proc,
        success_reason=success_reason,
        log_fn=log_info,
    )


def write_result(result: EpisodeResult, output_dir: Path) -> Path:
    layout = os.environ.get("MCBOTS_SINGLE_TASK_RESULT_LAYOUT", "").strip().lower()
    result_stem = f"{utc_compact_ts()}_{result.task_name.replace(':', '_')}"
    configured_run_id = os.environ.get("MCBOTS_SINGLE_TASK_RUN_ID", "").strip()
    run_root = os.environ.get("MCBOTS_SINGLE_TASK_RUN_ROOT", "").strip()

    if layout in {
        "single_task_summary",
        "single_task_summary_dir",
        "single_task_dir",
        "summary_dir",
    }:
        if configured_run_id:
            result_stem = configured_run_id
        if not run_root:
            run_root = "single_tasks"
        if run_root in {".", "./"}:
            run_dir = output_dir / result_stem
        else:
            run_root_path = Path(run_root)
            if run_root_path.is_absolute():
                run_dir = run_root_path / result_stem
            else:
                run_dir = output_dir / run_root_path / result_stem
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "summary.json"
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{result_stem}.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(asdict(result), f, ensure_ascii=False, indent=2)
    return path


def run_family_eval_main(spec: EvalFamilySpec) -> None:
    args = parse_args(spec)
    log_info(
        f"start task={args.task_name} player={args.player_name} "
        f"server_port={args.server_port} rcon={args.rcon_host}:{args.rcon_port}"
    )
    prepared_task = core_prepare_task_runtime(
        task_config_file=Path(args.task_config),
        task_name=args.task_name,
        pick_init_tool_fn=lambda task_cfg: pick_init_tool(task_cfg, spec),
        load_task_commands_fn=load_task_commands,
        detect_task_command_overrides_fn=detect_task_command_overrides,
        load_json_fn=load_json,
    )
    task_cfg_all = prepared_task.task_cfg_all
    task_cfg = prepared_task.task_cfg
    score_criterion = resolve_score_criterion_for_task(spec, args.task_name, task_cfg)
    summon_entity = resolve_summon_entity_for_task(spec, args.task_name, task_cfg)

    if args.no_server_restart and not args.skip_world_restore and not args.dry_run:
        raise RuntimeError(
            "--no-server-restart 与 --skip-world-restore 不能同时缺省："
            "当前不会在运行中的服务端上直接覆盖世界。"
        )

    seed_cfg = prepared_task.seed_cfg
    spawn_pos = prepared_task.spawn_pos
    seed = prepared_task.seed
    task_dimension = str(seed_cfg.get("dimension") or task_cfg.get("dimension") or "overworld")
    init_tool = prepared_task.init_tool
    summon_offset = sample_family_spawn_offset(spec) if spec.uses_summon_target else None
    task_commands = prepared_task.task_commands
    task_overrides = prepared_task.task_overrides

    active_overrides = [key for key, enabled in task_overrides.items() if enabled]
    if active_overrides:
        log_info(f"task commands override external init params: {', '.join(active_overrides)}")

    start_ts = time.time()
    start_iso = now_iso()
    base_score = 0
    final_score = 0
    success = False
    reason = "unknown"
    agent_proc: Optional[subprocess.Popen] = None

    server_data_dir = Path(args.server_data_dir)
    server_mods_dir = Path(args.server_mods_dir)
    server_properties = server_data_dir / "server.properties"
    server_entrypoint = Path(args.server_entrypoint)
    server_log_file = Path(args.server_log_file)
    server_pid_file = Path(args.server_pid_file)

    try:
        ensure_server_layout(
            server_data_dir=server_data_dir,
            server_mods_dir=server_mods_dir,
            dry_run=args.dry_run,
        )

        if not args.no_server_restart:
            stop_server(pid_file=server_pid_file, dry_run=args.dry_run)

        if args.skip_world_restore:
            log_info("skip-world-restore enabled")
        else:
            restore_world(
                template_world_dir=Path(args.template_world_dir),
                server_world_dir=Path(args.server_world_dir),
                dry_run=args.dry_run,
            )

        update_server_properties(
            server_properties=server_properties,
            server_port=args.server_port,
            rcon_port=args.rcon_port,
            rcon_password=args.rcon_password,
            dry_run=args.dry_run,
        )

        if not args.no_server_restart:
            start_server(
                server_entrypoint=server_entrypoint,
                server_data_dir=server_data_dir,
                server_log_file=server_log_file,
                server_pid_file=server_pid_file,
                mc_version=args.mc_version,
                neoforge_version=args.neoforge_version,
                memory_min=args.server_memory_min,
                memory_max=args.server_memory_max,
                server_port=args.server_port,
                rcon_port=args.rcon_port,
                rcon_password=args.rcon_password,
                dry_run=args.dry_run,
            )

        rcon = RconClient(
            host=args.rcon_host,
            port=args.rcon_port,
            password=args.rcon_password,
            log_fn=log_info,
        )

        if not args.dry_run:
            log_info("wait RCON ready...")
            rcon.wait_ready(timeout_sec=args.wait_rcon_timeout_sec, interval_sec=1.0)
            log_info("RCON ready")
            pre_player_init_commands = build_pre_player_init_commands(
                init_weather=args.init_weather,
                init_mob_spawning=args.init_mob_spawning,
                init_time=args.init_time,
                init_clear_existing_hostiles=args.init_clear_existing_hostiles,
                task_overrides=task_overrides,
            )
            if pre_player_init_commands:
                log_info("run pre-player env init commands (before player online)")
                run_init_commands(rcon=rcon, commands=pre_player_init_commands, dry_run=False)
            if args.skip_player_wait:
                log_info("skip-player-wait enabled")
            else:
                log_info(f"wait player online: {args.player_name}")
                rcon.wait_player_online(
                    player_name=args.player_name,
                    timeout_sec=args.wait_player_timeout_sec,
                    interval_sec=1.0,
                )
                log_info(f"player online: {args.player_name}")
                post_wait_sec = max(float(args.post_player_online_wait_sec), 0.0)
                if post_wait_sec > 0:
                    log_info(f"post-player-online settle wait: {post_wait_sec:.1f}s")
                    time.sleep(post_wait_sec)

        if args.dry_run:
            print(f"[dry-run] ensure objective: {spec.score_objective} {score_criterion}")
            base_score = 0
            base_death_score = 0
        else:
            rcon.ensure_score_objective(objective=spec.score_objective, criterion=score_criterion)
            rcon.ensure_score_objective(
                objective=INTERNAL_DEATH_OBJECTIVE,
                criterion=INTERNAL_DEATH_CRITERION,
            )
            base_death_score = rcon.get_score(
                player_name=args.player_name,
                objective=INTERNAL_DEATH_OBJECTIVE,
            )

        resolved_equip_distraction_level = resolve_distraction_level_for_task(
            task_cfg=task_cfg,
            requested_level=args.init_equip_distraction_level,
            kind="equip",
        )
        resolved_inventory_distraction_level = resolve_distraction_level_for_task(
            task_cfg=task_cfg,
            requested_level=args.init_inventory_distraction_level,
            kind="inventory",
        )

        init_commands = build_init_commands(
            spec=spec,
            player_name=args.player_name,
            spawn_pos=spawn_pos,
            init_tool=init_tool,
            init_equip_distraction_mode=args.init_equip_distraction_mode,
            init_equip_distraction_level=resolved_equip_distraction_level,
            init_equip_distraction_fixed_json=args.init_equip_distraction_fixed_json,
            init_equip_distraction_random_head_candidates=args.init_equip_distraction_random_head_candidates,
            init_inventory_distraction_mode=args.init_inventory_distraction_mode,
            init_inventory_distraction_level=resolved_inventory_distraction_level,
            summon_offset=summon_offset,
            summon_entity_override=summon_entity,
        )
        family_extra_init_commands = build_family_extra_init_commands(
            spec=spec,
            task_cfg=task_cfg,
            player_name=args.player_name,
            inventory_distraction_mode=args.init_inventory_distraction_mode,
            inventory_distraction_level=resolved_inventory_distraction_level,
        )
        if family_extra_init_commands:
            log_info(f"append family extra init commands: count={len(family_extra_init_commands)}")
            init_commands.extend(family_extra_init_commands)
        if task_commands:
            log_info(f"append task commands from task config: count={len(task_commands)}")
            init_commands.extend(task_commands)
        run_init_commands(
            rcon=rcon,
            commands=init_commands,
            dry_run=args.dry_run,
            execution_dimension=task_dimension,
        )

        if args.openha_init_action_replay != "off":
            openha_init_actions, openha_init_actions_source = resolve_openha_init_actions_for_task(
                spec=spec,
                task_cfg=task_cfg,
                seed=seed,
                spawn_pos=spawn_pos,
            )
            if openha_init_actions:
                log_info(
                    "replay OpenHA init_actions locally via xdo: "
                    f"steps={len(openha_init_actions)} source={openha_init_actions_source}"
                )
                try:
                    replay_openha_init_actions_via_xdo(
                        actions=openha_init_actions,
                        dry_run=args.dry_run,
                        step_sleep_sec=max(0.0, float(args.openha_init_action_step_sleep_sec)),
                        camera_pixels_per_unit=float(args.openha_init_action_camera_pixels_per_unit),
                    )
                except Exception as e:
                    log_info(
                        "warning: OpenHA init_actions replay skipped after local xdo error "
                        f"(mode=auto): {e}"
                    )
            else:
                log_info(f"OpenHA init_actions replay: none ({openha_init_actions_source})")

        if args.dry_run:
            base_score = 0
        else:
            base_score = rcon.get_score(player_name=args.player_name, objective=spec.score_objective)

        if args.skip_agent:
            log_info("skip-agent enabled; judge will run without launching agent process")
        else:
            agent_instruction = core_resolve_agent_instruction(
                explicit_instruction=args.agent_instruction,
                task_name=args.task_name,
                instruction_file=Path(args.task_instruction_file),
                instruction_mode=args.task_instruction_mode,
                log_fn=log_info,
            )
            log_info(f"start agent process: {args.agent_cmd}")
            agent_proc = start_agent(
                agent_cmd=args.agent_cmd,
                dry_run=args.dry_run,
                extra_env={
                    "MCBOTS_INITIAL_USER_INPUT": agent_instruction,
                    "MCBOTS_EVAL_MODE": "1",
                },
            )

        success, reason, final_score = run_judge_loop(
            rcon=rcon,
            player_name=args.player_name,
            objective=spec.score_objective,
            success_reason=spec.success_reason,
            base_score=base_score,
            death_objective=None if args.dry_run else INTERNAL_DEATH_OBJECTIVE,
            base_death_score=base_death_score,
            timeout_sec=args.task_timeout_sec,
            interval_sec=args.judge_interval_sec,
            dry_run=args.dry_run,
            agent_proc=agent_proc,
        )
        if success:
            if not args.dry_run:
                marker_cmd = f'tellraw {args.player_name} {{"text":"{EVAL_SUCCESS_FORCE_KEEP_MARKER}"}}'
                marker_res = rcon.run_raw(marker_cmd, timeout_sec=10.0)
                if marker_res.returncode != 0:
                    log_info(
                        "warning: failed to send eval-success force-keep marker; "
                        f"stderr={marker_res.stderr!r}"
                    )
            post_success_wait_sec = max(float(args.post_success_wait_sec), 0.0)
            if post_success_wait_sec > 0:
                log_info(f"post-success settle wait: {post_success_wait_sec:.1f}s")
                time.sleep(post_success_wait_sec)

    finally:
        stop_agent(agent_proc=agent_proc, dry_run=args.dry_run)

    end_iso = now_iso()
    duration = time.time() - start_ts
    result = EpisodeResult(
        task_name=args.task_name,
        player_name=args.player_name,
        success=success,
        reason=reason,
        start_time=start_iso,
        end_time=end_iso,
        duration_sec=duration,
        base_score=base_score,
        final_score=final_score,
        delta_score=final_score - base_score,
        timeout_sec=args.task_timeout_sec,
        judge_interval_sec=args.judge_interval_sec,
        dry_run=args.dry_run,
        template_world_dir=args.template_world_dir,
        server_world_dir=args.server_world_dir,
        seed=seed,
        spawn_position=spawn_pos,
        init_tool=init_tool,
        agent_cmd=args.agent_cmd or None,
        server_entrypoint=args.server_entrypoint,
        server_pid=read_pid(server_pid_file),
        server_log_file=str(server_log_file),
    )
    result_path = write_result(result=result, output_dir=Path(args.output_dir))
    print(f"[done] result saved: {result_path}")
    print(
        f"[done] success={result.success} reason={result.reason} "
        f"delta_score={result.delta_score}"
    )


def _direct_run_error() -> int:
    print(
        "This is a shared base runner module. Use a family wrapper script "
        "(e.g. eval/kill_sheep_eval_runner.py or eval/mine_block_eval_runner.py)."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_direct_run_error())

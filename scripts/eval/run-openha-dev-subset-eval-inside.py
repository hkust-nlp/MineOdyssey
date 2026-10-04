#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


RESULT_PATH_RE = re.compile(r"^\[done\] result saved: (?P<path>.+)$")
RECORD_DIR_RE = re.compile(r"record directory:\s*(?P<path>.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class TaskSpec:
    index: int
    name: str
    dimension: str


@dataclass
class TaskRunResult:
    index: int
    task_name: str
    task_dimension: str
    rc: int
    timed_out: bool
    duration_sec: float
    result_path: Optional[str]
    eval_success: Optional[bool]
    eval_reason: Optional[str]
    delta_score: Optional[int]
    log_path: str
    runtime_dir: str
    record_dir: Optional[str]
    status: str
    dry_run: bool


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def as_bool_text(value: bool) -> str:
    return "true" if value else "false"


def parse_key_value(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid empty key in: {item}")
        out[key] = value
    return out


def canonicalize_task_name_compat(task_name: str) -> str:
    task_name = task_name.strip()
    if task_name.startswith("craft item "):
        suffix = task_name[len("craft item ") :].strip().replace(" ", "_")
        return f"craft_item:{suffix}"
    return task_name


def infer_family(task_name: str) -> str:
    if task_name.startswith("kill_entity:"):
        return "kill_entity"
    if task_name.startswith("mine_block:"):
        return "mine_block"
    if task_name.startswith("craft_item:"):
        return "craft_item"
    if task_name.startswith("smelt_item:"):
        return "smelt_item"
    if task_name.startswith("interact_block:") or task_name.startswith("custom:interact_with_"):
        return "interact_block"
    return "unknown"


def normalize_dimension(raw_dim: str) -> str:
    dim = (raw_dim or "").strip().lower()
    if not dim:
        return "overworld"
    if dim in {"overworld", "minecraft:overworld"}:
        return "overworld"
    if dim in {"the_nether", "nether", "minecraft:the_nether"}:
        return "the_nether"
    if dim in {"the_end", "end", "minecraft:the_end"}:
        return "the_end"
    return dim


def infer_task_dimension(task_name: str, imported_task_configs_dir: Path) -> str:
    task_name = canonicalize_task_name_compat(task_name)
    family = infer_family(task_name)
    if family == "unknown":
        return "overworld"
    cfg_path = imported_task_configs_dir / f"{family}.json"
    if not cfg_path.exists():
        return "overworld"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return "overworld"
    if not isinstance(cfg, dict):
        return "overworld"
    task_cfg = cfg.get(task_name)
    if not isinstance(task_cfg, dict):
        return "overworld"
    direct_dim = task_cfg.get("dimension")
    if isinstance(direct_dim, str) and direct_dim.strip():
        return normalize_dimension(direct_dim)
    seeds = task_cfg.get("seeds")
    if isinstance(seeds, list):
        for seed in seeds:
            if isinstance(seed, dict):
                dim = seed.get("dimension")
                if isinstance(dim, str) and dim.strip():
                    return normalize_dimension(dim)
    return "overworld"


def make_task_slug(task_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_name.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "task"


def load_subset_tasks(subset_file: Path, subset_key: str) -> List[str]:
    with subset_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {subset_file}")
    subset = data.get(subset_key)
    if not isinstance(subset, list):
        raise RuntimeError(f"Subset '{subset_key}' not found or not a list in {subset_file}")
    tasks = []
    for item in subset:
        if isinstance(item, str) and item.strip():
            tasks.append(canonicalize_task_name_compat(item))
    if not tasks:
        raise RuntimeError(f"Subset '{subset_key}' has no tasks in {subset_file}")
    return tasks


def frame_filter_profile_env(profile: str) -> Dict[str, str]:
    if profile == "default":
        return {}
    if profile == "minimal":
        return {
            "MCBOTS_FRAME_DEDUP_ENABLE": "true",
            "MCBOTS_FRAME_APPROX_DEDUP_ENABLE": "false",
            "MCBOTS_FRAME_ADAPTIVE_BUDGET_ENABLE": "false",
            "MCBOTS_FRAME_FILTER_TELEMETRY": "true",
        }
    if profile == "aggressive-smart":
        return {
            "MCBOTS_FRAME_DEDUP_ENABLE": "true",
            "MCBOTS_FRAME_DEDUP_FORCE_KEEP_AFTER_EVENT": "1",
            "MCBOTS_FRAME_APPROX_DEDUP_ENABLE": "true",
            "MCBOTS_FRAME_APPROX_SIG_WIDTH": "24",
            "MCBOTS_FRAME_APPROX_SIG_HEIGHT": "14",
            "MCBOTS_FRAME_APPROX_TILE_COLS": "4",
            "MCBOTS_FRAME_APPROX_TILE_ROWS": "3",
            "MCBOTS_FRAME_APPROX_CENTER_ROI_ENABLE": "true",
            "MCBOTS_FRAME_APPROX_CENTER_ROI_WIDTH_RATIO": "0.60",
            "MCBOTS_FRAME_APPROX_CENTER_ROI_HEIGHT_RATIO": "0.60",
            "MCBOTS_FRAME_APPROX_CENTER_ROI_WEIGHT": "2.8",
            "MCBOTS_FRAME_APPROX_DIFF_THRESHOLD": "3.0",
            "MCBOTS_FRAME_APPROX_PEAK_TILE_GUARD_THRESHOLD": "10.0",
            "MCBOTS_FRAME_ADAPTIVE_BUDGET_ENABLE": "false",
            "MCBOTS_FRAME_FILTER_TELEMETRY": "true",
        }
    raise RuntimeError(f"Unsupported vision profile: {profile}")


def parse_json_object(name: str, raw: str) -> Dict[str, object]:
    try:
        loaded = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid JSON for {name}: {e}") from e
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return loaded


def parse_result_path_from_log(log_path: Path) -> Optional[str]:
    if not log_path.exists():
        return None
    result_path: Optional[str] = None
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RESULT_PATH_RE.match(line.rstrip("\n"))
            if m:
                result_path = m.group("path").strip()
    return result_path


def resolve_result_path(raw_result_path: str, project_root: Path) -> Path:
    candidate = Path(raw_result_path)
    if candidate.is_absolute():
        return candidate
    return (project_root / candidate).resolve()


def normalize_project_path(raw_path: str, project_root: Path) -> str:
    path_text = raw_path.strip()
    if not path_text:
        return path_text
    candidate = Path(path_text)
    if candidate.is_absolute():
        workspace_prefix = "/workspace/mcbots/"
        if str(project_root) != "/workspace/mcbots" and path_text.startswith(workspace_prefix):
            mapped = project_root / path_text[len(workspace_prefix) :]
            return str(mapped.resolve())
        return str(candidate)
    return str((project_root / candidate).resolve())


def parse_record_dir_from_log(log_path: Path, project_root: Path) -> Optional[str]:
    if not log_path.exists():
        return None
    record_dir: Optional[str] = None
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RECORD_DIR_RE.search(line.rstrip("\n"))
            if m:
                record_dir = normalize_project_path(m.group("path"), project_root)
    return record_dir


def relocate_single_task_result_dir(output_dir: Path, task_run_id: str) -> Optional[tuple[Path, Path]]:
    src_dir = output_dir / "single_tasks" / task_run_id
    dst_dir = output_dir / task_run_id
    if (not src_dir.exists()) or dst_dir.exists():
        return None
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_dir), str(dst_dir))
    return src_dir, dst_dir


def remap_path_after_relocate(path_text: Optional[str], src_dir: Path, dst_dir: Path) -> Optional[str]:
    if not path_text:
        return path_text
    raw = path_text.strip()
    if not raw:
        return raw
    src_prefix = str(src_dir.resolve())
    dst_prefix = str(dst_dir.resolve())
    norm = str(Path(raw).resolve())
    if norm == src_prefix:
        return dst_prefix
    prefix = src_prefix + os.sep
    if norm.startswith(prefix):
        return dst_prefix + norm[len(src_prefix) :]
    return raw


def remap_json_paths_after_relocate(value: object, src_dir: Path, dst_dir: Path) -> object:
    if isinstance(value, str):
        mapped = remap_path_after_relocate(value, src_dir=src_dir, dst_dir=dst_dir)
        return mapped if mapped is not None else value
    if isinstance(value, list):
        return [remap_json_paths_after_relocate(item, src_dir=src_dir, dst_dir=dst_dir) for item in value]
    if isinstance(value, dict):
        return {
            key: remap_json_paths_after_relocate(item, src_dir=src_dir, dst_dir=dst_dir)
            for key, item in value.items()
        }
    return value


def rewrite_run_config_after_relocate(src_dir: Path, dst_dir: Path) -> None:
    run_config_path = dst_dir / "run_config.json"
    if not run_config_path.exists():
        return
    try:
        with run_config_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    rewritten = remap_json_paths_after_relocate(payload, src_dir=src_dir, dst_dir=dst_dir)
    if isinstance(rewritten, dict):
        layout = rewritten.get("layout")
        if isinstance(layout, dict):
            layout["single_task_run_id"] = dst_dir.name
    with run_config_path.open("w", encoding="utf-8") as f:
        json.dump(rewritten, f, ensure_ascii=False, indent=2)


def run_one_task(
    *,
    task_spec: TaskSpec,
    total_tasks: int,
    args: argparse.Namespace,
    project_root: Path,
    wrapper_path: Path,
    run_id: str,
    runtime_root: Path,
    common_env: Dict[str, str],
) -> TaskRunResult:
    task_slug = make_task_slug(task_spec.name)
    run_slug = f"{run_id}-{task_spec.index:02d}-{task_slug}"
    task_run_id = run_slug
    task_runtime_dir = runtime_root / run_slug
    task_runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_runtime_dir / "runner.log"

    task_env = dict(common_env)
    task_env.update(
        {
            "TASK_NAME": task_spec.name,
            "TASK_SLUG": f"{args.subset}-{task_spec.index:02d}-{task_slug}",
            "RUN_TS": run_id.replace("_", ""),
            "TASK_RUNTIME_DIR": str(task_runtime_dir),
            "SERVER_LOG_FILE": str(task_runtime_dir / "server.log"),
            "EVAL_CLIENT_LOG_FILE": str(task_runtime_dir / "client.log"),
            "MCBOTS_SINGLE_TASK_RUN_ID": task_run_id,
        }
    )

    cmd = ["bash", str(wrapper_path)]
    if args.dry_run:
        dry_cmd = " ".join(cmd)
        with log_path.open("w", encoding="utf-8") as log_fp:
            log_fp.write(f"[dry-run] task={task_spec.name}\n")
            log_fp.write(f"[dry-run] cmd={dry_cmd}\n")
            for key in sorted(task_env):
                log_fp.write(f"[dry-run] env {key}={task_env[key]}\n")
        return TaskRunResult(
            index=task_spec.index,
            task_name=task_spec.name,
            task_dimension=task_spec.dimension,
            rc=0,
            timed_out=False,
            duration_sec=0.0,
            result_path=None,
            eval_success=None,
            eval_reason=None,
            delta_score=None,
            log_path=str(log_path),
            runtime_dir=str(task_runtime_dir),
            record_dir=None,
            status="dry_run",
            dry_run=True,
        )

    start_ts = time.time()
    timed_out = False
    rc = 0
    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(
            f"[run] [{task_spec.index}/{total_tasks}] task={task_spec.name} dimension={task_spec.dimension}\n"
        )
        log_fp.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_root,
                env={**os.environ, **task_env},
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=args.case_timeout_sec,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = 124
            log_fp.write(f"\n[error] process timeout after {args.case_timeout_sec}s\n")

    duration_sec = time.time() - start_ts
    raw_result_path = parse_result_path_from_log(log_path)
    eval_success: Optional[bool] = None
    eval_reason: Optional[str] = None
    delta_score: Optional[int] = None
    record_dir = parse_record_dir_from_log(log_path=log_path, project_root=project_root)

    if raw_result_path:
        resolved_result_path = resolve_result_path(raw_result_path, project_root)
        if resolved_result_path.exists():
            try:
                with resolved_result_path.open("r", encoding="utf-8") as f:
                    result_obj = json.load(f)
                eval_success = result_obj.get("success")
                eval_reason = result_obj.get("reason")
                delta_score = result_obj.get("delta_score")
                raw_result_path = str(resolved_result_path)
            except Exception:
                pass

    output_dir = Path(common_env["OUTPUT_DIR"])
    relocated = relocate_single_task_result_dir(output_dir=output_dir, task_run_id=task_run_id)
    if relocated:
        src_dir, dst_dir = relocated
        rewrite_run_config_after_relocate(src_dir=src_dir, dst_dir=dst_dir)
        raw_result_path = remap_path_after_relocate(raw_result_path, src_dir=src_dir, dst_dir=dst_dir)
        record_dir = remap_path_after_relocate(record_dir, src_dir=src_dir, dst_dir=dst_dir)

    if timed_out:
        status = "process_timeout"
    elif rc != 0:
        status = "wrapper_error"
    elif eval_success is True:
        status = "success"
    elif eval_success is False:
        status = f"task_failed:{eval_reason or 'unknown'}"
    elif raw_result_path:
        status = "result_unparsed"
    else:
        status = "missing_result"

    return TaskRunResult(
        index=task_spec.index,
        task_name=task_spec.name,
        task_dimension=task_spec.dimension,
        rc=rc,
        timed_out=timed_out,
        duration_sec=duration_sec,
        result_path=raw_result_path,
        eval_success=eval_success,
        eval_reason=eval_reason,
        delta_score=delta_score,
        log_path=str(log_path),
        runtime_dir=str(task_runtime_dir),
        record_dir=record_dir,
        status=status,
        dry_run=False,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="One-click OpenHA dev subset evaluator (inside container/local project workspace)."
    )
    # script path: <repo>/scripts/eval/run-openha-dev-subset-eval-inside.py
    # project root should resolve to <repo>.
    p.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument(
        "--subset-file",
        type=Path,
        default=Path("eval/openha_assets/task_subsets_dev_eval.json"),
    )
    p.add_argument("--subset", default="quick10", help="Subset key inside subset-file (default: quick10).")
    p.add_argument("--task", action="append", default=[], help="Run explicit task(s), overrides --subset when set.")
    p.add_argument(
        "--include-non-overworld",
        action="store_true",
        help="Include nether/end tasks. Default is overworld-only.",
    )
    p.add_argument("--max-tasks", type=int, default=0, help="Limit selected tasks (0 means no limit).")

    p.add_argument("--parallelism", type=int, default=10, help="Concurrent task workers (default: 10).")
    p.add_argument("--task-timeout-sec", type=float, default=600.0, help="In-task judge timeout (default: 600).")
    p.add_argument("--case-timeout-sec", type=float, default=1800.0, help="Per-task wall timeout (default: 1800).")
    p.add_argument("--judge-interval-sec", type=float, default=0.5, help="Judge polling interval.")
    p.add_argument("--wait-rcon-timeout-sec", type=float, default=180.0)
    p.add_argument("--wait-player-timeout-sec", type=float, default=120.0)
    p.add_argument(
        "--snapshot-template-mode",
        choices=["required", "off"],
        default="required",
        help="Snapshot template mode passed to wrapper (default: required).",
    )

    p.add_argument("--api-model-alias", default="kimi-k2.5", help="Model alias in api_models.json.")
    p.add_argument("--api-models-config", type=Path, default=Path("config/api_models.json"))
    p.add_argument(
        "--no-auto-load-api-model",
        action="store_true",
        help="Disable profile auto-load from api_models config.",
    )
    p.add_argument(
        "--model-params-json",
        default="",
        help="Set MCBOTS_MODEL_PARAMS_JSON (must be a JSON object).",
    )
    p.add_argument(
        "--sampling-config-path",
        type=Path,
        default=Path("agent/sampling_config.json"),
        help="Sampling config file for agent top-level sampling params.",
    )
    p.add_argument(
        "--sampling-json",
        default="",
        help="Inline JSON object to override sampling config for this batch run.",
    )

    p.add_argument(
        "--vision-profile",
        choices=["default", "aggressive-smart", "minimal"],
        default="aggressive-smart",
        help="Frame filtering profile (default: aggressive-smart).",
    )
    p.add_argument("--frame-env", action="append", default=[], help="Extra frame env override KEY=VALUE.")

    record_group = p.add_mutually_exclusive_group()
    record_group.add_argument("--record-video", action="store_true", dest="record_video")
    record_group.add_argument("--no-record-video", action="store_false", dest="record_video")
    p.set_defaults(record_video=True)
    p.add_argument("--video-fps", type=int, default=15)
    p.add_argument("--video-crf", type=int, default=30)

    p.add_argument("--run-id", default="", help="Optional run id suffix. Default uses UTC timestamp.")
    p.add_argument("--output-root", type=Path, default=Path("eval/results/dev_subset"))
    p.add_argument("--runtime-root", type=Path, default=Path("eval/runtime/dev_subset"))
    p.add_argument("--extra-env", action="append", default=[], help="Pass through KEY=VALUE env to each task.")
    p.add_argument("--skip-agent", action="store_true", help="Do not start agent (debug/smoke only).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--require-success",
        action="store_true",
        help="Exit non-zero when any task eval result is not success.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    project_root = args.project_root.resolve()

    subset_file = args.subset_file
    if not subset_file.is_absolute():
        subset_file = (project_root / subset_file).resolve()

    wrapper_path = project_root / "scripts" / "eval" / "run-openha-task-eval-inside.sh"
    if not wrapper_path.exists():
        print(f"[error] wrapper not found: {wrapper_path}", file=sys.stderr)
        return 2

    imported_task_configs_dir = project_root / "eval" / "openha_assets" / "imported" / "task_configs"
    if not imported_task_configs_dir.exists():
        print(
            f"[warn] imported task configs not found: {imported_task_configs_dir}. "
            "dimension filter will default to overworld."
        )

    if args.task:
        raw_tasks = [canonicalize_task_name_compat(x) for x in args.task if x.strip()]
        source_mode = "explicit_task_args"
    else:
        raw_tasks = load_subset_tasks(subset_file=subset_file, subset_key=args.subset)
        source_mode = f"subset:{args.subset}"

    task_specs: List[TaskSpec] = []
    for task_name in raw_tasks:
        dim = infer_task_dimension(task_name=task_name, imported_task_configs_dir=imported_task_configs_dir)
        if (not args.include_non_overworld) and dim != "overworld":
            continue
        task_specs.append(TaskSpec(index=len(task_specs) + 1, name=task_name, dimension=dim))

    if args.max_tasks > 0:
        task_specs = task_specs[: args.max_tasks]
        for idx, spec in enumerate(task_specs, start=1):
            task_specs[idx - 1] = TaskSpec(index=idx, name=spec.name, dimension=spec.dimension)

    if not task_specs:
        print("[error] no tasks selected after filtering", file=sys.stderr)
        return 2

    if args.parallelism <= 0:
        print("[error] --parallelism must be >= 1", file=sys.stderr)
        return 2

    output_root = args.output_root
    runtime_root = args.runtime_root
    if not output_root.is_absolute():
        output_root = (project_root / output_root).resolve()
    if not runtime_root.is_absolute():
        runtime_root = (project_root / runtime_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)

    run_id = args.run_id.strip() or utc_now_compact()
    run_output_dir = output_root / run_id
    run_output_dir.mkdir(parents=True, exist_ok=True)
    batch_runtime_dir = runtime_root / run_id
    batch_runtime_dir.mkdir(parents=True, exist_ok=True)

    frame_env = frame_filter_profile_env(args.vision_profile)
    frame_env.update(parse_key_value(args.frame_env))
    extra_env = parse_key_value(args.extra_env)

    common_env: Dict[str, str] = {
        "MCBOTS_PROJECT_ROOT": str(project_root),
        "USE_IMPORTED_TASK_CONFIG": "true",
        "OPENHA_SNAPSHOT_TEMPLATE_MODE": args.snapshot_template_mode,
        "TASK_TIMEOUT_SEC": str(args.task_timeout_sec),
        "JUDGE_INTERVAL_SEC": str(args.judge_interval_sec),
        "WAIT_RCON_TIMEOUT_SEC": str(args.wait_rcon_timeout_sec),
        "WAIT_PLAYER_TIMEOUT_SEC": str(args.wait_player_timeout_sec),
        "SKIP_AGENT": as_bool_text(args.skip_agent),
        "START_EVAL_CLIENT": "true",
        "EVAL_CLIENT_ENABLE_VNC": "false",
        "EVAL_CLIENT_ENABLE_REMOTE_BASH": "true",
        "EVAL_CLIENT_DISPLAY_RESOLUTION": "800x600x24",
        "AUTO_LOAD_API_MODEL": as_bool_text(not args.no_auto_load_api_model),
        "API_MODELS_CONFIG": str(
            (project_root / args.api_models_config).resolve()
            if not args.api_models_config.is_absolute()
            else args.api_models_config
        ),
        "API_MODEL_ALIAS": args.api_model_alias,
        "MCBOTS_SINGLE_TASK_RESULT_LAYOUT": "single_task_summary_dir",
        # For dev subset runs, write each task directly under:
        # <run_output_dir>/<task_run_id>/{summary.json,run_config.json,records/...}
        "MCBOTS_SINGLE_TASK_RUN_ROOT": ".",
        "OUTPUT_DIR": str(run_output_dir),
        "MCBOTS_RECORD_VIDEO": as_bool_text(args.record_video),
        "MCBOTS_VIDEO_FPS": str(max(args.video_fps, 1)),
        "MCBOTS_VIDEO_CRF": str(args.video_crf),
    }
    common_env.update(frame_env)
    common_env.update(extra_env)

    sampling_config_path: Optional[Path] = None
    if args.sampling_json.strip():
        sampling_obj = parse_json_object("sampling-json", args.sampling_json)
        sampling_config_path = batch_runtime_dir / "sampling_config.json"
        with sampling_config_path.open("w", encoding="utf-8") as f:
            json.dump(sampling_obj, f, ensure_ascii=False, indent=2)
    else:
        sampling_path = args.sampling_config_path
        if not sampling_path.is_absolute():
            sampling_path = (project_root / sampling_path).resolve()
        if not sampling_path.exists():
            print(f"[error] sampling config not found: {sampling_path}", file=sys.stderr)
            return 2
        sampling_config_path = sampling_path
    common_env["MCBOTS_SAMPLING_CONFIG_PATH"] = str(sampling_config_path)

    if args.model_params_json.strip():
        model_params_obj = parse_json_object("model-params-json", args.model_params_json)
        common_env["MCBOTS_MODEL_PARAMS_JSON"] = json.dumps(
            model_params_obj, ensure_ascii=False, separators=(",", ":")
        )

    print("[config] OpenHA dev subset eval")
    print(f"  project_root={project_root}")
    print(f"  source={source_mode}")
    print(f"  tasks={len(task_specs)} parallelism={args.parallelism}")
    print(f"  subset={args.subset} include_non_overworld={args.include_non_overworld}")
    print(f"  snapshot_template_mode={args.snapshot_template_mode}")
    print(f"  task_timeout_sec={args.task_timeout_sec} case_timeout_sec={args.case_timeout_sec}")
    print(f"  vision_profile={args.vision_profile}")
    print(f"  api_model_alias={args.api_model_alias}")
    print(f"  sampling_config_path={sampling_config_path}")
    print(f"  output_root={output_root}")
    print(f"  run_output_dir={run_output_dir}")
    print(f"  runtime_root={runtime_root}")
    if args.model_params_json.strip():
        print("  model_params_json=provided")
    if frame_env:
        print(f"  frame_env_overrides={len(frame_env)}")
    if extra_env:
        print(f"  extra_env_overrides={len(extra_env)}")
    print("[config] selected tasks:")
    for spec in task_specs:
        print(f"  - [{spec.index:02d}] {spec.name} (dimension={spec.dimension})")

    results: List[TaskRunResult] = []
    total = len(task_specs)
    completed = 0
    start_all = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallelism) as executor:
        future_map = {
            executor.submit(
                run_one_task,
                task_spec=task_spec,
                total_tasks=total,
                args=args,
                project_root=project_root,
                wrapper_path=wrapper_path,
                run_id=run_id,
                runtime_root=batch_runtime_dir,
                common_env=common_env,
            ): task_spec
            for task_spec in task_specs
        }
        for future in concurrent.futures.as_completed(future_map):
            completed += 1
            task_spec = future_map[future]
            try:
                result = future.result()
            except Exception as e:  # noqa: BLE001
                result = TaskRunResult(
                    index=task_spec.index,
                    task_name=task_spec.name,
                    task_dimension=task_spec.dimension,
                    rc=2,
                    timed_out=False,
                    duration_sec=0.0,
                    result_path=None,
                    eval_success=None,
                    eval_reason=f"runner_exception:{e}",
                    delta_score=None,
                    log_path=str((batch_runtime_dir / f"{run_id}-{task_spec.index:02d}-{make_task_slug(task_spec.name)}") / "runner.log"),
                    runtime_dir=str(batch_runtime_dir / f"{run_id}-{task_spec.index:02d}-{make_task_slug(task_spec.name)}"),
                    record_dir=None,
                    status="runner_exception",
                    dry_run=args.dry_run,
                )
            results.append(result)
            print(
                f"[progress] {completed}/{total} "
                f"[{result.index:02d}] {result.task_name} "
                f"status={result.status} rc={result.rc} "
                f"reason={result.eval_reason or '-'} "
                f"dur={result.duration_sec:.1f}s"
            )

    elapsed_sec = time.time() - start_all
    results.sort(key=lambda x: x.index)
    legacy_single_tasks_dir = run_output_dir / "single_tasks"
    if legacy_single_tasks_dir.exists() and not any(legacy_single_tasks_dir.iterdir()):
        legacy_single_tasks_dir.rmdir()

    success_count = sum(1 for r in results if r.eval_success is True)
    eval_fail_count = sum(1 for r in results if r.eval_success is False)
    infra_fail_count = sum(1 for r in results if r.status in {"wrapper_error", "process_timeout", "missing_result", "runner_exception"})
    timeout_count = sum(1 for r in results if r.timed_out or (r.eval_reason == "timeout"))

    summary = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "source_mode": source_mode,
        "subset": args.subset,
        "task_count": total,
        "parallelism": args.parallelism,
        "run_output_dir": str(run_output_dir),
        "task_result_layout": "per_task_subdir",
        "task_timeout_sec": args.task_timeout_sec,
        "case_timeout_sec": args.case_timeout_sec,
        "vision_profile": args.vision_profile,
        "api_model_alias": args.api_model_alias,
        "sampling_config_path": str(sampling_config_path),
        "model_params_json_provided": bool(args.model_params_json.strip()),
        "include_non_overworld": args.include_non_overworld,
        "elapsed_sec": elapsed_sec,
        "counts": {
            "success": success_count,
            "eval_failed": eval_fail_count,
            "infra_failed": infra_fail_count,
            "timeout_like": timeout_count,
        },
        "results": [asdict(r) for r in results],
    }

    summary_path = run_output_dir / "summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("[summary]")
    print(f"  success={success_count}/{total}")
    print(f"  eval_failed={eval_fail_count} infra_failed={infra_fail_count} timeout_like={timeout_count}")
    print(f"  summary={summary_path}")

    if args.require_success and success_count != total:
        return 1
    if infra_fail_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

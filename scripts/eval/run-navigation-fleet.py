#!/usr/bin/env python3
"""Run navigation tasks concurrently in resource-limited OCI containers."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINER_REPO_ROOT = Path("/workspace/mcbots")
CONTAINER_RESULTS_ROOT = Path("/workspace/mcbots-output/results")
CONTAINER_RUNTIME_ROOT = Path("/workspace/mcbots-output/runtime")
DEFAULT_GPU_IMAGE = "mcbots-navigation-gpu:1.21.11"
GPU_XORG_MODULE_MOUNTS = (
    (
        Path("/usr/lib64/xorg/modules/drivers/nvidia_drv.so"),
        Path("/usr/lib/xorg/modules/drivers/nvidia_drv.so"),
    ),
    (
        Path("/usr/lib64/xorg/modules/extensions/libglxserver_nvidia.so"),
        Path("/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so"),
    ),
)
SAFE_MEMORY = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+)?[kmgt]?(?:i?b)?$", re.I)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.runner import validate_runtime_template  # noqa: E402
from eval.navigation.runtime_defaults import LINUX_CPU_IMAGE, runtime_template_root  # noqa: E402
from eval.navigation.schema import (  # noqa: E402
    load_benchmark,
    load_profile,
    load_tasks,
    resolve_task_eval_setting,
)
from eval.navigation.snapshots import verify_snapshot  # noqa: E402
from eval.navigation.schema import load_map  # noqa: E402
import eval.navigation.schema as navigation_schema  # noqa: E402

DEFAULT_IMAGE = LINUX_CPU_IMAGE


def _append_gpu_xorg_module_mounts(command: list[str]) -> None:
    for source, destination in GPU_XORG_MODULE_MOUNTS:
        if source.is_file():
            command.extend(["--volume", f"{source}:{destination}:ro"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pilot", "formal"), default="formal")
    parser.add_argument(
        "--task-list-file",
        type=Path,
        help="JSON object with task_ids; omit to run the complete benchmark.",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--cpus-per-task", type=float, default=4.0)
    parser.add_argument("--memory-per-task", default="8g")
    parser.add_argument("--shm-size-per-task", default="2g")
    parser.add_argument(
        "--container-engine",
        choices=("auto", "podman", "docker"),
        default="auto",
    )
    parser.add_argument(
        "--image",
        help=(
            f"Container image; defaults to {DEFAULT_IMAGE} for CPU or "
            f"{DEFAULT_GPU_IMAGE} when --gpu-devices is set."
        ),
    )
    parser.add_argument(
        "--gpu-devices",
        help=(
            "Comma-separated host NVIDIA GPU indices available to the fleet, "
            "for example 0,2,4,5. Omit for CPU rendering. GPU workers require Podman CDI."
        ),
    )
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--base-url", default=os.getenv("MCBOTS_BASE_URL", ""))
    parser.add_argument(
        "--api-protocol",
        choices=("chat_completions", "responses"),
        default="chat_completions",
    )
    parser.add_argument("--model-parameters-file", type=Path, required=True)
    parser.add_argument(
        "--eval-setting-file",
        type=Path,
        help="JSON eval-setting overrides; omit to use standard defaults.",
    )
    parser.add_argument(
        "--record-video",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Skip complete scored tasks and rerun incomplete or infrastructure-error tasks "
            "from their initial state (default: enabled)."
        ),
    )
    parser.add_argument(
        "--infrastructure-retries",
        type=int,
        default=1,
        help="Immediate retries after an incomplete/infrastructure attempt (default: 1).",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=REPO_ROOT / "eval" / "results" / "navigation",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=REPO_ROOT / "eval" / "runtime" / "navigation",
    )
    parser.add_argument(
        "--runtime-template-root",
        type=Path,
        default=runtime_template_root(REPO_ROOT),
    )
    parser.add_argument("--skip-preflight", action="store_true")
    return parser.parse_args()


def _safe_component(value: str, label: str) -> str:
    if not value or value != Path(value).name or value in {".", ".."}:
        raise SystemExit(f"unsafe {label}: {value!r}")
    return value


def _resolve_engine(requested: str) -> str:
    candidates = ("podman", "docker") if requested == "auto" else (requested,)
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise SystemExit(f"container engine is unavailable: {', '.join(candidates)}")


def _parse_gpu_devices(value: str | None) -> list[str]:
    if value is None:
        return []
    devices = [item.strip() for item in value.split(",")]
    if not devices or any(not item or not item.isdigit() for item in devices):
        raise SystemExit("--gpu-devices must be comma-separated non-negative indices")
    normalized = [str(int(item)) for item in devices]
    if len(normalized) != len(set(normalized)):
        raise SystemExit("--gpu-devices contains duplicate GPU indices")
    return normalized


def _gpu_slot_plan(parallelism: int, gpu_devices: list[str]) -> list[str]:
    """Return an interleaved, maximally even assignment for worker slots."""
    if not gpu_devices:
        return []
    quotient, remainder = divmod(parallelism, len(gpu_devices))
    counts = {
        device: quotient + (1 if index < remainder else 0)
        for index, device in enumerate(gpu_devices)
    }
    return [
        device
        for slot_index in range(max(counts.values(), default=0))
        for device in gpu_devices
        if slot_index < counts[device]
    ]


def _xorg_bus_id(pci_bus_id: str) -> str:
    match = re.fullmatch(
        r"(?:[0-9A-Fa-f]{4,8}:)?([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\.([0-7])",
        pci_bus_id.strip(),
    )
    if match is None:
        raise RuntimeError(f"invalid NVIDIA PCI bus ID: {pci_bus_id!r}")
    bus, device, function = match.groups()
    return f"PCI:{int(bus, 16)}:{int(device, 16)}:{int(function)}"


def _gpu_bus_ids(gpu_devices: list[str]) -> dict[str, str]:
    bus_ids: dict[str, str] = {}
    for device in gpu_devices:
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "-i",
                    device,
                    "--query-gpu=pci.bus_id",
                    "--format=csv,noheader",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as error:
            raise SystemExit("--gpu-devices requires nvidia-smi on the host") from error
        pci_bus_id = completed.stdout.strip().splitlines()
        if completed.returncode != 0 or len(pci_bus_id) != 1:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise SystemExit(f"GPU {device} is unavailable to nvidia-smi: {detail}")
        try:
            bus_ids[device] = _xorg_bus_id(pci_bus_id[0])
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
    return bus_ids


def _worker_resources(args: argparse.Namespace) -> dict[str, Any]:
    resources: dict[str, Any] = {
        "cpus": args.cpus_per_task,
        "memory": args.memory_per_task,
    }
    if args.gpu_devices:
        resources["render_mode"] = "gpu"
    return resources


def _container_repo_path(host_path: Path) -> Path:
    resolved = host_path.expanduser().resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError as error:
        raise SystemExit(
            f"runtime template root must be inside the repository: {resolved}"
        ) from error
    return CONTAINER_REPO_ROOT / relative


def _selected_tasks(args: argparse.Namespace) -> list[str]:
    benchmark = load_benchmark("finalpool-navigation-v1")
    available = [
        str(task["id"])
        for map_id in benchmark["maps"]
        for task in load_tasks(map_id)
    ]
    if args.task_list_file is None:
        return available
    payload = _load_required_json(args.task_list_file, "task list")
    if payload.get("schema_version", 1) != 1:
        raise SystemExit("task list schema_version must be 1")
    selected = payload.get("task_ids")
    if not isinstance(selected, list) or any(
        not isinstance(task_id, str) or not task_id.strip()
        for task_id in selected
    ):
        raise SystemExit("task list task_ids must be an array of non-empty strings")
    selected = [task_id.strip() for task_id in selected]
    if not selected:
        raise SystemExit("task list must contain at least one task ID")
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise SystemExit(f"unknown task IDs: {', '.join(unknown)}")
    if len(selected) != len(set(selected)):
        raise SystemExit("task list contains duplicate task IDs")
    return selected


def _load_required_json(path: Path, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"missing {label} file: {resolved}") from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"invalid {label} JSON {resolved}: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} file must contain a JSON object")
    return payload


def _preflight(args: argparse.Namespace, task_ids: list[str]) -> None:
    benchmark = load_benchmark("finalpool-navigation-v1")
    selected_set = set(task_ids)
    selected_maps = [
        map_id
        for map_id in benchmark["maps"]
        if any(str(task["id"]) in selected_set for task in load_tasks(map_id))
    ]
    navigation_schema.RUNTIME_TEMPLATE_ROOT = (
        args.runtime_template_root.expanduser().resolve()
    )
    validate_runtime_template(
        load_profile(str(benchmark["profile_id"])),
        require_smoke=args.mode == "formal",
    )
    for map_id in selected_maps:
        verify_snapshot(load_map(map_id))
    print(
        f"Preflight passed: maps={len(selected_maps)} tasks={len(task_ids)} "
        f"runtime={navigation_schema.RUNTIME_TEMPLATE_ROOT}",
        flush=True,
    )


def _preflight_gpu_runtime(
    args: argparse.Namespace,
    *,
    engine: str,
) -> None:
    for device in args.gpu_devices:
        command = [
            engine,
            "run",
            "--rm",
            "--cap-add=ALL",
        ]
        if engine == "podman":
            command.append("--http-proxy=false")
        command.extend(
            [
                "--security-opt=label=disable",
                "--device",
                f"nvidia.com/gpu={device}",
            ]
        )
        _append_gpu_xorg_module_mounts(command)
        command.extend(
            [
                "--volume",
                f"{REPO_ROOT}:{CONTAINER_REPO_ROOT}:ro",
                "--workdir",
                str(CONTAINER_REPO_ROOT),
                "--env",
                "NVIDIA_DRIVER_CAPABILITIES=graphics,display,utility",
                "--env",
                f"GPU_PCI_BUSID={args.gpu_bus_ids[device]}",
                args.image,
                "bash",
                "scripts/containers/preflight-navigation-gpu-inside.sh",
            ]
        )
        print(f"GPU preflight: device={device}", flush=True)
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if completed.returncode != 0:
            raise SystemExit(
                f"GPU preflight failed for device {device} (exit {completed.returncode})"
            )


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _result_state(
    task_dir: Path,
    *,
    mode: str,
    expected_model_parameters: dict[str, Any],
    record_video: bool,
    worker_resources: dict[str, Any],
    eval_setting_overrides: dict[str, Any],
) -> str:
    """Return absent, incomplete, infrastructure_error, or complete."""
    if not task_dir.is_dir():
        return "absent"
    run = _load_object(task_dir / "run.json")
    if run is not None:
        if run.get("mode") != mode:
            raise RuntimeError(f"{task_dir.name}: existing result has a different mode")
        if run.get("model_parameters") != expected_model_parameters:
            raise RuntimeError(
                f"{task_dir.name}: existing result has different model parameters"
            )
        if bool(run.get("record_video", False)) != record_video:
            raise RuntimeError(
                f"{task_dir.name}: existing result has a different video setting"
            )
        if run.get("worker_resources") != worker_resources:
            raise RuntimeError(
                f"{task_dir.name}: existing result has different worker resources"
            )
        if run.get("eval_setting_overrides", {}) != eval_setting_overrides:
            raise RuntimeError(
                f"{task_dir.name}: existing result has different eval settings"
            )
    required = (
        "run.json",
        "completion.json",
        "metrics.json",
        "supervisor.json",
        "agent-result.json",
        "snapshot-after-run.json",
    )
    if any(not (task_dir / name).is_file() for name in required):
        return "incomplete"
    completion = _load_object(task_dir / "completion.json")
    snapshot = _load_object(task_dir / "snapshot-after-run.json")
    if completion is None or snapshot is None or snapshot.get("verified") is not True:
        return "incomplete"
    if completion.get("infrastructure_error") is True:
        return "infrastructure_error"
    if (
        isinstance(completion.get("success"), bool)
        and completion.get("infrastructure_error") is False
    ):
        return "complete"
    return "incomplete"


def _remove_stale_task_paths(runtime_dir: Path, result_dir: Path) -> None:
    for path in (runtime_dir, result_dir):
        if path.exists():
            shutil.rmtree(path)


def _container_name(run_id: str, task_id: str, attempt: int) -> str:
    raw = f"mcbots-nav-{run_id}-{task_id}-a{attempt}"
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-.")
    return normalized[:120]


def _container_command(
    args: argparse.Namespace,
    *,
    engine: str,
    run_id: str,
    task_id: str,
    attempt: int,
    gpu_device: str | None = None,
) -> tuple[str, list[str]]:
    name = _container_name(run_id, task_id, attempt)
    template_in_container = _container_repo_path(args.runtime_template_root)
    command = [
        engine,
        "run",
        "--rm",
        "--name",
        name,
        "--init",
        "--cpus",
        str(args.cpus_per_task),
        "--memory",
        args.memory_per_task,
        "--shm-size",
        args.shm_size_per_task,
        "--cap-add=ALL",
    ]
    if engine == "podman":
        command.append("--http-proxy=false")
    if gpu_device is not None:
        command.extend(
            [
                "--security-opt=label=disable",
                "--device",
                f"nvidia.com/gpu={gpu_device}",
                "--env",
                "NVIDIA_DRIVER_CAPABILITIES=graphics,display,utility",
                "--env",
                "RENDER_MODE=gpu",
                "--env",
                "DISPLAY_INDEX=0",
                "--env",
                f"GPU_PCI_BUSID={args.gpu_bus_ids[gpu_device]}",
                "--env",
                f"MCBOTS_NAV_GPU_DEVICE={gpu_device}",
            ]
        )
        _append_gpu_xorg_module_mounts(command)
    command.extend(
        [
        "--volume",
        f"{REPO_ROOT}:{CONTAINER_REPO_ROOT}:ro",
        "--volume",
        f"{args.results_root}:{CONTAINER_RESULTS_ROOT}:rw",
        "--volume",
        f"{args.runtime_root}:{CONTAINER_RUNTIME_ROOT}:rw",
        "--workdir",
        str(CONTAINER_REPO_ROOT),
        "--env",
        "MCBOTS_API_KEY",
        "--env",
        f"MCBOTS_BASE_URL={args.base_url}",
        "--env",
        f"MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT={template_in_container}",
        "--env",
        (
            "MCBOTS_NAV_RUNTIME_BACKEND=linux-container-gpu"
            if gpu_device is not None
            else "MCBOTS_NAV_RUNTIME_BACKEND=linux-container-cpu"
        ),
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        args.image,
        "uv",
        "run",
        "python",
        "scripts/eval/run-navigation-benchmark.py",
        "--mode",
        args.mode,
        "--task",
        task_id,
        "--run-id",
        run_id,
        "--model-id",
        args.model_id,
        "--api-protocol",
        args.api_protocol,
        "--model-parameters-json",
        args.model_parameters_json,
        "--eval-setting-overrides-json",
        args.eval_setting_overrides_json,
        "--results-root",
        str(CONTAINER_RESULTS_ROOT),
        "--runtime-root",
        str(CONTAINER_RUNTIME_ROOT),
        "--allocated-cpus",
        str(args.cpus_per_task),
        "--allocated-memory",
        args.memory_per_task,
        ]
    )
    if gpu_device is not None:
        command.extend(["--allocated-gpu", gpu_device])
    command.append("--record-video" if args.record_video else "--no-record-video")
    return name, command


def _run_task(
    args: argparse.Namespace,
    *,
    engine: str,
    run_id: str,
    task_id: str,
    expected_model_parameters: dict[str, Any],
    active_names: set[str],
    active_lock: threading.Lock,
    gpu_slots: queue.Queue[str] | None,
) -> dict[str, Any]:
    result_dir = args.results_root / run_id / task_id
    runtime_dir = args.runtime_root / run_id / task_id
    state = _result_state(
        result_dir,
        mode=args.mode,
        expected_model_parameters=expected_model_parameters,
        record_video=args.record_video,
        worker_resources=_worker_resources(args),
        eval_setting_overrides=args.eval_setting_overrides,
    )
    if state == "complete" and args.resume:
        print(f"SKIP {task_id}: complete", flush=True)
        return {"task_id": task_id, "status": "skipped_complete", "attempts": 0}
    if state != "absent":
        if not args.resume:
            raise RuntimeError(f"{task_id}: existing result prevents a non-resume run")
        _remove_stale_task_paths(runtime_dir, result_dir)

    gpu_device = gpu_slots.get() if gpu_slots is not None else None
    try:
        log_dir = args.results_root / "_fleet" / run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        max_attempts = 1 + args.infrastructure_retries
        last_state = "incomplete"
        last_returncode: int | None = None
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                _remove_stale_task_paths(runtime_dir, result_dir)
            name, command = _container_command(
                args,
                engine=engine,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                gpu_device=gpu_device,
            )
            log_path = log_dir / f"{task_id}.attempt-{attempt}.log"
            gpu_suffix = f" gpu={gpu_device}" if gpu_device is not None else ""
            print(
                f"START {task_id} attempt={attempt}/{max_attempts} "
                f"container={name}{gpu_suffix}",
                flush=True,
            )
            with active_lock:
                active_names.add(name)
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    completed = subprocess.run(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=REPO_ROOT,
                        check=False,
                    )
                last_returncode = completed.returncode
            finally:
                with active_lock:
                    active_names.discard(name)
            last_state = _result_state(
                result_dir,
                mode=args.mode,
                expected_model_parameters=expected_model_parameters,
                record_video=args.record_video,
                worker_resources=_worker_resources(args),
                eval_setting_overrides=args.eval_setting_overrides,
            )
            if last_state == "complete":
                print(f"DONE {task_id} attempt={attempt}{gpu_suffix}", flush=True)
                return {
                    "task_id": task_id,
                    "status": "complete",
                    "attempts": attempt,
                    "returncode": last_returncode,
                    "gpu_device": gpu_device,
                }
            print(
                f"RETRYABLE {task_id} attempt={attempt} state={last_state} "
                f"returncode={last_returncode}{gpu_suffix}",
                flush=True,
            )
        return {
            "task_id": task_id,
            "status": last_state,
            "attempts": max_attempts,
            "returncode": last_returncode,
            "gpu_device": gpu_device,
        }
    finally:
        if gpu_slots is not None and gpu_device is not None:
            gpu_slots.put(gpu_device)


def _validate_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.parallelism <= 0:
        raise SystemExit("--parallelism must be >= 1")
    if args.cpus_per_task <= 0:
        raise SystemExit("--cpus-per-task must be > 0")
    if args.infrastructure_retries < 0:
        raise SystemExit("--infrastructure-retries must be >= 0")
    args.gpu_devices = _parse_gpu_devices(args.gpu_devices)
    args.image = args.image or (
        DEFAULT_GPU_IMAGE if args.gpu_devices else DEFAULT_IMAGE
    )
    args.gpu_bus_ids = _gpu_bus_ids(args.gpu_devices) if args.gpu_devices else {}
    for name, value in (
        ("--memory-per-task", args.memory_per_task),
        ("--shm-size-per-task", args.shm_size_per_task),
    ):
        if not SAFE_MEMORY.fullmatch(value):
            raise SystemExit(f"{name} has an invalid size: {value!r}")
    if not args.base_url:
        raise SystemExit("--base-url or MCBOTS_BASE_URL is required")
    if not os.getenv("MCBOTS_API_KEY"):
        raise SystemExit("MCBOTS_API_KEY is required in the host environment")
    parameters = _load_required_json(args.model_parameters_file, "model parameters")
    reserved = sorted({"model_id", "api_protocol"} & set(parameters))
    if reserved:
        raise SystemExit(
            "model parameters file must not define fleet identity fields: "
            + ", ".join(reserved)
        )
    eval_setting_overrides = (
        _load_required_json(args.eval_setting_file, "eval setting")
        if args.eval_setting_file is not None
        else {}
    )
    resolve_task_eval_setting(eval_setting_overrides)
    args.eval_setting_overrides = eval_setting_overrides
    args.eval_setting_overrides_json = json.dumps(
        eval_setting_overrides,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    parameters = dict(parameters)
    parameters["model_id"] = args.model_id
    parameters["api_protocol"] = args.api_protocol
    args.model_parameters_json = json.dumps(
        {k: v for k, v in parameters.items() if k not in {"model_id", "api_protocol"}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return parameters


def main() -> int:
    args = parse_args()
    args.results_root = args.results_root.expanduser().resolve()
    args.runtime_root = args.runtime_root.expanduser().resolve()
    args.runtime_template_root = args.runtime_template_root.expanduser().resolve()
    expected_model_parameters = _validate_args(args)
    task_ids = _selected_tasks(args)
    run_id = _safe_component(
        args.run_id
        or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "run ID",
    )
    engine = _resolve_engine(args.container_engine)
    if args.gpu_devices and engine != "podman":
        raise SystemExit("--gpu-devices currently requires --container-engine podman")
    args.results_root.mkdir(parents=True, exist_ok=True)
    args.runtime_root.mkdir(parents=True, exist_ok=True)
    lock_dir = args.results_root / "_fleet-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{run_id}.lock"

    with lock_path.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit(f"another fleet owns run ID {run_id}") from error
        lock_handle.write(f"pid={os.getpid()}\n")
        lock_handle.flush()

        if not args.skip_preflight:
            _preflight(args, task_ids)
            if args.gpu_devices:
                _preflight_gpu_runtime(args, engine=engine)
        gpu_slot_plan = _gpu_slot_plan(args.parallelism, args.gpu_devices)
        gpu_slots: queue.Queue[str] | None = None
        gpu_slot_counts: dict[str, int] = {
            device: 0 for device in args.gpu_devices
        }
        if gpu_slot_plan:
            gpu_slots = queue.Queue()
            for device in gpu_slot_plan:
                gpu_slots.put(device)
                gpu_slot_counts[device] += 1
        print(
            f"Fleet start: run_id={run_id} tasks={len(task_ids)} "
            f"parallelism={args.parallelism} per_task={args.cpus_per_task}CPU/"
            f"{args.memory_per_task} protocol={args.api_protocol} video={args.record_video} "
            f"gpus={gpu_slot_counts or 'cpu'}",
            flush=True,
        )

        active_names: set[str] = set()
        active_lock = threading.Lock()
        outcomes: list[dict[str, Any]] = []
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=args.parallelism
            ) as executor:
                futures = {
                    executor.submit(
                        _run_task,
                        args,
                        engine=engine,
                        run_id=run_id,
                        task_id=task_id,
                        expected_model_parameters=expected_model_parameters,
                        active_names=active_names,
                        active_lock=active_lock,
                        gpu_slots=gpu_slots,
                    ): task_id
                    for task_id in task_ids
                }
                for future in concurrent.futures.as_completed(futures):
                    task_id = futures[future]
                    try:
                        outcomes.append(future.result())
                    except Exception as error:
                        print(f"ERROR {task_id}: {type(error).__name__}: {error}", flush=True)
                        outcomes.append(
                            {
                                "task_id": task_id,
                                "status": "orchestrator_error",
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
        except KeyboardInterrupt:
            with active_lock:
                names = sorted(active_names)
            for name in names:
                subprocess.run(
                    [engine, "rm", "--force", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            raise

    summary = {
        "schema_version": 1,
        "artifact_kind": "navigation-fleet-summary",
        "run_id": run_id,
        "mode": args.mode,
        "parallelism": args.parallelism,
        "cpus_per_task": args.cpus_per_task,
        "memory_per_task": args.memory_per_task,
        "container_image": args.image,
        "render_mode": "gpu" if args.gpu_devices else "cpu",
        "gpu_devices": args.gpu_devices,
        "gpu_slot_counts": gpu_slot_counts,
        "record_video": args.record_video,
        "model_parameters": expected_model_parameters,
        "model_parameters_file": str(args.model_parameters_file),
        "eval_setting_overrides": args.eval_setting_overrides,
        "eval_setting_file": (
            str(args.eval_setting_file) if args.eval_setting_file is not None else None
        ),
        "task_list_file": (
            str(args.task_list_file) if args.task_list_file is not None else None
        ),
        "outcomes": sorted(outcomes, key=lambda row: row["task_id"]),
    }
    summary_path = args.results_root / "_fleet" / run_id / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    failed = [
        row
        for row in outcomes
        if row.get("status") not in {"complete", "skipped_complete"}
    ]
    print(f"Fleet summary: {summary_path}", flush=True)
    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())

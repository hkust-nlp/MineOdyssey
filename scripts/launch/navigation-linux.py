#!/usr/bin/env python3
"""Build and run the anonymous navigation benchmark on Linux with CPU rendering."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_ROOT = "/workspace/mcbots"
DEFAULT_IMAGE = "localhost/anonymous-navigation:linux-cpu"
RELEASE_MANIFEST = Path("eval/navigation/releases/navigation-maps-1.21.11-v1.json")
DEFAULT_DOWNLOADS = ROOT / "downloads/navigation-maps-1.21.11-v1"
MAP_COMMANDS = {"prepare-map", "prepare-maps"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--engine", choices=("docker", "podman"))
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--forward-proxy", action="store_true", help="Allow Podman to inherit a container-reachable host proxy.")
    p.add_argument("--dry-run", action="store_true", help="Print argv without running or using credentials.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="Build Ubuntu/Python/Java/Mesa dependencies.")
    sub.add_parser("doctor", help="Check tools, headless OpenGL and the command sandbox.")
    sub.add_parser("tasks", help="List task IDs and map IDs (no container required).")
    sub.add_parser("prepare-runtime", help="Download pinned Minecraft/mods and run a live smoke test.")
    m = sub.add_parser("prepare-map", help="Import one verified release map archive.")
    m.add_argument("--map", required=True)
    m.add_argument("--downloads-dir", type=Path, default=DEFAULT_DOWNLOADS)
    maps = sub.add_parser("prepare-maps", help="Import all 30 benchmark release map archives.")
    maps.add_argument("--downloads-dir", type=Path, default=DEFAULT_DOWNLOADS)
    run = sub.add_parser("run", help="Run one task; defaults to review without model requests.")
    run.add_argument("--task", required=True)
    run.add_argument("--mode", choices=("review", "pilot", "formal"), default="review")
    run.add_argument("--model-id", default="")
    run.add_argument("--api-protocol", choices=("chat_completions", "responses"), default="chat_completions")
    run.add_argument("--action-protocol", choices=("xml", "tool_calls"), default="tool_calls")
    run.add_argument("--vnc", action="store_true", help="Publish noVNC on loopback only.")
    run.add_argument("--vnc-port", type=int, default=6080)
    return p


def task_catalog(root: Path = ROOT) -> dict[str, dict]:
    rows = json.loads((root / "eval/navigation/tasks.json").read_text())["tasks"]
    return {row["task_id"]: row for row in rows}


def choose_engine(explicit: str | None) -> str:
    if explicit:
        if not shutil.which(explicit):
            raise ValueError(f"{explicit} is not installed or not on PATH")
        return explicit
    for engine in ("docker", "podman"):
        if shutil.which(engine):
            return engine
    raise ValueError("Install Docker Engine or Podman, then rerun this command.")


def container_command(args: argparse.Namespace, engine: str, root: Path = ROOT) -> list[str]:
    if args.command == "build":
        return [engine, "build", *(["--http-proxy=false"] if engine == "podman" and not args.forward_proxy else []), "--file", str(root / "containers/Containerfile.navigation-cpu"),
                "--tag", args.image, str(root)]
    # Bubblewrap needs mount/namespace operations inside the outer container.
    # No host networking, Docker socket, device passthrough or --privileged.
    cmd = [engine, "run", "--rm", "--shm-size=1g", "--cap-add=SYS_ADMIN",
           "--security-opt=seccomp=unconfined"]
    cmd += (["--security-opt=label=disable", *([] if args.forward_proxy else ["--http-proxy=false"])] if engine == "podman"
            else ["--security-opt=apparmor=unconfined"])
    cmd += ["--volume", f"{root}:{CONTAINER_ROOT}:rw", "--workdir", CONTAINER_ROOT]
    for value in ["MCBOTS_PROJECT_ROOT=" + CONTAINER_ROOT,
                  "MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT=" + CONTAINER_ROOT + "/eval/templates/_local/navigation-linux-cpu",
                  "MCBOTS_NAV_RUNTIME_BACKEND=linux-container-cpu", "RENDER_MODE=cpu",
                  "LIBGL_ALWAYS_SOFTWARE=1", "PYTHONDONTWRITEBYTECODE=1",
                  "MCBOTS_NAV_VNC_PORT=5900", "MCBOTS_NAV_NOVNC_PORT=6080"]:
        cmd += ["--env", value]
    if args.command in MAP_COMMANDS:
        cmd += ["--volume", f"{args.downloads_dir.resolve()}:/inputs:ro"]
    if args.command == "run":
        for name in ("MCBOTS_API_KEY", "MCBOTS_BASE_URL"):
            if name in os.environ:
                cmd += ["--env", name]  # Never put credential values in argv/logs.
        if args.vnc:
            cmd += ["--publish", f"127.0.0.1:{args.vnc_port}:6080"]
    cmd += [args.image, "uv", "run", "--frozen", "python"]
    if args.command == "doctor":
        return cmd + ["scripts/runtime/check-navigation-linux.py"]
    if args.command == "prepare-runtime":
        return cmd + ["scripts/eval/prepare-navigation-runtime.py"]
    if args.command in MAP_COMMANDS:
        cmd += ["scripts/snapshot/import-navigation-map-release.py", "--downloads-dir", "/inputs"]
        if args.command == "prepare-map":
            cmd += ["--map", args.map]
        return cmd
    cmd += ["scripts/eval/run-navigation-benchmark.py", "--mode", args.mode, "--task", args.task,
            "--api-protocol", args.api_protocol, "--model-parameters-json",
            json.dumps({"action_protocol": args.action_protocol}, separators=(",", ":"))]
    if args.model_id:
        cmd += ["--model-id", args.model_id]
    if args.vnc:
        cmd += ["--vnc"]
    return cmd


def validate(args: argparse.Namespace, root: Path = ROOT) -> None:
    if args.command == "run":
        if args.task not in task_catalog(root):
            raise ValueError(f"Unknown task {args.task!r}; use the tasks command.")
        if not 1 <= args.vnc_port <= 65535:
            raise ValueError("VNC port must be between 1 and 65535")
        if args.mode != "review":
            if not args.model_id.strip():
                raise ValueError("pilot/formal mode requires --model-id")
            key = os.environ.get("MCBOTS_API_KEY", "").strip()
            if not args.dry_run and (not key or key.startswith("<")):
                raise ValueError("Set MCBOTS_API_KEY before starting a model run.")
        if not args.dry_run:
            receipt = root / "eval/templates/_local/navigation-linux-cpu/1.21.11/runtime-receipt.json"
            if not receipt.is_file():
                raise ValueError("Runtime is missing; run prepare-runtime first.")
    if args.command in MAP_COMMANDS:
        benchmark_maps = {t["map_id"] for t in task_catalog(root).values()}
        if args.command == "prepare-map" and args.map not in benchmark_maps:
            raise ValueError(f"Unknown map {args.map!r}; use the tasks command.")
        manifest = json.loads((root / RELEASE_MANIFEST).read_text())
        assets = {row["map_id"]: row for row in manifest["assets"]}
        selected = [args.map] if args.command == "prepare-map" else sorted(benchmark_maps)
        for map_id in selected:
            if map_id not in assets:
                raise ValueError(f"Release manifest has no map {map_id!r}.")
            expected = args.downloads_dir / assets[map_id]["asset_name"]
            if not args.dry_run and not expected.is_file():
                raise ValueError(f"Map archive missing: {expected}. Download it with "
                                 f"python3 scripts/snapshot/download-navigation-map-release.py --map {map_id}")


def main() -> int:
    p = parser()
    args = p.parse_args()
    if args.command == "tasks":
        for task_id, row in task_catalog().items():
            print(f"{task_id}\t{row['map_id']}")
        return 0
    try:
        if platform.system() != "Linux":
            raise ValueError("This launcher targets Linux hosts.")
        validate(args)
        engine = args.engine if args.dry_run and args.engine else choose_engine(args.engine)
        cmd = container_command(args, engine)
        print(shlex.join(cmd), flush=True)
        if args.dry_run:
            return 0
        if args.command == "run" and args.vnc:
            print(f"Open http://127.0.0.1:{args.vnc_port}/vnc.html after client startup.", flush=True)
        return subprocess.run(cmd, cwd=ROOT).returncode
    except (ValueError, OSError) as error:
        p.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

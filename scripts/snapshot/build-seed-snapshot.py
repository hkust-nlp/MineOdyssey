#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][seed-snapshot] {msg}", flush=True)


def utc_compact_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def task_slug(task_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", task_name.strip())
    slug = slug.strip("._-")
    return slug or "task"


def version_slug(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", version).strip("_") or "version"


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def resolve_seed_and_position(
    task_config: Path, task_name: str, explicit_seed: Optional[int]
) -> Tuple[int, Optional[Tuple[int, int, int]]]:
    cfg = load_json(task_config)
    if task_name not in cfg:
        raise KeyError(f"Task not found in config: {task_name}")
    task = cfg[task_name]
    if not isinstance(task, dict):
        raise RuntimeError(f"Invalid task config for {task_name}: expected object")
    seeds = task.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise RuntimeError(f"Task {task_name} has no seeds[]")
    first = seeds[0]
    if not isinstance(first, dict) or "seed" not in first:
        raise RuntimeError(f"Task {task_name} seeds[0] missing 'seed'")
    seed_val = first["seed"]
    if isinstance(seed_val, bool):
        raise RuntimeError(f"Invalid boolean seed in task {task_name}")
    try:
        seed = int(seed_val)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse seed for task {task_name}: {seed_val}") from e

    if explicit_seed is not None:
        seed = explicit_seed

    position: Optional[Tuple[int, int, int]] = None
    raw_pos = first.get("position")
    if isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 3:
        try:
            position = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
        except Exception:
            position = None

    return seed, position


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_json(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid JSON payload from {url}")
    return data


def download_file(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dst)


def resolve_server_download(version: str, cache_root: Path) -> Tuple[str, str]:
    manifest_cache = cache_root / "mojang_version_manifest_v2.json"
    log(f"resolve server download for version={version}")
    manifest: Dict[str, Any]
    if manifest_cache.is_file():
        try:
            manifest = load_json(manifest_cache)
        except Exception:
            manifest = {}
    else:
        manifest = {}
    if not manifest or "versions" not in manifest:
        log(f"download manifest: {VERSION_MANIFEST_URL}")
        manifest = download_json(VERSION_MANIFEST_URL)
        manifest_cache.parent.mkdir(parents=True, exist_ok=True)
        manifest_cache.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    versions = manifest.get("versions")
    if not isinstance(versions, list):
        raise RuntimeError("Invalid Mojang version manifest: missing versions[]")
    version_meta_url = None
    for item in versions:
        if isinstance(item, dict) and item.get("id") == version and isinstance(item.get("url"), str):
            version_meta_url = item["url"]
            break
    if not version_meta_url:
        raise RuntimeError(f"Minecraft version not found in Mojang manifest: {version}")

    version_meta_cache = cache_root / "versions" / version_slug(version) / "version_meta.json"
    if version_meta_cache.is_file():
        try:
            version_meta = load_json(version_meta_cache)
        except Exception:
            version_meta = {}
    else:
        version_meta = {}
    if not version_meta or "downloads" not in version_meta:
        log(f"download version metadata: {version_meta_url}")
        version_meta = download_json(version_meta_url)
        version_meta_cache.parent.mkdir(parents=True, exist_ok=True)
        version_meta_cache.write_text(json.dumps(version_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    downloads = version_meta.get("downloads")
    if not isinstance(downloads, dict):
        raise RuntimeError(f"Version metadata missing downloads for {version}")
    server = downloads.get("server")
    if not isinstance(server, dict):
        raise RuntimeError(f"Version {version} does not expose a server download")
    url = server.get("url")
    sha1 = server.get("sha1")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"Invalid server download URL for {version}")
    if not isinstance(sha1, str) or not sha1:
        raise RuntimeError(f"Invalid server SHA1 for {version}")
    return url, sha1.lower()


def ensure_server_jar(version: str, cache_root: Path) -> Path:
    server_dir = cache_root / "servers" / version_slug(version)
    jar_path = server_dir / "server.jar"
    url, expected_sha1 = resolve_server_download(version=version, cache_root=cache_root)

    if jar_path.is_file():
        actual_sha1 = sha1_file(jar_path).lower()
        if actual_sha1 == expected_sha1:
            log(f"reuse cached server jar: {jar_path}")
            return jar_path
        log(f"cached jar sha1 mismatch, re-downloading: {jar_path}")
        jar_path.unlink()

    log(f"download server jar: version={version} -> {jar_path}")
    download_file(url=url, dst=jar_path)
    actual_sha1 = sha1_file(jar_path).lower()
    if actual_sha1 != expected_sha1:
        raise RuntimeError(
            f"Server jar SHA1 mismatch for {version}: expected={expected_sha1} actual={actual_sha1}"
        )
    return jar_path


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


def write_server_properties(server_dir: Path, seed: int, port: int) -> None:
    props = {
        "level-name": "world",
        "level-seed": str(seed),
        "gamemode": "survival",
        "difficulty": "easy",
        "hardcore": "false",
        "online-mode": "false",
        "white-list": "false",
        "enable-rcon": "false",
        "enable-query": "false",
        "server-port": str(port),
        "server-ip": "127.0.0.1",
        "motd": "mcbots seed snapshot builder",
        "spawn-protection": "0",
        "max-tick-time": "-1",
        "allow-nether": "true",
        "view-distance": "10",
        "simulation-distance": "10",
        "sync-chunk-writes": "true",
    }
    path = server_dir / "server.properties"
    lines = [f"{k}={v}" for k, v in props.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare_server_dir(server_dir: Path, seed: int) -> None:
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    port = reserve_free_port()
    write_server_properties(server_dir=server_dir, seed=seed, port=port)


def read_tail(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(raw[-lines:])


def dedupe_positions(positions: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    out: List[Tuple[int, int, int]] = []
    seen: set[Tuple[int, int, int]] = set()
    for p in positions:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def normalize_dimension_name(raw: str) -> str:
    s = (raw or "").strip().lower()
    aliases = {
        "": "overworld",
        "0": "overworld",
        "overworld": "overworld",
        "minecraft:overworld": "overworld",
        "-1": "the_nether",
        "nether": "the_nether",
        "the_nether": "the_nether",
        "minecraft:the_nether": "the_nether",
        "1": "the_end",
        "end": "the_end",
        "the_end": "the_end",
        "minecraft:the_end": "the_end",
    }
    return aliases.get(s, s or "overworld")


def dimension_execute_id(raw: str) -> str:
    dim = normalize_dimension_name(raw)
    return dim if ":" in dim else f"minecraft:{dim}"


def parse_position_arg(raw: str) -> Tuple[int, int, int]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ValueError(f"Invalid --target-position '{raw}', expected format x,y,z")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def build_target_chunk_pregen_commands(
    positions: List[Tuple[int, int, int]], radius_chunks: int, target_dimension: str
) -> List[str]:
    if not positions:
        return []

    r = max(0, int(radius_chunks))
    boxes: List[Tuple[int, int, int, int]] = []
    seen_boxes: set[Tuple[int, int, int, int]] = set()
    for x, _y, z in positions:
        chunk_x = x // 16
        chunk_z = z // 16
        from_x = (chunk_x - r) * 16
        from_z = (chunk_z - r) * 16
        to_x = (chunk_x + r) * 16
        to_z = (chunk_z + r) * 16
        box = (from_x, from_z, to_x, to_z)
        if box in seen_boxes:
            continue
        seen_boxes.add(box)
        boxes.append(box)

    cmds = ["gamerule sendCommandFeedback false"]
    dim_exec = dimension_execute_id(target_dimension)
    for from_x, from_z, to_x, to_z in boxes:
        cmds.append(f"execute in {dim_exec} run forceload add {from_x} {from_z} {to_x} {to_z}")
    # Keep the snapshot world clean (no persistent forced chunk tickets) while ensuring
    # target areas get generated and flushed to disk.
    cmds.append("save-all flush")
    for from_x, from_z, to_x, to_z in boxes:
        cmds.append(f"execute in {dim_exec} run forceload remove {from_x} {from_z} {to_x} {to_z}")
    return cmds


def run_console_commands(
    proc: subprocess.Popen[str],
    *,
    commands: List[str],
    log_path: Path,
    per_command_sleep_sec: float = 0.25,
) -> None:
    if not commands:
        return
    if proc.poll() is not None:
        tail = read_tail(log_path, lines=120)
        raise RuntimeError(f"Server exited before console commands (rc={proc.returncode}). log_tail:\n{tail}")
    if proc.stdin is None:
        raise RuntimeError("Server process has no stdin for console commands")

    for raw_cmd in commands:
        cmd = raw_cmd.strip()
        if not cmd:
            continue
        if cmd.startswith("/"):
            cmd = cmd[1:].strip()
        if not cmd:
            continue
        log(f"console> {cmd}")
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            tail = read_tail(log_path, lines=120)
            raise RuntimeError(f"Failed writing console command '{cmd}': {e}\nlog_tail:\n{tail}") from e
        if per_command_sleep_sec > 0:
            time.sleep(per_command_sleep_sec)


def probe_java_version(java_cmd: str) -> str:
    try:
        proc = subprocess.run(
            [java_cmd, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return f"<probe failed: {e}>"
    line = (proc.stdout or "").splitlines()
    return line[0].strip() if line else "<no output>"


def parse_java_major(version_line: str) -> Optional[int]:
    m = re.search(r'version "([^"]+)"', version_line)
    if not m:
        return None
    raw = m.group(1)
    if raw.startswith("1."):
        parts = raw.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return None
    head = raw.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def wait_for_server_done(proc: subprocess.Popen[str], log_path: Path, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    next_log_at = 0.0
    success_patterns = ("Done (", "For help, type")
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = read_tail(log_path, lines=120)
            raise RuntimeError(f"Server exited early (rc={proc.returncode}). log_tail:\n{tail}")
        text = read_tail(log_path, lines=200)
        if any(p in text for p in success_patterns):
            return
        now = time.time()
        if now >= next_log_at:
            remaining = max(0.0, deadline - now)
            log(f"waiting server ready... remaining={remaining:.0f}s log={log_path}")
            next_log_at = now + 5.0
        time.sleep(1.0)
    tail = read_tail(log_path, lines=120)
    raise RuntimeError(f"Timed out waiting for server ready ({timeout_sec}s). log_tail:\n{tail}")


def graceful_stop_server(proc: subprocess.Popen[str], timeout_sec: float = 90.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.write("save-all flush\n")
            proc.stdin.flush()
            time.sleep(0.3)
            proc.stdin.write("stop\n")
            proc.stdin.flush()
    except Exception:
        pass

    try:
        proc.wait(timeout=timeout_sec)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        proc.kill()
    except Exception:
        pass
    proc.wait(timeout=10)


def run_server_once(
    *,
    phase_name: str,
    version: str,
    java_cmd: str,
    memory: str,
    server_dir: Path,
    jar_path: Path,
    startup_timeout_sec: float,
    ready_commands: Optional[List[str]] = None,
) -> Path:
    logs_dir = server_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{phase_name}.log"
    if log_path.exists():
        log_path.unlink()

    cmd = [java_cmd, f"-Xms{memory}", f"-Xmx{memory}", "-jar", str(jar_path), "nogui"]
    log(f"start {phase_name}: version={version} java={java_cmd} cwd={server_dir}")

    with log_path.open("a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            cmd,
            cwd=server_dir,
            stdin=subprocess.PIPE,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_server_done(proc=proc, log_path=log_path, timeout_sec=startup_timeout_sec)
            if ready_commands:
                run_console_commands(proc, commands=ready_commands, log_path=log_path)
        finally:
            graceful_stop_server(proc=proc)

    if proc.returncode not in (0, None):
        tail = read_tail(log_path, lines=120)
        raise RuntimeError(f"{phase_name} server failed (rc={proc.returncode}). log_tail:\n{tail}")
    log(f"{phase_name} completed: log={log_path}")
    return log_path


def copy_world_snapshot(src_world: Path, dst_world: Path, force: bool) -> None:
    if not src_world.exists():
        raise FileNotFoundError(f"World directory not found: {src_world}")
    if dst_world.exists():
        if not force:
            raise FileExistsError(f"Snapshot already exists: {dst_world}")
        shutil.rmtree(dst_world)
    dst_world.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_world, dst_world)
    session_lock = dst_world / "session.lock"
    if session_lock.exists():
        session_lock.unlink()


@dataclass
class SnapshotMetadata:
    task_name: str
    snapshot_slug: str
    seed: int
    task_position: Optional[Tuple[int, int, int]]
    target_positions: List[Tuple[int, int, int]]
    target_pregen_radius_chunks: int
    target_dimension: str
    raw_version: str
    upgraded_version: str
    raw_world_dir: str
    upgraded_world_dir: str
    legacy_log: str
    upgrade_log: str
    built_at_utc: str


def write_metadata(path: Path, meta: SnapshotMetadata) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build seed world snapshots in an isolated, one-off flow: "
            "generate raw world on 1.16.5, then load once on 1.21.1 to upgrade."
        )
    )
    p.add_argument("--task-config", default="eval/openha_assets/kill_entity_min.json")
    p.add_argument("--task-name", default="kill_entity:sheep")
    p.add_argument("--snapshot-slug", default="", help="Override output path slug (default derives from task name).")
    p.add_argument("--seed", type=int, default=None, help="Override seed; otherwise use task config seeds[0].")
    p.add_argument("--raw-version", default="1.16.5")
    p.add_argument("--upgraded-version", default="1.21.1")
    p.add_argument("--java-legacy", default=os.environ.get("MCBOTS_JAVA_LEGACY", "java"))
    p.add_argument("--java-modern", default=os.environ.get("MCBOTS_JAVA_MODERN", "java"))
    p.add_argument("--legacy-memory", default="2G")
    p.add_argument("--modern-memory", default="2G")
    p.add_argument("--startup-timeout-sec", type=float, default=600.0)
    p.add_argument(
        "--target-pregen-radius-chunks",
        type=int,
        default=3,
        help="Pre-generate legacy target area(s) (radius in chunks around task position and extra --target-position values).",
    )
    p.add_argument(
        "--target-position",
        action="append",
        default=[],
        help="Additional target position to pre-generate in legacy world, format: x,y,z (repeatable).",
    )
    p.add_argument(
        "--target-dimension",
        default="overworld",
        help="Dimension used when pre-generating target chunks (overworld/the_nether/the_end).",
    )
    p.add_argument("--snapshot-root", default="eval/snapshots")
    p.add_argument("--cache-root", default="")
    p.add_argument("--work-root", default="")
    p.add_argument("--force", action="store_true", help="Overwrite existing snapshot outputs.")
    p.add_argument("--keep-work", action="store_true", help="Keep temporary work dirs for debugging.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    task_cfg_path = Path(args.task_config)
    if not task_cfg_path.is_absolute():
        task_cfg_path = project_root / task_cfg_path
    snapshot_root = Path(args.snapshot_root)
    if not snapshot_root.is_absolute():
        snapshot_root = project_root / snapshot_root
    cache_root = Path(args.cache_root) if args.cache_root else (snapshot_root / "_cache")
    if not cache_root.is_absolute():
        cache_root = project_root / cache_root
    work_root = Path(args.work_root) if args.work_root else (snapshot_root / "_work")
    if not work_root.is_absolute():
        work_root = project_root / work_root

    seed, task_position = resolve_seed_and_position(
        task_config=task_cfg_path, task_name=args.task_name, explicit_seed=args.seed
    )
    extra_target_positions: List[Tuple[int, int, int]] = []
    for raw_pos in args.target_position:
        try:
            extra_target_positions.append(parse_position_arg(raw_pos))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e)) from e
    target_positions: List[Tuple[int, int, int]] = []
    if task_position is not None:
        target_positions.append(task_position)
    target_positions.extend(extra_target_positions)
    target_positions = dedupe_positions(target_positions)
    target_dimension = normalize_dimension_name(args.target_dimension)
    tslug = task_slug(args.snapshot_slug) if str(args.snapshot_slug).strip() else task_slug(args.task_name)
    raw_out = (
        snapshot_root
        / f"seed_raw_{version_slug(args.raw_version)}"
        / tslug
        / str(seed)
        / "world"
    )
    upgraded_out = (
        snapshot_root
        / f"seed_upgraded_{version_slug(args.upgraded_version)}"
        / tslug
        / str(seed)
        / "world"
    )

    legacy_java_line = probe_java_version(args.java_legacy)
    modern_java_line = probe_java_version(args.java_modern)
    legacy_java_major = parse_java_major(legacy_java_line)
    modern_java_major = parse_java_major(modern_java_line)

    log(f"task={args.task_name} seed={seed}")
    log(f"raw snapshot target: {raw_out}")
    log(f"upgraded snapshot target: {upgraded_out}")
    if task_position is not None:
        log(f"task position detected: {task_position}")
    else:
        log("task position not found in seeds[0]")
    if target_positions:
        log(
            "legacy target pregen enabled: "
            f"positions={len(target_positions)} radius={max(0, int(args.target_pregen_radius_chunks))} chunks "
            f"dimension={target_dimension}"
        )
        for i, pos in enumerate(target_positions[:8]):
            log(f"  pregen_target[{i}]={pos}")
        if len(target_positions) > 8:
            log(f"  ... ({len(target_positions) - 8} more positions)")
    else:
        log("legacy target pregen skipped (no target positions)")
    log(f"java legacy probe: {args.java_legacy} -> {legacy_java_line}")
    log(f"java modern probe: {args.java_modern} -> {modern_java_line}")
    if args.raw_version.startswith("1.16") and legacy_java_major is not None and legacy_java_major >= 17:
        log(
            "warning: legacy 1.16.x server may fail on Java 17+. "
            "Consider --java-legacy=/path/to/java8_or_java11"
        )
    if args.upgraded_version.startswith("1.21") and modern_java_major is not None and modern_java_major < 21:
        log(
            "warning: modern 1.21.x server usually requires Java 21. "
            "Consider --java-modern=/path/to/java21"
        )

    if args.dry_run:
        print("dry-run summary:")
        print(f"  task_name={args.task_name}")
        print(f"  snapshot_slug={tslug}")
        print(f"  seed={seed}")
        print(f"  task_position={task_position}")
        print(f"  target_positions={target_positions}")
        print(f"  target_pregen_radius_chunks={max(0, int(args.target_pregen_radius_chunks))}")
        print(f"  target_dimension={target_dimension}")
        print(f"  raw_version={args.raw_version} -> {raw_out}")
        print(f"  upgraded_version={args.upgraded_version} -> {upgraded_out}")
        print(f"  java_legacy={args.java_legacy}")
        print(f"  java_modern={args.java_modern}")
        print(f"  snapshot_root={snapshot_root}")
        print(f"  cache_root={cache_root}")
        print(f"  work_root={work_root}")
        return

    work_root.mkdir(parents=True, exist_ok=True)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    legacy_jar = ensure_server_jar(version=args.raw_version, cache_root=cache_root)
    modern_jar = ensure_server_jar(version=args.upgraded_version, cache_root=cache_root)

    work_dir_path = Path(
        tempfile.mkdtemp(prefix=f"seed-snapshot-{utc_compact_ts()}-", dir=str(work_root))
    )
    log(f"work dir: {work_dir_path}")

    legacy_server_dir = work_dir_path / "legacy_server"
    modern_server_dir = work_dir_path / "upgrade_server"
    legacy_world_dir = legacy_server_dir / "world"
    modern_world_dir = modern_server_dir / "world"
    legacy_log_path: Optional[Path] = None
    upgrade_log_path: Optional[Path] = None

    try:
        prepare_server_dir(server_dir=legacy_server_dir, seed=seed)
        legacy_ready_commands = build_target_chunk_pregen_commands(
            positions=target_positions,
            radius_chunks=args.target_pregen_radius_chunks,
            target_dimension=target_dimension,
        )
        legacy_log_path = run_server_once(
            phase_name=f"generate-{version_slug(args.raw_version)}",
            version=args.raw_version,
            java_cmd=args.java_legacy,
            memory=args.legacy_memory,
            server_dir=legacy_server_dir,
            jar_path=legacy_jar,
            startup_timeout_sec=args.startup_timeout_sec,
            ready_commands=legacy_ready_commands,
        )
        copy_world_snapshot(src_world=legacy_world_dir, dst_world=raw_out, force=args.force)
        log(f"raw snapshot saved: {raw_out}")

        prepare_server_dir(server_dir=modern_server_dir, seed=seed)
        copy_world_snapshot(src_world=raw_out, dst_world=modern_world_dir, force=True)
        upgrade_log_path = run_server_once(
            phase_name=f"upgrade-{version_slug(args.upgraded_version)}",
            version=args.upgraded_version,
            java_cmd=args.java_modern,
            memory=args.modern_memory,
            server_dir=modern_server_dir,
            jar_path=modern_jar,
            startup_timeout_sec=args.startup_timeout_sec,
        )
        copy_world_snapshot(src_world=modern_world_dir, dst_world=upgraded_out, force=args.force)
        log(f"upgraded snapshot saved: {upgraded_out}")

        raw_parent = raw_out.parent
        upgraded_parent = upgraded_out.parent
        if legacy_log_path is not None:
            shutil.copy2(legacy_log_path, raw_parent / "build_server.log")
        if upgrade_log_path is not None:
            shutil.copy2(upgrade_log_path, upgraded_parent / "build_server.log")

        meta = SnapshotMetadata(
            task_name=args.task_name,
            snapshot_slug=tslug,
            seed=seed,
            task_position=task_position,
            target_positions=target_positions,
            target_pregen_radius_chunks=max(0, int(args.target_pregen_radius_chunks)),
            target_dimension=target_dimension,
            raw_version=args.raw_version,
            upgraded_version=args.upgraded_version,
            raw_world_dir=str(raw_out),
            upgraded_world_dir=str(upgraded_out),
            legacy_log=str(raw_parent / "build_server.log"),
            upgrade_log=str(upgraded_parent / "build_server.log"),
            built_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        write_metadata(raw_parent / "snapshot_meta.json", meta)
        write_metadata(upgraded_parent / "snapshot_meta.json", meta)
        log("snapshot build complete")
        log(f"use this for eval TEMPLATE_WORLD_DIR: {upgraded_out}")
    finally:
        if args.keep_work:
            log(f"keep-work enabled; work dir preserved: {work_dir_path}")
        else:
            shutil.rmtree(work_dir_path, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

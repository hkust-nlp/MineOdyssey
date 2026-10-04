#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][visual-compare] {msg}", flush=True)


def utc_compact_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def reserve_display() -> int:
    for disp in range(240, 300):
        lock = Path(f"/tmp/.X{disp}-lock")
        if not lock.exists():
            return disp
    raise RuntimeError("No free X display found in 240..299")


def write_server_properties(server_dir: Path, port: int) -> None:
    props = {
        "level-name": "world",
        "online-mode": "false",
        "enable-rcon": "false",
        "enable-query": "false",
        "server-port": str(port),
        "server-ip": "127.0.0.1",
        "motd": "mcbots visual compare",
        "spawn-protection": "0",
        "max-tick-time": "-1",
        "sync-chunk-writes": "true",
        "view-distance": "10",
        "simulation-distance": "10",
    }
    lines = [f"{k}={v}" for k, v in props.items()]
    (server_dir / "server.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (server_dir / "eula.txt").write_text("eula=true\n", encoding="utf-8")


def read_tail(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


def wait_log_pattern(
    path: Path,
    patterns: List[str],
    timeout_sec: float,
    proc: Optional[subprocess.Popen[str]] = None,
    phase: str = "",
) -> str:
    deadline = time.time() + timeout_sec
    regexes = [re.compile(p) for p in patterns]
    next_log_at = 0.0
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            tail = read_tail(path, lines=200)
            raise RuntimeError(f"{phase} process exited early rc={proc.returncode}. log_tail:\n{tail}")
        text = read_tail(path, lines=400)
        for rx in regexes:
            m = rx.search(text)
            if m:
                return m.group(0)
        now = time.time()
        if now >= next_log_at:
            remaining = max(0.0, deadline - now)
            log(f"waiting {phase}... remaining={remaining:.0f}s log={path}")
            next_log_at = now + 5
        time.sleep(1)
    tail = read_tail(path, lines=200)
    raise RuntimeError(f"Timeout waiting {phase}. log_tail:\n{tail}")


def graceful_stop(proc: Optional[subprocess.Popen[str]], stop_cmd: Optional[str] = None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if stop_cmd and proc.stdin is not None:
        try:
            proc.stdin.write(stop_cmd)
            proc.stdin.flush()
        except Exception:
            pass
    try:
        proc.wait(timeout=20)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    proc.wait(timeout=5)


def assert_proc_alive(proc: Optional[subprocess.Popen[str]], name: str, log_path: Path) -> None:
    if proc is None:
        raise RuntimeError(f"{name} process is None")
    if proc.poll() is None:
        return
    tail = read_tail(log_path, lines=200)
    raise RuntimeError(f"{name} exited rc={proc.returncode}. log_tail:\n{tail}")


def start_xvfb(display_num: int, resolution: str, log_path: Path) -> subprocess.Popen[str]:
    with log_path.open("a", encoding="utf-8") as fp:
        proc = subprocess.Popen(
            ["Xvfb", f":{display_num}", "-screen", "0", f"{resolution}x24", "-nolisten", "tcp"],
            stdout=fp,
            stderr=subprocess.STDOUT,
            text=True,
        )
    time.sleep(1)
    if proc.poll() is not None:
        tail = read_tail(log_path, lines=80)
        raise RuntimeError(f"Xvfb failed rc={proc.returncode}. log_tail:\n{tail}")
    return proc


def start_server(server_dir: Path, server_jar: Path, java_cmd: str, memory: str, log_path: Path) -> subprocess.Popen[str]:
    with log_path.open("a", encoding="utf-8") as fp:
        proc = subprocess.Popen(
            [java_cmd, f"-Xms{memory}", f"-Xmx{memory}", "-jar", str(server_jar), "nogui"],
            cwd=server_dir,
            stdin=subprocess.PIPE,
            stdout=fp,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return proc


def start_client(
    *,
    version: str,
    game_dir: Path,
    player_name: str,
    host: str,
    port: int,
    display_num: int,
    resolution: str,
    log_path: Path,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["DISPLAY"] = f":{display_num}"
    cmd = [
        "portablemc",
        "--main-dir",
        str(game_dir),
        "--work-dir",
        str(game_dir),
        "start",
        version,
        "--username",
        player_name,
        "-s",
        host,
        "-p",
        str(port),
        "--resolution",
        resolution,
    ]
    game_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fp:
        proc = subprocess.Popen(
            cmd,
            cwd=game_dir,
            env=env,
            stdout=fp,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return proc


def send_server_cmd(server_proc: subprocess.Popen[str], command: str) -> None:
    if server_proc.stdin is None:
        raise RuntimeError("Server stdin is not available")
    server_proc.stdin.write(command.rstrip("\n") + "\n")
    server_proc.stdin.flush()


def clear_nearby_double_plants(server_proc: subprocess.Popen[str], x: int, y: int, z: int, radius: int = 24) -> None:
    x0, y0, z0 = x - radius, max(0, y - 4), z - radius
    x1, y1, z1 = x + radius, y + 8, z + radius
    for plant in ("minecraft:tall_grass", "minecraft:large_fern"):
        for half in ("lower", "upper"):
            send_server_cmd(
                server_proc,
                f"fill {x0} {y0} {z0} {x1} {y1} {z1} minecraft:air replace {plant}[half={half}]",
            )


def clear_problematic_entities_for_legacy_client(server_proc: subprocess.Popen[str]) -> None:
    # In 1.16.5 + Mesa software rendering, some sessions crash while ticking/rendering falling blocks.
    send_server_cmd(server_proc, "kill @e[type=minecraft:falling_block]")


def capture_display_png(display_num: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import_cmd = shutil.which("import")
    if import_cmd:
        proc = subprocess.run(
            [import_cmd, "-display", f":{display_num}", "-window", "root", str(out_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0 and out_path.exists():
            return
        import_err = proc.stderr.strip()
    else:
        import_err = "import not found"

    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        raise RuntimeError(f"Screenshot capture failed: {import_err}; ffmpeg not found")

    # ffmpeg x11grab can capture the root window in one frame; use a short timeout and overwrite output.
    proc = subprocess.run(
        [ffmpeg_cmd, "-y", "-f", "x11grab", "-i", f":{display_num}", "-frames:v", "1", str(out_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"Screenshot capture failed via import ({import_err}) and ffmpeg rc={proc.returncode} stderr={proc.stderr.strip()}"
        )


@dataclass
class PhaseResult:
    label: str
    version: str
    screenshot: str
    server_log: str
    client_log: str
    port: int
    display: str


def run_phase(
    *,
    label: str,
    version: str,
    world_template: Path,
    server_jar: Path,
    java_cmd: str,
    player_name: str,
    x: int,
    y: int,
    z: int,
    yaw: float,
    pitch: float,
    output_dir: Path,
    resolution: str,
    memory: str,
    timeout_sec: float,
    post_tp_settle_sec: float,
    post_tp_maintenance_interval_sec: float,
    screenshot_min_bytes: int,
    screenshot_retry_count: int,
    screenshot_retry_interval_sec: float,
    client_cache_root: Path,
) -> PhaseResult:
    phase_dir = output_dir / label
    if phase_dir.exists():
        shutil.rmtree(phase_dir)
    phase_dir.mkdir(parents=True, exist_ok=True)
    work_dir = phase_dir / "work"
    server_dir = work_dir / "server"
    client_dir = client_cache_root / label
    server_world = server_dir / "world"
    server_log = phase_dir / "server.log"
    client_log = phase_dir / "client.log"
    xvfb_log = phase_dir / "xvfb.log"
    screenshot_path = phase_dir / "view.png"

    port = reserve_port()
    display_num = reserve_display()
    log(f"[{label}] setup port={port} display=:{display_num}")

    server_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(world_template, server_world)
    session_lock = server_world / "session.lock"
    if session_lock.exists():
        session_lock.unlink()
    write_server_properties(server_dir=server_dir, port=port)

    xvfb_proc: Optional[subprocess.Popen[str]] = None
    server_proc: Optional[subprocess.Popen[str]] = None
    client_proc: Optional[subprocess.Popen[str]] = None
    try:
        xvfb_proc = start_xvfb(display_num=display_num, resolution=resolution, log_path=xvfb_log)
        server_proc = start_server(
            server_dir=server_dir,
            server_jar=server_jar,
            java_cmd=java_cmd,
            memory=memory,
            log_path=server_log,
        )
        wait_log_pattern(
            path=server_log,
            patterns=[r"Done \([^)]+\)! For help, type"],
            timeout_sec=timeout_sec,
            proc=server_proc,
            phase=f"{label} server ready",
        )
        log(f"[{label}] server ready")

        client_proc = start_client(
            version=version,
            game_dir=client_dir,
            player_name=player_name,
            host="127.0.0.1",
            port=port,
            display_num=display_num,
            resolution=resolution,
            log_path=client_log,
        )
        wait_log_pattern(
            path=server_log,
            patterns=[
                rf"{re.escape(player_name)}.*joined the game",
                rf"{re.escape(player_name)}\[.*\] logged in",
            ],
            timeout_sec=timeout_sec,
            proc=server_proc,
            phase=f"{label} player join",
        )
        log(f"[{label}] player joined")

        # Normalize environment for visual comparison only.
        send_server_cmd(server_proc, "gamerule doDaylightCycle false")
        send_server_cmd(server_proc, "weather clear")
        send_server_cmd(server_proc, "time set day")
        send_server_cmd(server_proc, "gamerule doMobSpawning false")
        clear_problematic_entities_for_legacy_client(server_proc)
        # 1.16.5 client (llvmpipe in container) can crash rendering some double plants.
        # Clear only nearby double plants in both versions so screenshots remain comparable.
        clear_nearby_double_plants(server_proc, x=x, y=y, z=z, radius=24)
        send_server_cmd(server_proc, f"tp {player_name} {x} {y} {z} {yaw} {pitch}")
        # Software rendering can be slow; keep the area normalized while chunks stream in.
        settle_deadline = time.time() + post_tp_settle_sec
        next_maint = 0.0
        while time.time() < settle_deadline:
            assert_proc_alive(client_proc, f"{label} client", client_log)
            assert_proc_alive(server_proc, f"{label} server", server_log)
            now = time.time()
            if now >= next_maint:
                clear_problematic_entities_for_legacy_client(server_proc)
                clear_nearby_double_plants(server_proc, x=x, y=y, z=z, radius=24)
                next_maint = now + post_tp_maintenance_interval_sec
            time.sleep(0.25)

        assert_proc_alive(client_proc, f"{label} client", client_log)
        assert_proc_alive(server_proc, f"{label} server", server_log)

        # Retry screenshot if we likely caught a loading screen/blank frame.
        attempt = 0
        while True:
            attempt += 1
            capture_display_png(display_num=display_num, out_path=screenshot_path)
            size = screenshot_path.stat().st_size if screenshot_path.exists() else 0
            if size >= screenshot_min_bytes:
                break
            if attempt > screenshot_retry_count:
                log(f"[{label}] screenshot looks suspiciously small ({size} bytes), keeping last capture")
                break
            assert_proc_alive(client_proc, f"{label} client", client_log)
            assert_proc_alive(server_proc, f"{label} server", server_log)
            log(f"[{label}] small screenshot ({size} bytes), retrying in {screenshot_retry_interval_sec:.1f}s (attempt {attempt}/{screenshot_retry_count})")
            clear_problematic_entities_for_legacy_client(server_proc)
            clear_nearby_double_plants(server_proc, x=x, y=y, z=z, radius=24)
            time.sleep(screenshot_retry_interval_sec)
        log(f"[{label}] screenshot saved: {screenshot_path}")
        return PhaseResult(
            label=label,
            version=version,
            screenshot=str(screenshot_path),
            server_log=str(server_log),
            client_log=str(client_log),
            port=port,
            display=f":{display_num}",
        )
    finally:
        if server_proc is not None and server_proc.poll() is None:
            try:
                send_server_cmd(server_proc, "save-all flush")
            except Exception:
                pass
            graceful_stop(server_proc, stop_cmd="stop\n")
        graceful_stop(client_proc)
        graceful_stop(xvfb_proc)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Visual compare screenshots for raw 1.16.5 vs upgraded 1.21.1 snapshots.")
    p.add_argument(
        "--raw-world",
        default="/workspace/mcbots/eval/snapshots/seed_raw_1_16_5/kill_entity_sheep/3001859308298079279/world",
    )
    p.add_argument(
        "--upgraded-world",
        default="/workspace/mcbots/eval/snapshots/seed_upgraded_1_21_1/kill_entity_sheep/3001859308298079279/world",
    )
    p.add_argument("--raw-server-jar", default="/workspace/mcbots/eval/snapshots/_cache/servers/1_16_5/server.jar")
    p.add_argument("--upgraded-server-jar", default="/workspace/mcbots/eval/snapshots/_cache/servers/1_21_1/server.jar")
    p.add_argument("--java", default="java")
    p.add_argument("--player-name", default="bot")
    p.add_argument("--x", type=int, default=31)
    p.add_argument("--y", type=int, default=64)
    p.add_argument("--z", type=int, default=61)
    p.add_argument("--yaw", type=float, default=180.0)
    p.add_argument("--pitch", type=float, default=0.0)
    p.add_argument("--resolution", default="1280x720")
    p.add_argument("--memory", default="2G")
    p.add_argument("--timeout-sec", type=float, default=600.0)
    p.add_argument("--post-tp-settle-sec", type=float, default=20.0)
    p.add_argument("--post-tp-maintenance-interval-sec", type=float, default=1.0)
    p.add_argument("--screenshot-min-bytes", type=int, default=16000)
    p.add_argument("--screenshot-retry-count", type=int, default=6)
    p.add_argument("--screenshot-retry-interval-sec", type=float, default=5.0)
    p.add_argument("--output-dir", default="")
    p.add_argument("--client-cache-root", default="/workspace/mcbots/eval/snapshots/visual_compare_client_cache")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = (Path(args.output_dir) if args.output_dir else Path("/workspace/mcbots/eval/snapshots/visual_compare") / utc_compact_ts()).resolve()
    raw_world = Path(args.raw_world).resolve()
    upgraded_world = Path(args.upgraded_world).resolve()
    raw_server_jar = Path(args.raw_server_jar).resolve()
    upgraded_server_jar = Path(args.upgraded_server_jar).resolve()
    client_cache_root = Path(args.client_cache_root).resolve()

    for p in [raw_world, upgraded_world, raw_server_jar, upgraded_server_jar]:
        if not p.exists():
            raise FileNotFoundError(f"Missing required path: {p}")
    out_dir.mkdir(parents=True, exist_ok=True)
    client_cache_root.mkdir(parents=True, exist_ok=True)

    log(f"output dir: {out_dir}")
    raw = run_phase(
        label="raw_1_16_5",
        version="1.16.5",
        world_template=raw_world,
        server_jar=raw_server_jar,
        java_cmd=args.java,
        player_name=args.player_name,
        x=args.x,
        y=args.y,
        z=args.z,
        yaw=args.yaw,
        pitch=args.pitch,
        output_dir=out_dir,
        resolution=args.resolution,
        memory=args.memory,
        timeout_sec=args.timeout_sec,
        post_tp_settle_sec=args.post_tp_settle_sec,
        post_tp_maintenance_interval_sec=args.post_tp_maintenance_interval_sec,
        screenshot_min_bytes=args.screenshot_min_bytes,
        screenshot_retry_count=args.screenshot_retry_count,
        screenshot_retry_interval_sec=args.screenshot_retry_interval_sec,
        client_cache_root=client_cache_root,
    )
    upgraded = run_phase(
        label="upgraded_1_21_1",
        version="1.21.1",
        world_template=upgraded_world,
        server_jar=upgraded_server_jar,
        java_cmd=args.java,
        player_name=args.player_name,
        x=args.x,
        y=args.y,
        z=args.z,
        yaw=args.yaw,
        pitch=args.pitch,
        output_dir=out_dir,
        resolution=args.resolution,
        memory=args.memory,
        timeout_sec=args.timeout_sec,
        post_tp_settle_sec=args.post_tp_settle_sec,
        post_tp_maintenance_interval_sec=args.post_tp_maintenance_interval_sec,
        screenshot_min_bytes=args.screenshot_min_bytes,
        screenshot_retry_count=args.screenshot_retry_count,
        screenshot_retry_interval_sec=args.screenshot_retry_interval_sec,
        client_cache_root=client_cache_root,
    )

    meta = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "coord": [args.x, args.y, args.z],
        "yaw": args.yaw,
        "pitch": args.pitch,
        "environment_normalized_for_compare": {
            "doDaylightCycle": False,
            "weather": "clear",
            "time": "day",
            "doMobSpawning": False,
            "clearNearbyTallGrass": True,
            "clearFallingBlockEntities": True,
        },
        "results": [asdict(raw), asdict(upgraded)],
    }
    meta_path = out_dir / "compare_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"done. raw={raw.screenshot}")
    log(f"done. upgraded={upgraded.screenshot}")
    log(f"meta={meta_path}")


if __name__ == "__main__":
    main()

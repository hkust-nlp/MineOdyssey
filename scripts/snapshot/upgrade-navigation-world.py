#!/usr/bin/env python3
"""Upgrade one copied Minecraft world with the pinned 1.21.11 server runtime."""

from __future__ import annotations

import argparse
import gzip
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import struct
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.snapshots import make_tree_writable, read_level_version  # noqa: E402


TARGET_VERSION = {"data_version": 4671, "version_name": "1.21.11"}


def normalize_upgrade_output(world: Path) -> None:
    """Remove wall-clock values emitted by --forceUpgrade for stable fingerprints."""

    level_path = world / "level.dat"
    raw = bytearray(gzip.decompress(level_path.read_bytes()))
    marker = bytes([4]) + struct.pack(">H", len("LastPlayed")) + b"LastPlayed"
    offsets: list[int] = []
    start = 0
    while True:
        found = raw.find(marker, start)
        if found < 0:
            break
        offsets.append(found)
        start = found + len(marker)
    if len(offsets) != 1:
        raise RuntimeError(f"expected one LastPlayed tag in {level_path}, found {len(offsets)}")
    value_offset = offsets[0] + len(marker)
    raw[value_offset : value_offset + 8] = struct.pack(">q", 0)
    level_path.write_bytes(gzip.compress(bytes(raw), mtime=0))
    (world / "level.dat_old").unlink(missing_ok=True)

    for region in world.rglob("*.mca"):
        with region.open("r+b") as handle:
            header = handle.read(8192)
            if not header:
                continue
            if len(header) < 8192:
                raise RuntimeError(f"truncated Anvil header: {region}")
            handle.seek(4096)
            handle.write(b"\0" * 4096)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-world", type=Path, required=True)
    parser.add_argument("--output-world", type=Path, required=True)
    parser.add_argument(
        "--server-template",
        type=Path,
        default=REPO_ROOT / "eval/templates/_local/navigation/1.21.11/server",
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args()


def copy_world(source: Path, destination: Path) -> None:
    try:
        subprocess.run(
            ["cp", "-cR", str(source), str(destination)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        shutil.copytree(source, destination)


def server_properties() -> str:
    values = {
        "allow-flight": "true",
        "difficulty": "peaceful",
        "enable-command-block": "false",
        "enforce-secure-profile": "false",
        "force-gamemode": "true",
        "gamemode": "creative",
        "generate-structures": "false",
        "level-name": "world",
        "max-players": "1",
        "online-mode": "false",
        "pause-when-empty-seconds": "-1",
        "server-ip": "127.0.0.1",
        "server-port": "0",
        "simulation-distance": "2",
        "spawn-animals": "false",
        "spawn-monsters": "false",
        "spawn-npcs": "false",
        "spawn-protection": "0",
        "sync-chunk-writes": "false",
        "view-distance": "2",
    }
    return "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"


def upgrade_world(
    source: Path,
    destination: Path,
    server_template: Path,
    *,
    replace: bool,
    timeout_seconds: int,
) -> None:
    source = source.resolve()
    destination = destination.resolve()
    server_template = server_template.resolve()
    if not (source / "level.dat").is_file():
        raise RuntimeError(f"source world lacks level.dat: {source}")
    if destination.exists() and not replace:
        raise FileExistsError(f"output world already exists: {destination}")
    run_sh = server_template / "run.sh"
    libraries = server_template / "libraries"
    if not run_sh.is_file() or not libraries.is_dir():
        raise RuntimeError(f"incomplete 1.21.11 server template: {server_template}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.upgrade.", dir=destination.parent
    ) as temporary_name:
        server = Path(temporary_name) / "server"
        server.mkdir()
        world = server / "world"
        copy_world(source, world)
        make_tree_writable(world)
        (world / "session.lock").unlink(missing_ok=True)
        shutil.copy2(run_sh, server / "run.sh")
        os.chmod(server / "run.sh", 0o755)
        (server / "libraries").symlink_to(libraries, target_is_directory=True)
        (server / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        (server / "server.properties").write_text(server_properties(), encoding="utf-8")
        (server / "user_jvm_args.txt").write_text(
            "-Xms512M\n-Xmx3G\n-XX:+UseG1GC\n-Dlog4j2.formatMsgNoLookups=true\n",
            encoding="utf-8",
        )

        log_path = destination.parent / f"{destination.name}.upgrade.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                ["sh", "run.sh", "--forceUpgrade", "--eraseCache", "nogui"],
                cwd=server,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            assert process.stdin is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_seconds
            ready = False
            fatal_line: str | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                for key, _ in selector.select(timeout=1):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    log.write(line)
                    log.flush()
                    stripped = line.rstrip()
                    print(stripped, flush=True)
                    if 'Done (' in line and 'For help, type "help"' in line:
                        ready = True
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    if "Failed to start the minecraft server" in line:
                        fatal_line = stripped
                if ready and process.poll() is not None:
                    break
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise TimeoutError(f"world upgrade timed out; see {log_path}")
            return_code = process.returncode
        if return_code != 0 or not ready:
            detail = f": {fatal_line}" if fatal_line else ""
            raise RuntimeError(
                f"Minecraft upgrade did not reach a clean server start (exit {return_code})"
                f"{detail}; see {log_path}"
            )
        (world / "session.lock").unlink(missing_ok=True)
        normalize_upgrade_output(world)
        level = read_level_version(world / "level.dat")
        if any(level.get(key) != value for key, value in TARGET_VERSION.items()):
            raise RuntimeError(f"upgraded world has unexpected version metadata: {level}")
        if destination.exists():
            shutil.rmtree(destination)
        world.rename(destination)


def main() -> int:
    args = parse_args()
    upgrade_world(
        args.source_world,
        args.output_world,
        args.server_template,
        replace=args.replace,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"prepared {args.output_world.resolve()}: {read_level_version(args.output_world / 'level.dat')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

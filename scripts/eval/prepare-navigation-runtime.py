#!/usr/bin/env python3
"""Prepare the isolated Minecraft 1.21.11 navigation client/server runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.runner import _allocate_ports, _clone_tree, _configure_server  # noqa: E402
from eval.navigation.schema import (  # noqa: E402
    atomic_write_json,
    digest_json,
    load_profile,
    load_setting,
    runtime_template_path,
)
from eval.navigation.snapshots import make_tree_writable, sha256_file  # noqa: E402


def _runtime_environment(profile: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    if (
        platform.system().lower() == "linux"
        and platform.machine().lower() in {"aarch64", "arm64"}
    ):
        environment["MCBOTS_PORTABLEMC_LWJGL_VERSION"] = str(
            profile["portablemc"]["linux_arm64_lwjgl_version"]
        )
    return environment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Prepare artifacts but leave receipt status=prepared (formal runs reject it).",
    )
    return parser.parse_args()


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".download",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with urllib.request.urlopen(url, timeout=60) as source:  # noqa: S310
            with temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            raise RuntimeError(
                f"download SHA-256 mismatch for {destination.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_bundled_mod(mod: dict[str, Any], display_name: str) -> Path:
    jar = REPO_ROOT / str(mod["artifact_path"])
    if not jar.is_file():
        raise RuntimeError(f"bundled {display_name} artifact is missing: {jar}")
    actual = sha256_file(jar)
    expected = str(mod["sha256"])
    if actual != expected:
        raise RuntimeError(
            f"bundled {display_name} hash mismatch: expected {expected}, got {actual}"
        )
    return jar


def _load_ground_navigation(profile: dict[str, Any]) -> Path:
    mod = next(
        row
        for row in profile["client_mods"]["optional_profiles"]["guideline"]
        if row["mod_id"] == "ground_navigation"
    )
    return _load_bundled_mod(mod, "Ground Navigation")


def _prepare_client(
    root: Path,
    profile: dict[str, Any],
    agentbridge: Path,
    ground_navigation: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    game = root / "client" / "game"
    game.mkdir(parents=True, exist_ok=True)
    shim = REPO_ROOT / "scripts" / "runtime" / "portablemc-neoforge-root-shim.py"
    subprocess.run(
        [
            sys.executable,
            str(shim),
            "--main-dir",
            str(game),
            "--work-dir",
            str(game),
            "--timeout",
            os.environ.get("MCBOTS_PORTABLEMC_TIMEOUT_SEC", "120"),
            "start",
            f"neoforge:{profile['neoforge']['version']}",
            "--dry",
            "--username",
            "NavRuntime",
        ],
        cwd=REPO_ROOT,
        env=_runtime_environment(profile),
        check=True,
    )
    mods = game / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for mod in profile["client_mods"]["required"]:
        destination = mods / str(mod["artifact_name"])
        if mod["mod_id"] == "agentbridge":
            shutil.copy2(agentbridge, destination)
        else:
            _download(
                str(mod["source_url"]),
                destination,
                str(mod["sha256"]),
            )
        actual = sha256_file(destination)
        if actual != mod["sha256"]:
            raise RuntimeError(f"client mod hash mismatch: {destination.name}")
        rows.append(
            {
                "name": mod["name"],
                "mod_id": mod["mod_id"],
                "artifact_name": destination.name,
                "bytes": destination.stat().st_size,
                "sha256": actual,
            }
        )
    active = sorted(path.name for path in mods.glob("*.jar"))
    expected = sorted(str(row["artifact_name"]) for row in profile["client_mods"]["required"])
    if active != expected:
        raise RuntimeError(f"unknown or missing active client mods: {active!r}")
    optional_rows: dict[str, list[dict[str, Any]]] = {}
    for profile_name, optional_mods in profile["client_mods"]["optional_profiles"].items():
        optional_dir = root / "client" / "optional-mods" / profile_name
        optional_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[dict[str, Any]] = []
        for mod in optional_mods:
            destination = optional_dir / str(mod["artifact_name"])
            if mod["mod_id"] == "ground_navigation":
                shutil.copy2(ground_navigation, destination)
            else:
                _download(str(mod["source_url"]), destination, str(mod["sha256"]))
            actual = sha256_file(destination)
            if actual != mod["sha256"]:
                raise RuntimeError(f"optional client mod hash mismatch: {destination.name}")
            prepared.append(
                {
                    "name": mod["name"],
                    "mod_id": mod["mod_id"],
                    "artifact_name": destination.name,
                    "bytes": destination.stat().st_size,
                    "sha256": actual,
                }
            )
        optional_rows[profile_name] = prepared
    (game / "options.txt").write_text(
        "fullscreen:false\n"
        "pauseOnLostFocus:false\n"
        "rawMouseInput:false\n"
        "skipMultiplayerWarning:true\n"
        "joinedFirstServer:true\n"
        "tutorialStep:none\n"
        "gamma:0.0\n"
        "fov:0.75\n",
        encoding="utf-8",
    )
    config_dir = game / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_rows: list[dict[str, Any]] = []
    for config in profile["client_configs"]:
        source = REPO_ROOT / str(config["source_path"])
        destination = config_dir / str(config["artifact_name"])
        if sha256_file(source) != config["sha256"]:
            raise RuntimeError(f"tracked client config hash mismatch: {source}")
        shutil.copy2(source, destination)
        config_rows.append(
            {
                "artifact_name": destination.name,
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )
    return rows, config_rows, optional_rows


def _prepare_server(root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    installer = root / "neoforge-installer.jar"
    _download(
        str(profile["neoforge"]["installer_url"]),
        installer,
        str(profile["neoforge"]["installer_sha256"]),
    )
    subprocess.run(
        ["java", "-jar", str(installer), "--installServer", "."],
        cwd=server,
        check=True,
    )
    installer.unlink()
    expected_library = (
        server
        / "libraries"
        / "net"
        / "neoforged"
        / "neoforge"
        / str(profile["neoforge"]["version"])
    )
    if not (server / "run.sh").is_file() or not expected_library.is_dir():
        raise RuntimeError("NeoForge server installer did not produce the pinned runtime")
    return {
        "run_sh": "server/run.sh",
        "neoforge_library": str(
            Path("server") / expected_library.relative_to(server)
        ),
    }


def _wait_port(port: int, process: subprocess.Popen[Any], timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"runtime process exited early with {process.returncode}")
        with socket.socket() as handle:
            handle.settimeout(0.5)
            if handle.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for port {port}")


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _smoke_runtime(root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    if shutil.which("Xvfb") is None:
        raise RuntimeError("Xvfb is required for navigation runtime smoke")
    setting = load_setting("final-navigation-v1")
    smoke_root = root.parent / f".{root.name}.smoke"
    failure_root = root.parent / "smoke-failure"
    if smoke_root.exists():
        make_tree_writable(smoke_root)
        shutil.rmtree(smoke_root)
    if failure_root.exists():
        make_tree_writable(failure_root)
        shutil.rmtree(failure_root)
    smoke_root.mkdir()
    server_process: subprocess.Popen[Any] | None = None
    client_process: subprocess.Popen[Any] | None = None
    xvfb_process: subprocess.Popen[Any] | None = None
    server_log: Any | None = None
    client_log: Any | None = None
    checks: dict[str, Any] = {}
    succeeded = False
    try:
        _clone_tree(root / "server", smoke_root / "server")
        _clone_tree(root / "client", smoke_root / "client")
        ports = _allocate_ports(
            ("server", "rcon", "agentbridge", "remote_bash")
        )
        _configure_server(smoke_root / "server", setting, ports)
        display_number = 191
        xvfb_process = subprocess.Popen(
            ["Xvfb", f":{display_number}", "-screen", "0", "800x600x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        server_log = (smoke_root / "server.log").open("wb")
        server_process = subprocess.Popen(
            ["sh", "run.sh", "nogui"],
            cwd=smoke_root / "server",
            stdout=server_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_port(ports["server"], server_process, 180)
        client_log = (smoke_root / "client.log").open("wb")
        environment = _runtime_environment(profile)
        environment.update(
            {
                "DISPLAY": f":{display_number}",
                "AGENTBRIDGE_PORT": str(ports["agentbridge"]),
            }
        )
        game = smoke_root / "client" / "game"
        client_process = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/runtime/portablemc-neoforge-root-shim.py"),
                "--main-dir",
                str(game),
                "--work-dir",
                str(game),
                "start",
                f"neoforge:{profile['neoforge']['version']}",
                "--username",
                "NavSmoke",
                "-s",
                "127.0.0.1",
                "-p",
                str(ports["server"]),
            ],
            cwd=REPO_ROOT,
            env=environment,
            stdout=client_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        health_url = f"http://127.0.0.1:{ports['agentbridge']}/api/health"
        deadline = time.monotonic() + 240
        health: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if client_process.poll() is not None:
                raise RuntimeError("client exited before AgentBridge became healthy")
            try:
                with urllib.request.urlopen(health_url, timeout=3) as response:  # noqa: S310
                    health = json.load(response)
                if health.get("success"):
                    break
            except Exception:
                time.sleep(1)
        if (
            health is None
            or health.get("minecraft_version") != profile["minecraft"]["version"]
        ):
            raise RuntimeError(f"unexpected AgentBridge health readback: {health}")
        if health.get("neoforge_version") != profile["neoforge"]["version"]:
            raise RuntimeError(f"unexpected NeoForge readback: {health}")
        checks["health"] = health
        state_url = f"http://127.0.0.1:{ports['agentbridge']}/api/state"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(state_url, timeout=3) as response:  # noqa: S310
                    state = json.load(response)
                if state.get("success") is True:
                    checks["/api/state"] = state
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("client did not join the smoke server")
        endpoints = (
            "/api/input/MOVE_FORWARD/true",
            "/api/input/MOVE_FORWARD/false",
            "/api/look?yaw=15&pitch=0",
            "/api/close_gui",
            "/api/right_click",
        )
        for endpoint in endpoints:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{ports['agentbridge']}{endpoint}",
                timeout=5,
            ) as response:
                payload = json.load(response)
            if payload.get("success") is not True:
                raise RuntimeError(f"smoke endpoint failed {endpoint}: {payload}")
            checks[endpoint] = payload
        succeeded = True
        return checks
    finally:
        _stop_process(client_process)
        _stop_process(server_process)
        _stop_process(xvfb_process)
        if client_log is not None:
            client_log.close()
        if server_log is not None:
            server_log.close()
        if smoke_root.exists():
            if succeeded:
                make_tree_writable(smoke_root)
                shutil.rmtree(smoke_root)
            else:
                smoke_root.replace(failure_root)
                print(
                    f"runtime smoke diagnostics preserved at {failure_root}",
                    file=sys.stderr,
                )


def _install_runtime_tree(source: Path, destination: Path) -> None:
    if not destination.exists():
        source.replace(destination)
        return
    backup = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.previous.",
            dir=destination.parent,
        )
    )
    backup.rmdir()
    destination.replace(backup)
    try:
        source.replace(destination)
    except BaseException:
        backup.replace(destination)
        raise
    make_tree_writable(backup)
    shutil.rmtree(backup)


def main() -> int:
    args = parse_args()
    profile = load_profile("minecraft-1.21.11")
    destination = runtime_template_path(profile)
    if destination.exists():
        if not args.replace:
            raise SystemExit(f"runtime template already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    agentbridge = _load_bundled_mod(profile["agentbridge"], "AgentBridge")
    ground_navigation = _load_ground_navigation(profile)
    staging_override = os.environ.get("MCBOTS_NAV_RUNTIME_STAGING_ROOT")
    if staging_override:
        staging = Path(staging_override).expanduser().resolve()
        if staging == destination.resolve():
            raise SystemExit("runtime staging root must differ from destination")
        staging.mkdir(parents=True, exist_ok=True)
        staging_context = nullcontext(str(staging))
    else:
        staging_context = tempfile.TemporaryDirectory(
            prefix=".navigation-1.21.11.",
            dir=destination.parent,
        )
    with staging_context as temporary_name:
        temporary = Path(temporary_name)
        client_mods, client_configs, optional_client_mods = _prepare_client(
            temporary,
            profile,
            agentbridge,
            ground_navigation,
        )
        server = _prepare_server(temporary, profile)
        smoke = None if args.skip_smoke else _smoke_runtime(temporary, profile)
        receipt = {
            "schema_version": 1,
            "artifact_kind": "navigation-runtime-receipt",
            "status": "prepared" if args.skip_smoke else "smoke_verified",
            "prepared_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "profile_id": profile["profile_id"],
            "profile_digest": digest_json(profile),
            "minecraft_version": profile["minecraft"]["version"],
            "data_version": profile["minecraft"]["data_version"],
            "neoforge_version": profile["neoforge"]["version"],
            "runtime_platform": {
                "system": platform.system().lower(),
                "machine": platform.machine().lower(),
                "backend": os.environ.get("MCBOTS_NAV_RUNTIME_BACKEND", "host"),
                "linux_arm64_lwjgl_version": (
                    profile["portablemc"]["linux_arm64_lwjgl_version"]
                    if (
                        platform.system().lower() == "linux"
                        and platform.machine().lower() in {"aarch64", "arm64"}
                    )
                    else None
                ),
            },
            "client_mods": client_mods,
            "optional_client_mods": optional_client_mods,
            "client_configs": client_configs,
            "server": server,
            "smoke": smoke,
        }
        atomic_write_json(temporary / "runtime-receipt.json", receipt)
        _install_runtime_tree(temporary, destination)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

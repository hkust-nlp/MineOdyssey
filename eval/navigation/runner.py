"""Materialize isolated navigation runs from immutable prepared snapshots."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schema import (
    REPO_ROOT,
    SchemaError,
    TASK_TIME_TICKS,
    atomic_write_json,
    digest_json,
    load_map,
    load_profile,
    load_references,
    load_setting,
    load_waypoints,
    reference_digest,
    resolve_task_eval_setting,
    resolved_task,
    runtime_template_path,
    task_digest,
)
from .snapshots import make_tree_writable, sha256_file, verify_snapshot


RUNTIME_ROOT = Path(
    os.environ.get(
        "MCBOTS_NAV_RUNTIME_ROOT",
        REPO_ROOT / "eval" / "runtime" / "navigation",
    )
).expanduser().resolve()
RESULTS_ROOT = Path(
    os.environ.get(
        "MCBOTS_NAV_RESULTS_ROOT",
        REPO_ROOT / "eval" / "results" / "navigation",
    )
).expanduser().resolve()


def _safe_component(value: str, field: str) -> str:
    if not value or value != Path(value).name or value in {".", ".."}:
        raise SchemaError(f"unsafe {field}: {value!r}")
    return value


def _clone_tree(source: Path, destination: Path) -> str:
    if not source.is_dir():
        raise FileNotFoundError(f"copy source is missing: {source}")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite materialized path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    commands: list[tuple[list[str], str]] = []
    if sys.platform == "darwin":
        commands.append((["cp", "-cR", str(source), str(destination)], "apfs-clone"))
    else:
        commands.append(
            (
                ["cp", "-a", "--reflink=auto", str(source), str(destination)],
                "reflink-or-copy",
            )
        )
    for command, method in commands:
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode == 0:
            _assert_no_hardlinks(source, destination)
            make_tree_writable(destination)
            return method
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    _assert_no_hardlinks(source, destination)
    make_tree_writable(destination)
    return "python-copy"


def _assert_no_hardlinks(source: Path, destination: Path) -> None:
    for source_file in source.rglob("*"):
        if not source_file.is_file() or source_file.is_symlink():
            continue
        target = destination / source_file.relative_to(source)
        if not target.is_file():
            raise RuntimeError(f"copy is incomplete; missing {target}")
        source_stat = source_file.stat()
        target_stat = target.stat()
        if (
            source_stat.st_dev == target_stat.st_dev
            and source_stat.st_ino == target_stat.st_ino
        ):
            raise RuntimeError(f"hardlinked runtime file is forbidden: {target}")


def _remove_runtime_history(world: Path) -> list[str]:
    removed: list[str] = []
    lock = world / "session.lock"
    if lock.exists():
        lock.unlink()
        removed.append("session.lock")
    for relative in ("playerdata", "stats", "advancements"):
        path = world / relative
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            removed.append(relative + "/")
        elif path.exists() or path.is_symlink():
            path.unlink()
            removed.append(relative)
    return removed


def _set_property(path: Path, key: str, value: str) -> None:
    rows = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    prefix = key + "="
    output = [prefix + value if row.startswith(prefix) else row for row in rows]
    if not any(row.startswith(prefix) for row in rows):
        output.append(prefix + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _configure_server(server: Path, setting: Mapping[str, Any], ports: Mapping[str, int]) -> None:
    runtime = setting["runtime"]
    properties = {
        "level-name": "world",
        "server-port": str(ports["server"]),
        "enable-rcon": "true",
        "rcon.port": str(ports["rcon"]),
        "rcon.password": "minecraft",
        "online-mode": "false",
        "enforce-secure-profile": "false",
        "gamemode": str(runtime["gamemode"]),
        "force-gamemode": "true",
        "difficulty": str(runtime["difficulty"]),
        "allow-flight": str(bool(runtime["allow_flight"])).lower(),
        "spawn-protection": "0",
        "max-players": "1",
        "enable-command-block": "true",
        "spawn-animals": "false" if runtime["disable_mobs"] else "true",
        "spawn-monsters": "false" if runtime["disable_mobs"] else "true",
        "spawn-npcs": "false" if runtime["disable_mobs"] else "true",
    }
    for key, value in properties.items():
        _set_property(server / "server.properties", key, value)
    (server / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    (server / "user_jvm_args.txt").write_text("-Xms2G\n-Xmx4G\n", encoding="utf-8")


def _effective_task_settings(
    setting: Mapping[str, Any],
    task: Mapping[str, Any],
    eval_setting_overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    task_eval_setting = resolve_task_eval_setting(
        {
            **dict(task.get("eval_setting") or {}),
            **dict(eval_setting_overrides or {}),
        }
    )
    effective_runtime = dict(setting["runtime"])
    effective_runtime["time"] = {
        "value": TASK_TIME_TICKS[str(task_eval_setting["time"])],
        "freeze": True,
    }
    effective_runtime["weather"] = {
        "value": str(task_eval_setting["weather"]),
        "freeze": True,
    }
    effective_agent = dict(setting["agent"])
    effective_agent["six_view_enabled"] = bool(
        task_eval_setting["six_view_enabled"]
    )
    return task_eval_setting, effective_runtime, effective_agent


def _xaero_line(waypoint: Mapping[str, Any]) -> str:
    safe_name = str(waypoint["name"]).replace(":", "-").replace("\n", " ").strip()
    safe_initials = (
        str(waypoint.get("initials") or safe_name[:1])
        .replace(":", "-")
        .replace("\n", " ")
        .strip()
    )
    position = waypoint["position"]
    xaero = waypoint.get("xaero") or {}
    color = waypoint.get("color")
    if color is None:
        color = 0
    return (
        f"waypoint:{safe_name}:{safe_initials}:"
        f"{position['x']}:{position['y']}:{position['z']}:{color}:"
        f"{str(bool(xaero.get('disabled', False))).lower()}:"
        f"{xaero.get('type', 0)}:{xaero.get('set', 'gui.xaero_default')}:"
        f"{str(bool(xaero.get('rotate_on_tp', False))).lower()}:"
        f"{xaero.get('tp_yaw', 0)}:{xaero.get('visibility_type', 0)}:"
        f"{str(bool(xaero.get('destination', False))).lower()}"
    )


def _write_xaero_profile(path: Path, settings: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key} = {value}\n" for key, value in settings.items()),
        encoding="utf-8",
    )


def _configure_client_target(
    client: Path,
    *,
    selected_waypoints: Sequence[Mapping[str, Any]],
    waypoint_in_world_max_distance: int = 32,
    hud_enabled: bool = False,
    coordinate_lock_enabled: bool = True,
) -> Path:
    game = client / "game"
    xaero_root = game / "xaero"
    if xaero_root.exists():
        shutil.rmtree(xaero_root)
    for backup in game.glob("XaeroWaypoints_BACKUP*"):
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()

    waypoint = (
        xaero_root
        / "minimap"
        / "Multiplayer_127.0.0.1"
        / "dim%0"
        / "mw$default_1.txt"
    )
    waypoint.parent.mkdir(parents=True, exist_ok=True)
    waypoint_lines = [_xaero_line(row) for row in selected_waypoints]
    waypoint.write_text(
        "\n".join(
            [
                "sets:gui.xaero_default",
                "#",
                "#waypoint:name:initials:x:y:z:color:disabled:type:set:rotate_on_tp:tp_yaw:visibility_type:destination",
                "#",
                *waypoint_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = game / "config"
    config.mkdir(parents=True, exist_ok=True)
    xaero_config = config / "xaero"
    if xaero_config.exists():
        shutil.rmtree(xaero_config)
    for legacy_config in ("xaerominimap.txt", "xaeroworldmap.txt"):
        legacy_path = config / legacy_config
        if legacy_path.exists():
            legacy_path.unlink()

    minimap_profile = xaero_config / "minimap" / "profiles" / "default.cfg"
    world_map_profile = xaero_config / "world-map" / "profiles" / "default.cfg"
    _write_xaero_profile(
        minimap_profile,
        {
            "display_minimap": str(hud_enabled).lower(),
            "waypoints_in_world": "true",
            "waypoint_name_in_world": "true",
            "waypoint_distance_in_world": "2",
            "waypoint_short_distance_in_world": "true",
            "multiple_waypoints_info": "2",
            "waypoint_icon_scale_in_world": "0",
            "waypoint_name_scale_in_world": "0",
            "waypoint_close_scale_in_world": "1.0",
            "waypoint_distance_scale_in_world": "0",
            "waypoint_icon_scale_on_minimap": "0",
            "waypoint_opacity_in_world": "80",
            # Applies only to Xaero's in-world/first-person waypoint renderer.
            # Local waypoints remain visible on the minimap and world map.
            "waypoint_max_distance": str(
                0 if hud_enabled else waypoint_in_world_max_distance
            ),
            "waypoint_min_distance_in_world": "0.0",
            "waypoint_distance_precision": "1",
            "hide_waypoint_coordinates": str(coordinate_lock_enabled).lower(),
            "tracked_players_in_world": "false",
            "waypoints_on_minimap": "true",
            "deathpoints": "false",
            "old_deathpoints": "false",
            "switch_auto_waypoints_on_death": "false",
            "display_radar": "false",
            "tracked_players_on_minimap": "false",
            "minimap_cave_mode_allowed": "false",
            "minimap_auto_cave_mode": "0",
            "default_waypoint_teleport_format": "",
            "default_waypoint_teleport_rotation_format": "",
            "waypoint_teleport_cross_dimension": "false",
            "waypoint_partial_y_teleport": "false",
        },
    )
    _write_xaero_profile(
        world_map_profile,
        {
            "display_coordinates": str(not coordinate_lock_enabled).lower(),
            "waypoints": "true",
            "render_waypoints": "true",
            "cave_mode_allowed": "false",
            "auto_cave_mode": "-1",
            "default_map_teleport_command_format": "",
            "default_map_teleport_command_dimension_format": "",
            "default_player_teleport_command_format": "",
            "map_teleport_allowed": "false",
            "partial_y_teleport": "false",
            "display_minimap_radar": "false",
            "display_tracked_players": "false",
        },
    )
    info_display = minimap_profile.parent / "info_display_config" / "default.cfg.txt"
    info_display.parent.mkdir(parents=True, exist_ok=True)
    info_display.write_text(
        "infoDisplayOrder:coords:overworld_coords:chunk_coords:angles:dimension:"
        "biome:weather:light_level:time:real_time:highlights:"
        "light_overlay_indicator:manual_cave_mode_indicator:custom_sub_world\n"
        "infoDisplay:coords:true:15:-1\n"
        "infoDisplay:overworld_coords:false:15:-1\n"
        "infoDisplay:chunk_coords:false:15:-1\n"
        "infoDisplay:angles:false:15:-1\n"
        "infoDisplay:dimension:false:15:-1\n"
        "infoDisplay:biome:false:15:-1\n"
        "infoDisplay:weather:false:15:-1\n"
        "infoDisplay:light_level:0:15:-1\n"
        "infoDisplay:time:0:15:-1\n"
        "infoDisplay:real_time:0:15:-1\n"
        "infoDisplay:highlights:false:15:-1\n"
        "infoDisplay:light_overlay_indicator:false:15:-1\n"
        "infoDisplay:manual_cave_mode_indicator:false:15:-1\n"
        "infoDisplay:custom_sub_world:false:15:-1\n",
        encoding="utf-8",
    )

    minihud_path = config / "minihud.json"
    if not minihud_path.is_file():
        raise SchemaError(f"missing BoccHUD config: {minihud_path}")
    minihud = json.loads(minihud_path.read_text(encoding="utf-8"))
    if not isinstance(minihud, dict):
        raise SchemaError(f"BoccHUD config must be a JSON object: {minihud_path}")
    generic = minihud.get("Generic")
    if not isinstance(generic, dict):
        raise SchemaError(f"BoccHUD config is missing Generic: {minihud_path}")
    main_rendering_toggle = generic.get("mainRenderingToggle")
    if not isinstance(main_rendering_toggle, dict):
        raise SchemaError(
            f"BoccHUD config is missing Generic.mainRenderingToggle: {minihud_path}"
        )
    main_rendering_toggle["enabled"] = hud_enabled
    atomic_write_json(minihud_path, minihud)
    return waypoint


def _task_waypoint_rows(
    resolved: Mapping[str, Any],
    waypoint_catalog: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return every task waypoint in route order for the client map.

    Loop tasks can use the same waypoint as both their start and target. Xaero
    only needs one marker for that location, so repeated IDs are removed while
    preserving the first occurrence.
    """

    ordered_ids = [
        str(resolved["start"]["waypoint_id"]),
        *(
            str(row["waypoint_id"])
            for row in resolved["required_waypoints"]
        ),
        str(resolved["target"]["waypoint_id"]),
    ]
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for waypoint_id in ordered_ids:
        if waypoint_id in seen:
            continue
        seen.add(waypoint_id)
        selected.append(waypoint_catalog[waypoint_id])
    return selected


def _allocate_ports(names: tuple[str, ...]) -> dict[str, int]:
    handles: list[socket.socket] = []
    try:
        output: dict[str, int] = {}
        for name in names:
            handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            handle.bind(("127.0.0.1", 0))
            handles.append(handle)
            output[name] = int(handle.getsockname()[1])
        return output
    finally:
        for handle in handles:
            handle.close()


def validate_runtime_template(
    profile: Mapping[str, Any],
    *,
    require_smoke: bool,
) -> dict[str, Any]:
    root = runtime_template_path(profile)
    receipt_path = root / "runtime-receipt.json"
    if not receipt_path.is_file():
        raise RuntimeError(
            f"navigation runtime is not prepared: {receipt_path}; "
            "run scripts/eval/prepare-navigation-runtime.py"
        )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise RuntimeError(f"invalid runtime receipt: {receipt_path}")
    if receipt.get("artifact_kind") != "navigation-runtime-receipt":
        raise RuntimeError(f"invalid runtime receipt kind: {receipt_path}")
    if receipt.get("status") not in {"prepared", "smoke_verified"}:
        raise RuntimeError(f"invalid runtime receipt status: {receipt_path}")
    if receipt.get("profile_digest") != digest_json(profile):
        raise RuntimeError("navigation runtime receipt is stale for the tracked profile")
    expected_versions = {
        "minecraft_version": profile["minecraft"]["version"],
        "data_version": profile["minecraft"]["data_version"],
        "neoforge_version": profile["neoforge"]["version"],
    }
    for field, expected in expected_versions.items():
        if receipt.get(field) != expected:
            raise RuntimeError(f"navigation runtime receipt has stale {field}")
    runtime_platform = receipt.get("runtime_platform")
    if not isinstance(runtime_platform, dict):
        raise RuntimeError("navigation runtime receipt lacks runtime_platform")
    for field in ("system", "machine", "backend"):
        if not isinstance(runtime_platform.get(field), str) or not runtime_platform[field]:
            raise RuntimeError(
                f"navigation runtime receipt has invalid runtime_platform.{field}"
            )
    if require_smoke and receipt.get("status") != "smoke_verified":
        raise RuntimeError("formal navigation requires a smoke_verified runtime receipt")
    if receipt.get("status") == "smoke_verified":
        smoke = receipt.get("smoke")
        required_smoke_keys = {
            "health",
            "/api/state",
            "/api/input/MOVE_FORWARD/true",
            "/api/input/MOVE_FORWARD/false",
            "/api/look?yaw=15&pitch=0",
            "/api/close_gui",
            "/api/right_click",
        }
        if not isinstance(smoke, dict) or not required_smoke_keys.issubset(smoke):
            raise RuntimeError("navigation runtime receipt lacks required smoke checks")
    mods = root / "client" / "game" / "mods"
    expected_names = {
        str(row["artifact_name"]): str(row["sha256"])
        for row in profile["client_mods"]["required"]
    }
    active = {path.name: sha256_file(path) for path in mods.glob("*.jar")}
    if active != expected_names:
        raise RuntimeError(
            "runtime client mod inventory differs from the exact tracked profile"
        )
    optional_receipt = receipt.get("optional_client_mods")
    if not isinstance(optional_receipt, dict):
        raise RuntimeError("navigation runtime receipt lacks optional client mods")
    for profile_name, rows in profile["client_mods"]["optional_profiles"].items():
        optional_root = root / "client" / "optional-mods" / profile_name
        expected_optional = {
            str(row["artifact_name"]): str(row["sha256"])
            for row in rows
        }
        actual_optional = {
            path.name: sha256_file(path) for path in optional_root.glob("*.jar")
        }
        if actual_optional != expected_optional:
            raise RuntimeError(
                f"runtime optional mod profile {profile_name} differs from the tracked profile"
            )
        receipt_rows = optional_receipt.get(profile_name)
        if not isinstance(receipt_rows, list) or {
            str(row.get("artifact_name")): str(row.get("sha256"))
            for row in receipt_rows
            if isinstance(row, dict)
        } != expected_optional:
            raise RuntimeError(
                f"runtime receipt has stale optional mod profile {profile_name}"
            )
    config_root = root / "client" / "game" / "config"
    for row in profile["client_configs"]:
        config_path = config_root / str(row["artifact_name"])
        if not config_path.is_file() or sha256_file(config_path) != row["sha256"]:
            raise RuntimeError(
                "runtime client config differs from the exact tracked profile"
            )
    if not (root / "server" / "run.sh").is_file():
        raise RuntimeError("runtime server template lacks NeoForge run.sh")
    neoforge_library = (
        root
        / "server"
        / "libraries"
        / "net"
        / "neoforged"
        / "neoforge"
        / str(profile["neoforge"]["version"])
    )
    if not neoforge_library.is_dir():
        raise RuntimeError("runtime server template has the wrong NeoForge version")
    return receipt


def _activate_optional_client_profile(
    client_root: Path,
    profile: Mapping[str, Any],
    profile_name: str,
) -> list[str]:
    rows = profile["client_mods"]["optional_profiles"][profile_name]
    source_root = client_root / "optional-mods" / profile_name
    mods_root = client_root / "game" / "mods"
    activated: list[str] = []
    for row in rows:
        source = source_root / str(row["artifact_name"])
        if not source.is_file() or sha256_file(source) != row["sha256"]:
            raise RuntimeError(
                f"optional client mod is missing or stale: {source}"
            )
        destination = mods_root / source.name
        shutil.copy2(source, destination)
        if sha256_file(destination) != row["sha256"]:
            raise RuntimeError(f"activated optional client mod hash mismatch: {destination}")
        activated.append(destination.name)
    return activated


def _write_guideline_baritone_settings(client_root: Path) -> Path:
    path = client_root / "game" / "baritone" / "settings.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "chatControl false\n"
        "chatControlAnyway false\n"
        "prefixControl false\n"
        "echoCommands false\n"
        "chatDebug false\n"
        "renderPath false\n"
        "renderGoal false\n"
        "allowBreak false\n"
        "allowPlace false\n"
        "allowInventory false\n",
        encoding="utf-8",
    )
    return path


def materialize_task(
    *,
    run_id: str,
    map_id: str,
    task: Mapping[str, Any],
    mode: str,
    benchmark: Mapping[str, Any],
    model_parameters: Mapping[str, Any],
    require_smoke: bool,
    vnc: bool,
    record_video: bool = False,
    worker_resources: Mapping[str, Any] | None = None,
    runtime_allocation: Mapping[str, Any] | None = None,
    eval_setting_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_run_id = _safe_component(run_id, "run ID")
    task_id = _safe_component(str(task["id"]), "task ID")
    if mode not in {"review", "pilot", "formal"}:
        raise ValueError(f"unsupported navigation mode: {mode}")
    profile = load_profile(str(benchmark["profile_id"]))
    setting = load_setting(str(benchmark["setting_id"]))
    runtime_receipt = validate_runtime_template(profile, require_smoke=require_smoke)
    map_payload = load_map(map_id)
    snapshot_before = verify_snapshot(map_payload)
    resolved = resolved_task(map_id, task)
    task_eval_setting, effective_runtime, effective_agent = _effective_task_settings(
        setting, task, eval_setting_overrides
    )
    if mode == "formal" and not task_eval_setting["coordinate_lock_enabled"]:
        raise ValueError("formal navigation runs require coordinate_lock_enabled=true")
    effective_setting = {**setting, "runtime": effective_runtime, "agent": effective_agent}
    reference: dict[str, Any] | None = None
    try:
        reference = load_references(map_id)[task_id]
    except (SchemaError, OSError, ValueError):
        pass
    if reference is not None and reference["status"] != "reachable":
        reference = None

    runtime_dir = RUNTIME_ROOT / safe_run_id / task_id
    results_dir = RESULTS_ROOT / safe_run_id / task_id
    if runtime_dir.exists() or results_dir.exists():
        raise FileExistsError(
            f"run already materialized for {safe_run_id}/{task_id}"
        )
    for child in ("control", "logs"):
        (runtime_dir / child).mkdir(parents=True, exist_ok=False)
    results_dir.mkdir(parents=True, exist_ok=False)
    template = runtime_template_path(profile)
    server_copy = _clone_tree(template / "server", runtime_dir / "server")
    client_copy = _clone_tree(template / "client", runtime_dir / "client")
    active_optional_profiles: list[str] = []
    activated_optional_mods: list[str] = []
    if task_eval_setting["guideline"]:
        active_optional_profiles.append("guideline")
        activated_optional_mods.extend(
            _activate_optional_client_profile(
                runtime_dir / "client",
                profile,
                "guideline",
            )
        )
        _write_guideline_baritone_settings(runtime_dir / "client")
    world_copy = _clone_tree(
        Path(snapshot_before["world_dir"]),
        runtime_dir / "server" / "world",
    )
    removed = _remove_runtime_history(runtime_dir / "server" / "world")
    ports = _allocate_ports(
        ("server", "rcon", "agentbridge", "remote_bash")
    )
    _configure_server(runtime_dir / "server", effective_setting, ports)
    waypoint_catalog = load_waypoints(map_id)
    selected_waypoints = _task_waypoint_rows(resolved, waypoint_catalog)
    waypoint_path = _configure_client_target(
        runtime_dir / "client",
        selected_waypoints=selected_waypoints,
        waypoint_in_world_max_distance=int(
            setting["runtime"]["waypoint_in_world_max_distance"]
        ),
        hud_enabled=bool(task_eval_setting["hud_enabled"]),
        coordinate_lock_enabled=bool(task_eval_setting["coordinate_lock_enabled"]),
    )
    control = runtime_dir / "control"
    for child in ("claim-requests", "claim-responses"):
        (control / child).mkdir()

    run_payload = {
        "schema_version": 1,
        "artifact_kind": "navigation-run",
        "benchmark_id": benchmark["benchmark_id"],
        "run_id": safe_run_id,
        "task_id": task_id,
        "map_id": map_id,
        "mode": mode,
        "formal_eligible": (
            mode == "formal"
            and benchmark["task_admission"]["policy"]
            == "benchmark_catalog_owner_approved"
        ),
        "profile_id": benchmark["profile_id"],
        "profile_digest": digest_json(profile),
        "runtime_versions": {
            "minecraft": profile["minecraft"]["version"],
            "data_version": profile["minecraft"]["data_version"],
            "neoforge": profile["neoforge"]["version"],
        },
        "setting_id": benchmark["setting_id"],
        "setting_digest": digest_json(setting),
        "runtime": effective_runtime,
        "agent": effective_agent,
        "eval_setting": task_eval_setting,
        "eval_setting_overrides": dict(eval_setting_overrides or {}),
        "arrival": dict(setting["arrival"]),
        "limits": dict(setting["limits"]),
        "map_fingerprint": snapshot_before["fingerprint"]["value"],
        "source_map_fingerprint": snapshot_before["source"]["fingerprint"]["value"],
        "snapshot_preparation_digest": snapshot_before["receipt"][
            "preparation_config_digest"
        ],
        "task_digest": task_digest(map_id, task),
        "reference_digest": (
            reference_digest(reference) if reference is not None else None
        ),
        "reference_length_blocks": (
            reference["length_blocks"] if reference is not None else None
        ),
        "reference_status": (
            str(reference["status"]) if reference is not None else "not_available"
        ),
        "validation_receipt_digest": None,
        "task": resolved,
        "model_parameters": dict(model_parameters),
        "runtime_receipt_digest": digest_json(runtime_receipt),
        "runtime_dir": str(runtime_dir),
        "results_dir": str(results_dir),
        "ports": ports,
        "vnc": bool(vnc),
        "record_video": bool(record_video),
        "worker_resources": dict(worker_resources or {}),
        "runtime_allocation": dict(runtime_allocation or {}),
        "active_optional_mod_profiles": active_optional_profiles,
        "activated_optional_client_mods": activated_optional_mods,
        "copies": {
            "server_template": server_copy,
            "client_template": client_copy,
            "world": world_copy,
            "removed_runtime_world_history": removed,
            "world_snapshot": {
                "kind": "prepared",
                "source_fingerprint": snapshot_before["source"]["fingerprint"][
                    "value"
                ],
                "prepared_fingerprint": snapshot_before["fingerprint"]["value"],
                "preparation_id": snapshot_before["receipt"]["preparation_id"],
                "replacement_manifest_sha256": snapshot_before["receipt"].get(
                    "replacement_manifest_sha256"
                ),
            },
        },
        "xaero_target_file": str(waypoint_path),
    }
    atomic_write_json(control / "run.json", run_payload)
    atomic_write_json(results_dir / "run.json", run_payload)
    snapshot_after = verify_snapshot(map_payload)
    if (
        snapshot_after["fingerprint"] != snapshot_before["fingerprint"]
        or snapshot_after["source"]["fingerprint"]
        != snapshot_before["source"]["fingerprint"]
    ):
        raise RuntimeError("source or prepared snapshot changed during materialization")
    return run_payload

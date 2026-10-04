#!/usr/bin/env python3
"""Prepare and operate the host-native navigation review fleet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.snapshots import NbtReader, make_tree_writable  # noqa: E402


DOWNLOADS = REPO_ROOT / "downloads"
FLEET_ROOT = REPO_ROOT / "eval/runtime/navigation-review-fleet"
SERVER_TEMPLATE = REPO_ROOT / "eval/templates/_local/navigation/1.21.11/server"
CLIENT_TEMPLATE = REPO_ROOT / "eval/templates/_local/navigation/1.21.11/client/game"
CLIENT_ROOT = REPO_ROOT / "eval/templates/_local/navigation-review-client"
HEIGHTS_PACK = REPO_ROOT / "eval/navigation/compatibility/heights-1.21.11"
RELEASE_MANIFEST = (
    REPO_ROOT / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
)
MINECRAFT_ROOT = Path.home() / "Library/Application Support/minecraft"
PROFILE_ID = "mcbots-navigation-review-12111"
PREFERRED_PLAYER = "224fb2e6-70ac-46dc-ab19-2db9d5942639.dat"
BASE_PORT = 25565
BASE_RCON_PORT = 26565
RCON_PASSWORD = "mcbots-local-review"
LEGACY_HEIGHT_PACKS = {
    "heights",
    "heights.zip",
    "world-height-datapack.zip",
    "UK121-world-height",
    "UK121worldheight",
}


@dataclass(frozen=True)
class MapServer:
    index: int
    map_id: str
    slug: str
    name: str
    archive: Path
    archive_bytes: int
    archive_sha256: str
    world_root: str
    spawn: tuple[int, int, int]
    waypoint_count: int
    route_count: int
    prepared_world: Path | None = None

    @property
    def port(self) -> int:
        return BASE_PORT + self.index - 1

    @property
    def rcon_port(self) -> int:
        return BASE_RCON_PORT + self.index - 1

    @property
    def client_host(self) -> str:
        return f"{self.slug}.localhost"

    @property
    def instance(self) -> Path:
        return FLEET_ROOT / "servers" / self.slug

    @property
    def has_waypoints(self) -> bool:
        return self.waypoint_count > 0

    @property
    def has_routes(self) -> bool:
        return self.route_count > 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    result = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in result.split("-") if part)


def _declared_rows(map_id: str, filename: str, count_key: str, rows_key: str) -> int:
    path = REPO_ROOT / "eval/navigation/maps" / map_id / filename
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("map_id") != map_id:
        raise RuntimeError(f"map_id mismatch in {path}")
    rows = payload.get(rows_key)
    declared = payload.get(count_key)
    if not isinstance(rows, list) or declared != len(rows):
        raise RuntimeError(f"invalid {rows_key} inventory in {path}")
    return len(rows)


def annotation_counts(map_id: str) -> tuple[int, int]:
    waypoint_count = _declared_rows(
        map_id, "waypoints.json", "waypoint_count", "waypoints"
    )
    route_count = _declared_rows(map_id, "routes.json", "route_count", "routes")
    if route_count == 0:
        route_count = _declared_rows(map_id, "tasks.json", "task_count", "tasks")
    return waypoint_count, route_count


def availability_label(item: MapServer) -> str:
    waypoint = "YES" if item.has_waypoints else "NO"
    route = "YES" if item.has_routes else "NO"
    return f"Waypoint:{waypoint} Route:{route}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise RuntimeError(f"unsafe ZIP member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe ZIP member: {name!r}")
    return path


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def _read_world(world: Path) -> tuple[dict[str, object], tuple[int, int, int]]:
    root = NbtReader(gzip.decompress((world / "level.dat").read_bytes())).root()
    data = root.get("Data", root)
    players = sorted((world / "playerdata").glob("*.dat"))
    preferred = next((path for path in players if path.name == PREFERRED_PLAYER), None)
    player_path = preferred or (players[0] if players else None)
    pos = None
    if player_path:
        player = NbtReader(gzip.decompress(player_path.read_bytes())).root()
        pos = player.get("Pos")
    if not isinstance(pos, list) or len(pos) != 3:
        raise RuntimeError(f"no review spawn position in {world}")
    return data, tuple(math.floor(float(value)) for value in pos)


def inspect_archive(
    index: int,
    archive: Path,
    release_row: dict[str, object],
) -> MapServer:
    map_id = str(release_row["map_id"])
    expected_bytes = int(release_row["upstream_archive_bytes"])
    if archive.stat().st_size != expected_bytes:
        raise RuntimeError(
            f"source ZIP size mismatch for {archive.name}: "
            f"expected {expected_bytes}, found {archive.stat().st_size}"
        )
    with zipfile.ZipFile(archive) as handle:
        bad = handle.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC failure in {archive.name}: {bad}")
        levels = [
            info.filename
            for info in handle.infolist()
            if info.filename.endswith("level.dat") and "__MACOSX" not in info.filename
        ]
        if not levels:
            raise RuntimeError(f"no level.dat in {archive.name}")
        level_name = min(levels, key=lambda item: (item.count("/"), len(item)))
        world_root = level_name[: -len("level.dat")]
        root = NbtReader(gzip.decompress(handle.read(level_name))).root()
        data = root.get("Data", root)
        version = data.get("Version", {})
        source_version = version.get("Name")
        source_data_version = data.get("DataVersion")
        players = sorted(
            info.filename
            for info in handle.infolist()
            if info.filename.startswith(world_root + "playerdata/") and info.filename.endswith(".dat")
        )
        preferred = next((item for item in players if item.endswith(PREFERRED_PLAYER)), None)
        if not preferred and players:
            preferred = players[0]
        if preferred:
            player = NbtReader(gzip.decompress(handle.read(preferred))).root()
            pos = player.get("Pos")
        else:
            pos = None
        if not isinstance(pos, list) or len(pos) != 3:
            raise RuntimeError(f"no review spawn position in {archive.name}")
        spawn = tuple(math.floor(float(value)) for value in pos)
    stem = archive.stem
    slug = slugify(stem)
    name = str(release_row["display_name"])
    waypoint_count, route_count = annotation_counts(map_id)
    prepared_world = None
    if source_version != "1.21.11" or source_data_version != 4671:
        map_path = REPO_ROOT / "eval/navigation/maps" / map_id / "map.json"
        prepared_world = (
            REPO_ROOT
            / "eval/snapshots/_cache/navigation/1.21.11"
            / map_id
            / "prepared/world"
        )
        if not map_path.is_file() or not prepared_world.is_dir():
            raise RuntimeError(
                f"{archive.name} needs a verified Minecraft 1.21.11 prepared snapshot"
            )
        metadata = json.loads(map_path.read_text(encoding="utf-8"))
        expected_source = metadata["world"]["source_version"]
        if (
            expected_source["version_name"] != source_version
            or expected_source["data_version"] != source_data_version
        ):
            raise RuntimeError(f"source version metadata mismatch for {archive.name}")
        prepared_data, spawn = _read_world(prepared_world)
        prepared_version = prepared_data.get("Version", {})
        if prepared_version.get("Name") != "1.21.11" or prepared_data.get("DataVersion") != 4671:
            raise RuntimeError(f"prepared snapshot for {archive.name} is not Minecraft 1.21.11")
    return MapServer(
        index,
        map_id,
        slug,
        name,
        archive,
        expected_bytes,
        str(release_row["upstream_archive_sha256"]),
        world_root,
        spawn,
        waypoint_count,
        route_count,
        prepared_world,
    )


def discover_maps() -> list[MapServer]:
    payload = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    release_rows = payload["assets"]
    expected_names = [row["upstream_archive"] for row in release_rows]
    archives_by_name = {path.name: path for path in DOWNLOADS.glob("*.zip")}
    missing = [name for name in expected_names if name not in archives_by_name]
    unexpected = sorted(set(archives_by_name) - set(expected_names))
    if missing or unexpected:
        raise RuntimeError(
            f"source ZIP inventory does not match {RELEASE_MANIFEST}: "
            f"missing={missing}, unexpected={unexpected}"
        )
    archives = [archives_by_name[name] for name in expected_names]
    maps = [
        inspect_archive(index, archive, release_row)
        for index, (archive, release_row) in enumerate(
            zip(archives, release_rows, strict=True), 1
        )
    ]
    if len({item.slug for item in maps}) != len(maps):
        raise RuntimeError("map slugs are not unique")
    return maps


def extract_world(item: MapServer, destination: Path) -> None:
    destination.mkdir(parents=True)
    prefix = item.world_root
    with zipfile.ZipFile(item.archive) as handle:
        for info in handle.infolist():
            member = _safe_member(info.filename)
            if _zip_is_symlink(info):
                raise RuntimeError(f"ZIP symlink is forbidden: {info.filename}")
            normalized = member.as_posix()
            if not normalized.startswith(prefix):
                continue
            relative = PurePosixPath(normalized[len(prefix) :])
            if not relative.parts:
                continue
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)


def normalize_height_pack(world: Path) -> list[str]:
    datapacks = world / "datapacks"
    datapacks.mkdir(exist_ok=True)
    removed: list[str] = []
    for name in LEGACY_HEIGHT_PACKS:
        target = datapacks / name
        if not target.exists():
            continue
        removed.append(name)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copytree(HEIGHTS_PACK, datapacks / "heights")
    return sorted(removed)


def server_properties(item: MapServer) -> str:
    values = {
        "allow-flight": "true",
        "difficulty": "peaceful",
        "enable-command-block": "false",
        "enable-rcon": "true",
        "enforce-secure-profile": "true",
        "force-gamemode": "true",
        "gamemode": "creative",
        "generate-structures": "false",
        "level-name": "world",
        "max-players": "4",
        "motd": (
            f"MCBots Review {item.index:02d} - {item.name} - "
            f"{availability_label(item)}"
        ),
        "online-mode": "true",
        "pause-when-empty-seconds": "-1",
        "pvp": "false",
        "rcon.password": RCON_PASSWORD,
        "rcon.port": str(item.rcon_port),
        "server-ip": "127.0.0.1",
        "server-port": str(item.port),
        "simulation-distance": "4",
        "spawn-animals": "false",
        "spawn-monsters": "false",
        "spawn-npcs": "false",
        "spawn-protection": "0",
        "sync-chunk-writes": "false",
        "view-distance": "8",
        "white-list": "false",
    }
    return "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"


def prepare(
    maps: list[MapServer],
    *,
    excluded_map_ids: set[str] | None = None,
) -> None:
    if not (SERVER_TEMPLATE / "libraries/net/neoforged/neoforge/21.11.44/unix_args.txt").is_file():
        raise RuntimeError("the verified NeoForge 21.11.44 server template is missing")
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    total = len(maps)
    for progress, item in enumerate(maps, 1):
        instance = item.instance
        world = instance / "world"
        source_digest = sha256_file(item.archive)
        if source_digest != item.archive_sha256:
            raise RuntimeError(
                f"source ZIP SHA-256 mismatch for {item.archive.name}: "
                f"expected {item.archive_sha256}, found {source_digest}"
            )
        expected_receipt = {
            "schema_version": 1,
            "map_id": item.map_id,
            "archive": item.archive.name,
            "archive_bytes": item.archive_bytes,
            "archive_sha256": item.archive_sha256,
            "minecraft": "1.21.11",
            "data_version": 4671,
            "neoforge": "21.11.44",
        }
        receipt_path = instance / "fleet-source-receipt.json"
        try:
            current_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            current_receipt = None
        rebuild = not world.is_dir() or current_receipt != expected_receipt
        if rebuild:
            pid = read_pid(item)
            if pid and pid_running(pid):
                raise RuntimeError(
                    f"cannot rebuild running server {item.name}; stop the fleet first"
                )
            temporary = instance.with_name(instance.name + ".preparing")
            if temporary.exists():
                shutil.rmtree(temporary)
            if item.prepared_world:
                shutil.copytree(item.prepared_world, temporary / "world")
                make_tree_writable(temporary / "world")
                removed = []
            else:
                extract_world(item, temporary / "world")
                removed = normalize_height_pack(temporary / "world")
            (temporary / "world/session.lock").unlink(missing_ok=True)
            (temporary / "eula.txt").write_text("eula=true\n", encoding="utf-8")
            (temporary / "server.properties").write_text(server_properties(item), encoding="utf-8")
            (temporary / "fleet-source-receipt.json").write_text(
                json.dumps(expected_receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (temporary / "user_jvm_args.txt").write_text(
                "-Xms256M\n-Xmx1024M\n-XX:+UseG1GC\n-Dlog4j2.formatMsgNoLookups=true\n",
                encoding="utf-8",
            )
            libraries = temporary / "libraries"
            libraries.symlink_to(SERVER_TEMPLATE / "libraries", target_is_directory=True)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            if instance.exists():
                shutil.rmtree(instance)
            temporary.rename(instance)
            print(
                f"prepared {progress:02d}/{total} {item.name} "
                f"(replaced packs: {', '.join(removed) or 'none'})",
                flush=True,
            )
        else:
            (instance / "server.properties").write_text(server_properties(item), encoding="utf-8")
            print(f"current  {progress:02d}/{total} {item.name}", flush=True)
        manifest.append(
            {
                "index": item.index,
                "map_id": item.map_id,
                "slug": item.slug,
                "name": item.name,
                "archive": item.archive.name,
                "archive_sha256": item.archive_sha256,
                "host": item.client_host,
                "port": item.port,
                "rcon_port": item.rcon_port,
                "spawn": list(item.spawn),
                "waypoint_count": item.waypoint_count,
                "route_count": item.route_count,
            }
        )
    payload = {
        "schema_version": 1,
        "minecraft": "1.21.11",
        "data_version": 4671,
        "neoforge": "21.11.44",
        "excluded_map_ids": sorted(excluded_map_ids or set()),
        "prepared_at_utc": utc_now(),
        "maps": manifest,
    }
    (FLEET_ROOT / "fleet.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _nbt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def write_servers_dat(maps: list[MapServer], path: Path) -> None:
    output = bytearray(b"\x0a\x00\x00")
    output.extend(b"\x09" + _nbt_string("servers") + b"\x0a" + struct.pack(">i", len(maps)))
    for item in maps:
        output.extend(
            b"\x08"
            + _nbt_string("name")
            + _nbt_string(
                f"{item.index:02d} | {item.name} | {availability_label(item)}"
            )
        )
        output.extend(
            b"\x08"
            + _nbt_string("ip")
            + _nbt_string(f"{item.client_host}:{item.port}")
        )
        output.extend(b"\x01" + _nbt_string("hidden") + b"\x00")
        output.extend(b"\x00")
    output.extend(b"\x00")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


def _xaero_bool(value: object) -> str:
    return "true" if bool(value) else "false"


def install_xaero_waypoints(maps: list[MapServer]) -> tuple[int, int]:
    installed_maps = 0
    installed_points = 0
    for item in maps:
        destination_dir = (
            CLIENT_ROOT
            / "xaero/minimap"
            / f"Multiplayer_{item.client_host}"
            / "dim%0"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        for stale in destination_dir.glob("mw$default*.txt"):
            stale.unlink()
        if not item.has_waypoints:
            continue
        source = (
            REPO_ROOT
            / "eval/navigation/maps"
            / item.map_id
            / "waypoints.json"
        )
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload["waypoints"]
        if payload.get("map_id") != item.map_id or len(rows) != item.waypoint_count:
            raise RuntimeError(f"stale waypoint inventory in {source}")
        lines = [
            "#",
            (
                "#waypoint:name:initials:x:y:z:color:disabled:type:set:"
                "rotate_on_tp:tp_yaw:visibility_type:destination"
            ),
            "#",
        ]
        for row in rows:
            name = str(row["name"])
            initials = str(row.get("initials") or name[:1])
            if any(character in name + initials for character in (":", "\n", "\r")):
                raise RuntimeError(
                    f"Xaero delimiter in {item.map_id} waypoint {row.get('id')}"
                )
            position = row["position"]
            xaero = row.get("xaero", {})
            fields = [
                "waypoint",
                name,
                initials,
                str(position["x"]),
                str(position["y"]),
                str(position["z"]),
                str(row.get("color") if row.get("color") is not None else 0),
                _xaero_bool(xaero.get("disabled", False)),
                str(xaero.get("type", 0)),
                str(xaero.get("set", "gui.xaero_default")),
                _xaero_bool(xaero.get("rotate_on_tp", False)),
                str(xaero.get("tp_yaw", 0)),
                str(xaero.get("visibility_type", 0)),
                _xaero_bool(xaero.get("destination", False)),
            ]
            lines.append(":".join(fields))
        destination = destination_dir / "mw$default_1.txt"
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        installed_maps += 1
        installed_points += len(rows)
    return installed_maps, installed_points


def install_client(maps: list[MapServer]) -> None:
    versions = CLIENT_TEMPLATE / "versions"
    for version in ("1.21.11", "neoforge-21.11.44"):
        shutil.copytree(versions / version, MINECRAFT_ROOT / "versions" / version, dirs_exist_ok=True)
    shutil.copytree(CLIENT_TEMPLATE / "libraries", MINECRAFT_ROOT / "libraries", dirs_exist_ok=True)
    shutil.copytree(CLIENT_TEMPLATE / "assets", MINECRAFT_ROOT / "assets", dirs_exist_ok=True)
    mods = CLIENT_ROOT / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    for old in mods.glob("*.jar"):
        old.unlink()
    for source in sorted((CLIENT_TEMPLATE / "mods").glob("*.jar")):
        shutil.copy2(source, mods / source.name)
    config = CLIENT_ROOT / "config"
    config.mkdir(parents=True, exist_ok=True)
    minihud_config = CLIENT_TEMPLATE / "config/minihud.json"
    if minihud_config.is_file():
        shutil.copy2(minihud_config, config / minihud_config.name)
    for xaero_kind in ("minimap", "world-map"):
        merged_cache = CLIENT_ROOT / "xaero" / xaero_kind / "Multiplayer_127.0.0.1"
        if merged_cache.exists():
            shutil.rmtree(merged_cache)
    write_servers_dat(maps, CLIENT_ROOT / "servers.dat")
    waypoint_maps, waypoint_points = install_xaero_waypoints(maps)
    (CLIENT_ROOT / "options.txt").write_text(
        "autoJump:false\nrenderDistance:12\nsimulationDistance:6\nfov:0.0\nguiScale:3\n",
        encoding="utf-8",
    )
    profiles_path = MINECRAFT_ROOT / "launcher_profiles.json"
    backup = profiles_path.with_name(f"launcher_profiles.before-mcbots-{int(time.time())}.json")
    shutil.copy2(profiles_path, backup)
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    profiles.setdefault("profiles", {})[PROFILE_ID] = {
        "created": utc_now(),
        "gameDir": str(CLIENT_ROOT),
        "icon": "Lectern",
        "javaArgs": (
            "-Xms2G -Xmx8G -XX:+UseG1GC "
            "-Djava.net.preferIPv4Stack=true"
        ),
        "lastUsed": utc_now(),
        "lastVersionId": "neoforge-21.11.44",
        "name": f"MCBots Review 1.21.11 ({len(maps)} maps)",
        "type": "custom",
    }
    temporary = profiles_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, profiles_path)
    receipt = {
        "installed_at_utc": utc_now(),
        "profile_id": PROFILE_ID,
        "version": "neoforge-21.11.44",
        "game_dir": str(CLIENT_ROOT),
        "server_count": len(maps),
        "server_addresses": [f"{item.client_host}:{item.port}" for item in maps],
        "waypoint_maps": waypoint_maps,
        "waypoint_points": waypoint_points,
        "route_maps": sum(item.has_routes for item in maps),
        "mods": [path.name for path in sorted(mods.glob("*.jar"))],
        "launcher_profiles_backup": str(backup),
    }
    (CLIENT_ROOT / "install-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        f"installed Launcher profile, {len(receipt['mods'])} pinned client mods, "
        f"and {waypoint_points} waypoints across {waypoint_maps} maps"
    )


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def pid_owns_instance(pid: int, item: MapServer) -> bool:
    cwd = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    paths = [line[1:] for line in cwd.splitlines() if line.startswith("n")]
    return bool(paths) and Path(paths[0]).resolve() == item.instance.resolve()


def read_pid(item: MapServer) -> int | None:
    try:
        pid = int((item.instance / "server.pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid_running(pid) and pid_owns_instance(pid, item):
        return pid
    result = subprocess.run(
        ["lsof", "-a", "-t", f"-iTCP:{item.port}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
    )
    for value in result.stdout.splitlines():
        try:
            listener = int(value)
        except ValueError:
            continue
        if pid_owns_instance(listener, item):
            (item.instance / "server.pid").write_text(f"{listener}\n", encoding="utf-8")
            return listener
    return None


def tcp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def rcon_command(port: int, command: str) -> str:
    def packet(request_id: int, kind: int, payload: str) -> bytes:
        body = struct.pack("<ii", request_id, kind) + payload.encode() + b"\x00\x00"
        return struct.pack("<i", len(body)) + body

    def receive(sock: socket.socket) -> tuple[int, int, str]:
        size = struct.unpack("<i", sock.recv(4))[0]
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("RCON connection closed")
            data += chunk
        request_id, kind = struct.unpack("<ii", data[:8])
        return request_id, kind, data[8:-2].decode(errors="replace")

    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(packet(1, 3, RCON_PASSWORD))
        request_id, _, _ = receive(sock)
        if request_id == -1:
            raise RuntimeError("RCON authentication failed")
        sock.sendall(packet(2, 2, command))
        request_id, _, response = receive(sock)
        if request_id != 2:
            raise RuntimeError("unexpected RCON response")
        return response


def launch_one(item: MapServer) -> None:
    pid = read_pid(item)
    if pid and pid_running(pid):
        return
    logs = item.instance / "logs"
    logs.mkdir(exist_ok=True)
    console = (logs / "fleet-console.log").open("ab", buffering=0)
    command = [
        "/usr/bin/env",
        "java",
        "@user_jvm_args.txt",
        "@libraries/net/neoforged/neoforge/21.11.44/unix_args.txt",
        "nogui",
    ]
    process = subprocess.Popen(
        command,
        cwd=item.instance,
        stdin=subprocess.DEVNULL,
        stdout=console,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    console.close()
    (item.instance / "server.pid").write_text(f"{process.pid}\n", encoding="utf-8")


def configure_running(item: MapServer) -> None:
    x, y, z = item.spawn
    commands = [
        f"setworldspawn {x} {y} {z}",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "gamerule fall_damage true",
        "time set noon",
        "weather clear",
    ]
    for command in commands:
        rcon_command(item.rcon_port, command)


def start(maps: list[MapServer]) -> None:
    missing = [item.name for item in maps if not (item.instance / "world/level.dat").is_file()]
    if missing:
        raise RuntimeError("fleet is not prepared; run the prepare command first")
    failures: list[str] = []
    total = len(maps)
    for offset in range(0, len(maps), 4):
        batch = maps[offset : offset + 4]
        for batch_index, item in enumerate(batch):
            launch_one(item)
            progress = offset + batch_index + 1
            print(
                f"starting {progress:02d}/{total} {item.name} "
                f"on 127.0.0.1:{item.port}",
                flush=True,
            )
        deadline = time.monotonic() + 240
        pending = {item.slug: item for item in batch}
        while pending and time.monotonic() < deadline:
            for slug, item in list(pending.items()):
                pid = read_pid(item)
                if not pid or not pid_running(pid):
                    failures.append(f"{item.index:02d} {item.name}: process exited")
                    pending.pop(slug)
                elif tcp_ready(item.port):
                    try:
                        configure_running(item)
                    except (OSError, RuntimeError):
                        # The game socket starts accepting shortly before RCON.
                        continue
                    else:
                        progress = maps.index(item) + 1
                        print(f"ready    {progress:02d}/{total} {item.name}", flush=True)
                        pending.pop(slug)
            time.sleep(1)
        for item in pending.values():
            failures.append(f"{item.index:02d} {item.name}: startup timeout")
    if failures:
        raise RuntimeError("some servers failed:\n" + "\n".join(failures))


def stop(maps: list[MapServer]) -> None:
    total = len(maps)
    for progress, item in enumerate(maps, 1):
        pid = read_pid(item)
        if not pid or not pid_running(pid):
            continue
        try:
            rcon_command(item.rcon_port, "stop")
        except (OSError, RuntimeError):
            os.kill(pid, signal.SIGTERM)
        print(f"stopping {progress:02d}/{total} {item.name}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if all(not (pid := read_pid(item)) or not pid_running(pid) for item in maps):
            return
        time.sleep(1)


def status(maps: list[MapServer]) -> int:
    ready = 0
    for item in maps:
        pid = read_pid(item)
        state = "READY" if pid and pid_running(pid) and tcp_ready(item.port) else "DOWN"
        ready += state == "READY"
        print(
            f"{item.index:02d} {state:5s} 127.0.0.1:{item.port} "
            f"{item.name} [{availability_label(item)}]"
        )
    print(f"\n{ready}/{len(maps)} servers ready")
    return 0 if ready == len(maps) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "install-client", "start", "stop", "status", "all"))
    parser.add_argument(
        "--exclude-map",
        action="append",
        default=[],
        metavar="MAP_ID",
        help="omit a canonical release map ID; may be repeated",
    )
    parser.add_argument(
        "--all-maps",
        action="store_true",
        help="ignore the saved fleet selection and operate on all release maps",
    )
    args = parser.parse_args()
    discovered = discover_maps()
    known_ids = {item.map_id for item in discovered}
    if args.all_maps and args.exclude_map:
        raise RuntimeError("--all-maps cannot be combined with --exclude-map")
    excluded = set(args.exclude_map)
    fleet_manifest = FLEET_ROOT / "fleet.json"
    if not excluded and not args.all_maps and fleet_manifest.is_file():
        try:
            saved_fleet = json.loads(fleet_manifest.read_text(encoding="utf-8"))
            excluded = set(saved_fleet.get("excluded_map_ids", []))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid saved fleet manifest: {fleet_manifest}") from exc
    unknown = sorted(excluded - known_ids)
    if unknown:
        raise RuntimeError(f"unknown excluded map IDs: {unknown}")
    maps = [item for item in discovered if item.map_id not in excluded]
    if not maps:
        raise RuntimeError("all maps were excluded")
    if excluded:
        print(
            f"active fleet: {len(maps)}/{len(discovered)} maps; "
            f"excluded: {', '.join(sorted(excluded))}",
            flush=True,
        )
    if args.command in {"prepare", "all"}:
        prepare(maps, excluded_map_ids=excluded)
    if args.command in {"install-client", "all"}:
        install_client(maps)
    if args.command in {"start", "all"}:
        start(maps)
    if args.command == "stop":
        stop(maps)
    if args.command == "status":
        return status(maps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Strict schemas and digest helpers for navigation evaluation assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
NAVIGATION_ROOT = REPO_ROOT / "eval" / "navigation"
MAPS_ROOT = NAVIGATION_ROOT / "maps"
PROFILES_ROOT = NAVIGATION_ROOT / "profiles"
SETTINGS_ROOT = NAVIGATION_ROOT / "settings"
BENCHMARKS_ROOT = NAVIGATION_ROOT / "benchmarks"
TASK_CATALOG_PATH = NAVIGATION_ROOT / "tasks.json"
SNAPSHOT_CACHE_ROOT = REPO_ROOT / "eval" / "snapshots" / "_cache" / "navigation"
RUNTIME_TEMPLATE_ROOT = Path(
    os.environ.get(
        "MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT",
        REPO_ROOT / "eval" / "templates" / "_local" / "navigation",
    )
).expanduser().resolve()

TASK_EVAL_DEFAULTS: dict[str, Any] = {
    "time": "noon",
    "weather": "clear",
    "six_view_enabled": False,
    "player_scale": 1.0,
    "third_person": False,
    "hud_enabled": False,
    "navigation_hints_enabled": False,
    "coordinate_lock_enabled": True,
    "guideline": False,
    "resource_pack_and_shader": False,
}
TASK_TIME_TICKS = {"noon": 6000, "midnight": 18000}
TASK_WEATHER_VALUES = {"clear", "rain"}


class SchemaError(ValueError):
    """Raised when a navigation asset violates its tracked contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"missing navigation asset: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"expected a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaError(f"{field} must be a list")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{field} must be a non-empty string")
    return value.strip()


def _safe_component(value: Any, field: str) -> str:
    text = _text(value, field)
    if text != Path(text).name or text in {".", ".."}:
        raise SchemaError(f"{field} must be a safe path component")
    return text


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{field} must be a boolean")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{field} must be an integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise SchemaError(f"{field} must be finite")
    return result


def resolve_task_eval_setting(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate per-task overrides and return the complete effective setting."""

    raw = {} if value is None else _object(dict(value), "task.eval_setting")
    allowed = set(TASK_EVAL_DEFAULTS) | {"start_view"}
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise SchemaError(
            "task.eval_setting has unsupported fields: " + ", ".join(unexpected)
        )
    effective = {**TASK_EVAL_DEFAULTS, **{key: raw[key] for key in raw if key != "start_view"}}
    time_value = _text(effective["time"], "task.eval_setting.time")
    if time_value not in TASK_TIME_TICKS:
        raise SchemaError(
            "task.eval_setting.time must be one of: "
            + ", ".join(sorted(TASK_TIME_TICKS))
        )
    weather = _text(effective["weather"], "task.eval_setting.weather")
    if weather not in TASK_WEATHER_VALUES:
        raise SchemaError(
            "task.eval_setting.weather must be one of: "
            + ", ".join(sorted(TASK_WEATHER_VALUES))
        )
    for key in (
        "six_view_enabled",
        "third_person",
        "hud_enabled",
        "navigation_hints_enabled",
        "coordinate_lock_enabled",
        "guideline",
        "resource_pack_and_shader",
    ):
        effective[key] = _boolean(effective[key], f"task.eval_setting.{key}")
    player_scale = _number(effective["player_scale"], "task.eval_setting.player_scale")
    if not 0.0625 <= player_scale <= 16.0:
        raise SchemaError("task.eval_setting.player_scale must be between 0.0625 and 16")
    effective["time"] = time_value
    effective["weather"] = weather
    effective["player_scale"] = player_scale
    return effective


def _sha256(value: Any, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise SchemaError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _git_commit(value: Any, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
        raise SchemaError(f"{field} must be a lowercase 40-character Git commit")
    return text


def _schema_version(payload: Mapping[str, Any], field: str) -> None:
    if payload.get("schema_version") != 1:
        raise SchemaError(f"{field}.schema_version must be 1")


def _validate_annotation_source(
    payload: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    source = _object(payload.get("annotation_source"), f"{field}.annotation_source")
    if set(source) != {"repository", "git_commit", "path", "sha256"}:
        raise SchemaError(
            f"{field}.annotation_source must contain exactly repository, "
            "git_commit, path, and sha256"
        )
    _text(source.get("repository"), f"{field}.annotation_source.repository")
    _git_commit(source.get("git_commit"), f"{field}.annotation_source.git_commit")
    source_path = Path(_text(source.get("path"), f"{field}.annotation_source.path"))
    if source_path.is_absolute() or ".." in source_path.parts:
        raise SchemaError(f"{field}.annotation_source.path must be repository-relative")
    _sha256(source.get("sha256"), f"{field}.annotation_source.sha256")
    return source


def _repository_relative_path(value: Any, field: str) -> Path:
    path = Path(_text(value, field))
    if path == Path(".") or path.is_absolute() or ".." in path.parts:
        raise SchemaError(f"{field} must be repository-relative")
    return path


def _sha256_manifest_rows(path: Path, field: str) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise SchemaError(f"missing SHA-256 manifest: {path}") from exc
    if not lines:
        raise SchemaError(f"{field} must not be empty")
    output: dict[str, str] = {}
    for index, line in enumerate(lines):
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise SchemaError(f"{field}[{index}] must use '<sha256>  <path>'")
        digest = _sha256(parts[0], f"{field}[{index}].sha256")
        relative = _repository_relative_path(
            parts[1],
            f"{field}[{index}].path",
        ).as_posix()
        if relative in output:
            raise SchemaError(f"{field} contains duplicate path {relative!r}")
        output[relative] = digest
    if list(output) != sorted(output):
        raise SchemaError(f"{field} paths must be sorted")
    return output


def validate_position(value: Any, field: str) -> dict[str, float | int]:
    raw = _object(value, field)
    if set(raw) != {"x", "y", "z"}:
        raise SchemaError(f"{field} must contain exactly x, y, and z")
    output: dict[str, float | int] = {}
    for axis in ("x", "y", "z"):
        number = _number(raw[axis], f"{field}.{axis}")
        output[axis] = int(number) if number.is_integer() else number
    return output


def map_path(map_id: str) -> Path:
    normalized = _safe_component(map_id, "map_id")
    return MAPS_ROOT / normalized / "map.json"


def map_preparation_digest(map_payload: Mapping[str, Any]) -> str:
    """Bind a prepared snapshot to its source, transform, and expected output."""

    world = map_payload["world"]
    return digest_json(
        {
            "map_id": map_payload["map_id"],
            "expected_source_fingerprint": world["expected_source_fingerprint"],
            "expected_prepared_fingerprint": world["expected_prepared_fingerprint"],
            "preparation": world["preparation"],
        }
    )


def load_map(map_id: str) -> dict[str, Any]:
    path = map_path(map_id)
    payload = load_json(path)
    _schema_version(payload, f"map[{map_id}]")
    if payload.get("map_id") != map_id:
        raise SchemaError(f"{path}: map_id does not match directory")
    _text(payload.get("display_name"), f"{map_id}.display_name")

    if "out_of_bounds" in payload:
        out_of_bounds = _object(
            payload.get("out_of_bounds"), f"{map_id}.out_of_bounds"
        )
        if set(out_of_bounds) != {"at_or_below_y", "duration_sec"}:
            raise SchemaError(
                f"{map_id}.out_of_bounds has unexpected or missing fields"
            )
        _integer(
            out_of_bounds.get("at_or_below_y"),
            f"{map_id}.out_of_bounds.at_or_below_y",
        )
        duration_sec = _number(
            out_of_bounds.get("duration_sec"),
            f"{map_id}.out_of_bounds.duration_sec",
        )
        if duration_sec != 15:
            raise SchemaError(f"{map_id}.out_of_bounds.duration_sec must be 15")

    source = _object(payload.get("source"), f"{map_id}.source")
    _text(source.get("repository"), f"{map_id}.source.repository")
    if "release_id" in source:
        release_id = _integer(source.get("release_id"), f"{map_id}.source.release_id")
        if release_id <= 0:
            raise SchemaError(f"{map_id}.source.release_id must be positive")
        _text(source.get("release_url"), f"{map_id}.source.release_url")
    else:
        provenance = _object(source.get("provenance"), f"{map_id}.source.provenance")
        _text(provenance.get("kind"), f"{map_id}.source.provenance.kind")
        _text(provenance.get("git_commit"), f"{map_id}.source.provenance.git_commit")
        _repository_relative_path(
            provenance.get("git_path"), f"{map_id}.source.provenance.git_path"
        )
    _text(source.get("asset_name"), f"{map_id}.source.asset_name")
    archive_path = _repository_relative_path(
        source.get("archive_path", source.get("asset_name")),
        f"{map_id}.source.archive_path",
    )
    if archive_path.suffix.lower() != ".zip":
        raise SchemaError(f"{map_id}.source.archive_path must be a ZIP")
    archive_bytes = _integer(source.get("archive_bytes"), f"{map_id}.source.archive_bytes")
    if archive_bytes <= 0:
        raise SchemaError(f"{map_id}.source.archive_bytes must be positive")
    _sha256(source.get("archive_sha256"), f"{map_id}.source.archive_sha256")
    world_root = _text(source.get("world_root"), f"{map_id}.source.world_root")
    if world_root.startswith(("/", "\\")) or ".." in Path(world_root).parts:
        raise SchemaError(f"{map_id}.source.world_root is unsafe")

    world = _object(payload.get("world"), f"{map_id}.world")
    source_version = _object(
        world.get(
            "source_version",
            {
                "version_name": world.get("version_name"),
                "data_version": world.get("data_version"),
            },
        ),
        f"{map_id}.world.source_version",
    )
    _text(source_version.get("version_name"), f"{map_id}.world.source_version.version_name")
    if _integer(source_version.get("data_version"), f"{map_id}.world.source_version.data_version") <= 0:
        raise SchemaError(f"{map_id}.world.source_version.data_version must be positive")
    if "version_id" in source_version and _integer(
        source_version.get("version_id"),
        f"{map_id}.world.source_version.version_id",
    ) <= 0:
        raise SchemaError(f"{map_id}.world.source_version.version_id must be positive")
    if _text(world.get("minecraft_version"), f"{map_id}.world.minecraft_version") != "1.21.11":
        raise SchemaError(f"{map_id} must use Minecraft 1.21.11")
    if _text(world.get("version_name"), f"{map_id}.world.version_name") != "1.21.11":
        raise SchemaError(f"{map_id} must declare Version.Name 1.21.11")
    if _integer(world.get("data_version"), f"{map_id}.world.data_version") != 4671:
        raise SchemaError(f"{map_id} must use DataVersion 4671")
    _sha256(
        world.get("expected_source_fingerprint"),
        f"{map_id}.world.expected_source_fingerprint",
    )
    _sha256(
        world.get("expected_prepared_fingerprint"),
        f"{map_id}.world.expected_prepared_fingerprint",
    )
    preparation = _object(
        world.get("preparation"),
        f"{map_id}.world.preparation",
    )
    preparation_id = _text(
        preparation.get("id"), f"{map_id}.world.preparation.id"
    )
    if preparation_id not in {
        "heights-datapack-minecraft-1.21.11-v1",
        "height-datapack-compat-minecraft-1.21.11-v1",
        "identity-minecraft-1.21.11-v1",
        "minecraft-server-force-upgrade-1.21.11-v1",
    }:
        raise SchemaError(f"{map_id}: unexpected snapshot preparation transform")
    if preparation_id == "identity-minecraft-1.21.11-v1":
        if source_version["data_version"] != 4671 or source_version["version_name"] != "1.21.11":
            raise SchemaError(f"{map_id}: identity preparation requires a 1.21.11 source")
        _text(preparation.get("reason"), f"{map_id}.world.preparation.reason")
        license_payload = _object(payload.get("license"), f"{map_id}.license")
        for key in ("scope", "redistribution", "note"):
            _text(license_payload.get(key), f"{map_id}.license.{key}")
        return payload
    if preparation_id == "height-datapack-compat-minecraft-1.21.11-v1":
        remove_paths = _list(
            preparation.get("remove_paths"),
            f"{map_id}.world.preparation.remove_paths",
        )
        if not remove_paths:
            raise SchemaError(f"{map_id}: height compatibility requires remove_paths")
        for index, value in enumerate(remove_paths):
            _repository_relative_path(
                value, f"{map_id}.world.preparation.remove_paths[{index}]"
            )
        replacement = _object(
            preparation.get("replacement_datapack"),
            f"{map_id}.world.preparation.replacement_datapack",
        )
        if _repository_relative_path(
            replacement.get("source_path"),
            f"{map_id}.world.preparation.replacement_datapack.source_path",
        ) != Path("eval/navigation/compatibility/heights-1.21.11"):
            raise SchemaError(f"{map_id}: unexpected replacement datapack source")
        if _repository_relative_path(
            replacement.get("install_path"),
            f"{map_id}.world.preparation.replacement_datapack.install_path",
        ) != Path("datapacks/heights"):
            raise SchemaError(f"{map_id}: unexpected replacement datapack install path")
        manifest_relative = _repository_relative_path(
            replacement.get("manifest_path"),
            f"{map_id}.world.preparation.replacement_datapack.manifest_path",
        )
        manifest_path = REPO_ROOT / manifest_relative
        actual_manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        expected_manifest_digest = _sha256(
            replacement.get("manifest_sha256"),
            f"{map_id}.world.preparation.replacement_datapack.manifest_sha256",
        )
        if actual_manifest_digest != expected_manifest_digest:
            raise SchemaError(f"{map_id}: replacement datapack manifest is stale")
        if replacement.get("data_pack_format") != {"major": 94, "minor": 1}:
            raise SchemaError(f"{map_id}: expected Minecraft 1.21.11 data pack format 94.1")
        _text(preparation.get("reason"), f"{map_id}.world.preparation.reason")
        license_payload = _object(payload.get("license"), f"{map_id}.license")
        for key in ("scope", "redistribution", "note"):
            _text(license_payload.get(key), f"{map_id}.license.{key}")
        return payload
    if preparation_id == "minecraft-server-force-upgrade-1.21.11-v1":
        if source_version["data_version"] == 4671:
            raise SchemaError(f"{map_id}: an already-1.21.11 source must not be force-upgraded")
        command = _list(preparation.get("server_arguments"), f"{map_id}.world.preparation.server_arguments")
        if command != ["--forceUpgrade", "--eraseCache", "nogui"]:
            raise SchemaError(f"{map_id}: unexpected force-upgrade arguments")
        _text(preparation.get("runtime_profile"), f"{map_id}.world.preparation.runtime_profile")
        _text(preparation.get("reason"), f"{map_id}.world.preparation.reason")
        license_payload = _object(payload.get("license"), f"{map_id}.license")
        for key in ("scope", "redistribution", "note"):
            _text(license_payload.get(key), f"{map_id}.license.{key}")
        return payload
    source_datapack = _object(
        preparation.get("source_datapack"),
        f"{map_id}.world.preparation.source_datapack",
    )
    source_datapack_path = _repository_relative_path(
        source_datapack.get("relative_path"),
        f"{map_id}.world.preparation.source_datapack.relative_path",
    )
    if source_datapack_path != Path("datapacks/heights"):
        raise SchemaError(f"{map_id}: source datapack must be datapacks/heights")
    source_files = _object(
        source_datapack.get("files"),
        f"{map_id}.world.preparation.source_datapack.files",
    )
    expected_source_pack_paths = {
        "data/minecraft/dimension/overworld.json",
        "data/minecraft/dimension_type/overworld.json",
        "pack.mcmeta",
    }
    if set(source_files) != expected_source_pack_paths:
        raise SchemaError(
            f"{map_id}: source datapack lock must contain its exact three legacy files"
        )
    for relative, digest in source_files.items():
        _repository_relative_path(
            relative,
            f"{map_id}.world.preparation.source_datapack.files.path",
        )
        _sha256(
            digest,
            f"{map_id}.world.preparation.source_datapack.files[{relative!r}]",
        )

    replacement = _object(
        preparation.get("replacement_datapack"),
        f"{map_id}.world.preparation.replacement_datapack",
    )
    replacement_source_relative = _repository_relative_path(
        replacement.get("source_path"),
        f"{map_id}.world.preparation.replacement_datapack.source_path",
    )
    if replacement_source_relative != Path(
        "eval/navigation/compatibility/heights-1.21.11"
    ):
        raise SchemaError(f"{map_id}: unexpected replacement datapack source")
    replacement_install_path = _repository_relative_path(
        replacement.get("install_path"),
        f"{map_id}.world.preparation.replacement_datapack.install_path",
    )
    if replacement_install_path != source_datapack_path:
        raise SchemaError(
            f"{map_id}: replacement datapack must preserve the existing pack identity"
        )
    manifest_relative = _repository_relative_path(
        replacement.get("manifest_path"),
        f"{map_id}.world.preparation.replacement_datapack.manifest_path",
    )
    if manifest_relative != Path(
        "eval/navigation/compatibility/heights-1.21.11.files.sha256"
    ):
        raise SchemaError(f"{map_id}: unexpected replacement datapack manifest")
    manifest_path = REPO_ROOT / manifest_relative
    try:
        actual_manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except FileNotFoundError as exc:
        raise SchemaError(
            f"{map_id}: missing replacement datapack manifest {manifest_path}"
        ) from exc
    expected_manifest_digest = _sha256(
        replacement.get("manifest_sha256"),
        f"{map_id}.world.preparation.replacement_datapack.manifest_sha256",
    )
    if actual_manifest_digest != expected_manifest_digest:
        raise SchemaError(f"{map_id}: replacement datapack manifest is stale")
    manifest_rows = _sha256_manifest_rows(
        manifest_path,
        f"{map_id}.world.preparation.replacement_datapack.manifest",
    )
    replacement_source = REPO_ROOT / replacement_source_relative
    if not replacement_source.is_dir() or replacement_source.is_symlink():
        raise SchemaError(
            f"{map_id}: missing or unsafe replacement datapack {replacement_source}"
        )
    replacement_entries = list(replacement_source.rglob("*"))
    if any(path.is_symlink() for path in replacement_entries):
        raise SchemaError(f"{map_id}: replacement datapack contains a symlink")
    replacement_paths = sorted(
        path.relative_to(replacement_source).as_posix()
        for path in replacement_entries
        if path.is_file()
    )
    if set(replacement_paths) != set(manifest_rows):
        raise SchemaError(
            f"{map_id}: replacement datapack files do not match its manifest"
        )
    for relative, expected_digest in manifest_rows.items():
        path = replacement_source / relative
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise SchemaError(
                f"{map_id}: replacement datapack file is stale: {relative}"
            )

    data_pack_format = _object(
        replacement.get("data_pack_format"),
        f"{map_id}.world.preparation.replacement_datapack.data_pack_format",
    )
    if set(data_pack_format) != {"major", "minor"}:
        raise SchemaError(f"{map_id}: data_pack_format must contain major and minor")
    pack_format = (
        _integer(
            data_pack_format.get("major"),
            f"{map_id}.world.preparation.data_pack_format.major",
        ),
        _integer(
            data_pack_format.get("minor"),
            f"{map_id}.world.preparation.data_pack_format.minor",
        ),
    )
    if pack_format != (94, 1):
        raise SchemaError(f"{map_id}: expected Minecraft 1.21.11 data pack format 94.1")
    pack_meta = load_json(replacement_source / "pack.mcmeta")
    pack = _object(pack_meta.get("pack"), f"{map_id}.replacement_datapack.pack")
    if "pack_format" in pack:
        raise SchemaError(f"{map_id}: legacy pack_format is forbidden for format 94.1")
    if pack.get("min_format") != [94, 1] or pack.get("max_format") != [94, 1]:
        raise SchemaError(f"{map_id}: replacement pack must target exactly format 94.1")
    _text(pack.get("description"), f"{map_id}.replacement_datapack.description")
    dimension_type = load_json(
        replacement_source
        / "data"
        / "minecraft"
        / "dimension_type"
        / "overworld.json"
    )
    if (
        dimension_type.get("min_y") != -64
        or dimension_type.get("height") != 512
        or dimension_type.get("logical_height") != 512
    ):
        raise SchemaError(
            f"{map_id}: replacement overworld must preserve -64/512/512 heights"
        )
    _text(
        preparation.get("reason"),
        f"{map_id}.world.preparation.reason",
    )
    license_payload = _object(payload.get("license"), f"{map_id}.license")
    for key in ("scope", "redistribution", "note"):
        _text(license_payload.get(key), f"{map_id}.license.{key}")
    return payload


def load_waypoints(map_id: str) -> dict[str, dict[str, Any]]:
    path = map_path(map_id).parent / "waypoints.json"
    payload = load_json(path)
    _schema_version(payload, f"waypoints[{map_id}]")
    if payload.get("map_id") != map_id:
        raise SchemaError(f"{path}: map_id mismatch")
    _validate_annotation_source(payload, f"waypoints[{map_id}]")
    rows = _list(payload.get("waypoints"), f"{map_id}.waypoints")
    output: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(rows):
        row = _object(value, f"{map_id}.waypoints[{index}]")
        waypoint_id = _text(row.get("id"), f"{map_id}.waypoints[{index}].id")
        if waypoint_id in output:
            raise SchemaError(f"{map_id}: duplicate waypoint id {waypoint_id!r}")
        _text(row.get("name"), f"{map_id}.waypoints[{index}].name")
        validate_position(row.get("position"), f"{map_id}.waypoints[{index}].position")
        route_ids = _list(row.get("route_ids", []), f"{map_id}.waypoints[{index}].route_ids")
        for route_index, route_id in enumerate(route_ids):
            _text(route_id, f"{map_id}.waypoints[{index}].route_ids[{route_index}]")
        output[waypoint_id] = row
    declared = _integer(payload.get("waypoint_count"), f"{map_id}.waypoint_count")
    if declared != len(output):
        raise SchemaError(f"{map_id}: waypoint_count={declared}, actual={len(output)}")
    return output


def load_routes(map_id: str) -> list[dict[str, Any]]:
    """Load a map-local candidate route package and validate waypoint links."""

    path = map_path(map_id).parent / "routes.json"
    payload = load_json(path)
    _schema_version(payload, f"routes[{map_id}]")
    if payload.get("map_id") != map_id:
        raise SchemaError(f"{path}: map_id mismatch")
    route_source = _validate_annotation_source(payload, f"routes[{map_id}]")
    waypoint_payload = load_json(map_path(map_id).parent / "waypoints.json")
    waypoint_source = _validate_annotation_source(
        waypoint_payload,
        f"waypoints[{map_id}]",
    )
    if route_source["repository"] != waypoint_source["repository"]:
        raise SchemaError(
            f"{map_id}: route and waypoint annotation repositories differ"
        )

    waypoints = load_waypoints(map_id)
    rows = _list(payload.get("routes"), f"{map_id}.routes")
    declared = _integer(payload.get("route_count"), f"{map_id}.route_count")
    if declared != len(rows):
        raise SchemaError(f"{map_id}: route_count={declared}, actual={len(rows)}")
    _text(payload.get("quality_status"), f"{map_id}.quality_status")
    _text(payload.get("upload_recommendation"), f"{map_id}.upload_recommendation")

    output: list[dict[str, Any]] = []
    route_ids: set[str] = set()
    for index, value in enumerate(rows):
        route = _object(value, f"{map_id}.routes[{index}]")
        route_id = _text(route.get("id"), f"{map_id}.routes[{index}].id")
        if route_id in route_ids:
            raise SchemaError(f"{map_id}: duplicate route id {route_id!r}")
        route_ids.add(route_id)
        _text(route.get("title"), f"{map_id}.{route_id}.title")
        status = route.get("status", route.get("validation_status"))
        _text(status, f"{map_id}.{route_id}.status")
        points = _list(route.get("points"), f"{map_id}.{route_id}.points")
        if len(points) < 2:
            raise SchemaError(
                f"{map_id}.{route_id}.points must contain at least two points"
            )
        point_ids: list[str] = []
        for point_index, raw_point in enumerate(points):
            point = _object(raw_point, f"{map_id}.{route_id}.points[{point_index}]")
            if set(point) != {"waypoint_id", "role"}:
                raise SchemaError(
                    f"{map_id}.{route_id}.points[{point_index}] must contain exactly "
                    "waypoint_id and role"
                )
            waypoint_id = _text(
                point.get("waypoint_id"),
                f"{map_id}.{route_id}.points[{point_index}].waypoint_id",
            )
            if waypoint_id not in waypoints:
                raise SchemaError(
                    f"{map_id}.{route_id} references unknown waypoint {waypoint_id!r}"
                )
            role = _text(
                point.get("role"),
                f"{map_id}.{route_id}.points[{point_index}].role",
            )
            expected_role = (
                "start"
                if point_index == 0
                else "end"
                if point_index == len(points) - 1
                else "via"
            )
            if role != expected_role:
                raise SchemaError(
                    f"{map_id}.{route_id}.points[{point_index}].role must be {expected_role!r}"
                )
            point_ids.append(waypoint_id)

        segments = _list(route.get("segments", []), f"{map_id}.{route_id}.segments")
        if segments and len(segments) != len(point_ids) - 1:
            raise SchemaError(
                f"{map_id}.{route_id}: segment count does not match consecutive route points"
            )
        for segment_index, raw_segment in enumerate(segments):
            segment = _object(
                raw_segment,
                f"{map_id}.{route_id}.segments[{segment_index}]",
            )
            segment_from = segment.get("from", segment.get("from_point_id"))
            segment_to = segment.get("to", segment.get("to_point_id"))
            if (
                segment_from != point_ids[segment_index]
                or segment_to != point_ids[segment_index + 1]
            ):
                raise SchemaError(
                    f"{map_id}.{route_id}.segments[{segment_index}] does not match route points"
                )
        output.append(route)

    for waypoint_id, waypoint in waypoints.items():
        for route_id in waypoint.get("route_ids", []):
            if route_id not in route_ids:
                raise SchemaError(
                    f"{map_id}.{waypoint_id} references unknown route {route_id!r}"
                )
    return output


def _validate_endpoint(
    value: Any,
    *,
    field: str,
    waypoints: Mapping[str, Mapping[str, Any]],
    allow_view: bool,
) -> dict[str, Any]:
    endpoint = _object(value, field)
    waypoint_id = _text(endpoint.get("waypoint_id"), f"{field}.waypoint_id")
    if waypoint_id not in waypoints:
        raise SchemaError(f"{field} references unknown waypoint {waypoint_id!r}")
    if "position_override" in endpoint:
        validate_position(endpoint["position_override"], f"{field}.position_override")
        _text(endpoint.get("override_reason"), f"{field}.override_reason")
        _text(endpoint.get("override_selection"), f"{field}.override_selection")
    elif "override_reason" in endpoint or "override_selection" in endpoint:
        raise SchemaError(
            f"{field}.override_reason and override_selection require position_override"
        )
    if allow_view:
        _number(endpoint.get("yaw", 0.0), f"{field}.yaw")
        pitch = _number(endpoint.get("pitch", 0.0), f"{field}.pitch")
        if pitch < -90 or pitch > 90:
            raise SchemaError(f"{field}.pitch must be between -90 and 90")
    return endpoint


def load_tasks(map_id: str) -> list[dict[str, Any]]:
    """Load one map's tasks from the shared schema-v2 task catalog.

    The tracked catalog keeps the author-facing compact representation. This
    loader validates it against the map-local waypoint and route packages, then
    exposes the legacy-shaped internal task object consumed by the runner.
    """

    payload = load_json(TASK_CATALOG_PATH)
    if payload.get("schema_version") != 2:
        raise SchemaError("navigation task catalog schema_version must be 2")
    catalog_rows = _list(payload.get("tasks"), "navigation.tasks")
    waypoints = load_waypoints(map_id)
    routes = {route["id"]: route for route in load_routes(map_id)}
    output: list[dict[str, Any]] = []
    ids: set[str] = set()
    for catalog_index, value in enumerate(catalog_rows):
        compact = _object(value, f"navigation.tasks[{catalog_index}]")
        compact_map_id = _text(
            compact.get("map_id"), f"navigation.tasks[{catalog_index}].map_id"
        )
        if compact_map_id != map_id:
            continue
        if set(compact) != {
            "task_id",
            "prompt",
            "map_id",
            "waypoints",
            "eval_setting",
            "metadata",
        }:
            raise SchemaError(
                f"navigation.tasks[{catalog_index}] has unexpected or missing fields"
            )
        task_id = _safe_component(
            compact.get("task_id"), f"navigation.tasks[{catalog_index}].task_id"
        )
        if task_id in ids:
            raise SchemaError(f"{map_id}: duplicate task id {task_id!r}")
        ids.add(task_id)
        prompt = _text(compact.get("prompt"), f"{map_id}.{task_id}.prompt")
        if prompt == "default":
            raise SchemaError(f"{map_id}.{task_id}.prompt is still the placeholder")
        eval_setting = _object(
            compact.get("eval_setting"), f"{map_id}.{task_id}.eval_setting"
        )
        resolve_task_eval_setting(eval_setting)
        metadata = _object(compact.get("metadata"), f"{map_id}.{task_id}.metadata")
        source_route_id = _text(
            metadata.get("source_route_id"),
            f"{map_id}.{task_id}.metadata.source_route_id",
        )
        route = routes.get(source_route_id)
        if route is None:
            raise SchemaError(
                f"{map_id}.{task_id} references unknown route {source_route_id!r}"
            )
        compact_waypoints = _list(
            compact.get("waypoints"), f"{map_id}.{task_id}.waypoints"
        )
        if len(compact_waypoints) < 2:
            raise SchemaError(f"{map_id}.{task_id}.waypoints must contain at least two points")
        waypoint_ids: list[str] = []
        for waypoint_index, raw_waypoint in enumerate(compact_waypoints):
            waypoint = _object(
                raw_waypoint, f"{map_id}.{task_id}.waypoints[{waypoint_index}]"
            )
            if set(waypoint) != {"id", "name", "caption"}:
                raise SchemaError(
                    f"{map_id}.{task_id}.waypoints[{waypoint_index}] must contain "
                    "exactly id, name, and caption"
                )
            waypoint_id = _text(
                waypoint.get("id"),
                f"{map_id}.{task_id}.waypoints[{waypoint_index}].id",
            )
            if waypoint_id not in waypoints:
                raise SchemaError(
                    f"{map_id}.{task_id} references unknown waypoint {waypoint_id!r}"
                )
            name = _text(
                waypoint.get("name"),
                f"{map_id}.{task_id}.waypoints[{waypoint_index}].name",
            )
            if name != waypoints[waypoint_id]["name"]:
                raise SchemaError(
                    f"{map_id}.{task_id}: embedded name for {waypoint_id!r} is stale"
                )
            _text(
                waypoint.get("caption"),
                f"{map_id}.{task_id}.waypoints[{waypoint_index}].caption",
            )
            waypoint_ids.append(waypoint_id)
        route_waypoint_ids = [
            str(point["waypoint_id"]) for point in route["points"]
        ]
        if waypoint_ids != route_waypoint_ids:
            raise SchemaError(
                f"{map_id}.{task_id}: waypoint order differs from {source_route_id}"
            )
        required_ids = waypoint_ids[1:-1]
        if len(required_ids) != len(set(required_ids)):
            raise SchemaError(f"{map_id}.{task_id}: duplicate required waypoint")

        start_view = _object(
            eval_setting.get("start_view", {}),
            f"{map_id}.{task_id}.eval_setting.start_view",
        )
        yaw = _number(start_view.get("yaw", 0.0), f"{map_id}.{task_id}.start.yaw")
        pitch = _number(
            start_view.get("pitch", 0.0), f"{map_id}.{task_id}.start.pitch"
        )
        if pitch < -90 or pitch > 90:
            raise SchemaError(f"{map_id}.{task_id}.start.pitch must be between -90 and 90")
        status = route.get("status", route.get("validation_status"))
        task = {
            "id": task_id,
            "name": str(metadata.get("title_local") or task_id),
            "prompt": prompt,
            "category": ["navigation"],
            "start": {"waypoint_id": waypoint_ids[0], "yaw": yaw, "pitch": pitch},
            "target": {"waypoint_id": waypoint_ids[-1]},
            "required_waypoints": [
                {"waypoint_id": waypoint_id} for waypoint_id in required_ids
            ],
            "source": {
                "route_id": source_route_id,
                "kind": "route",
                "status": _text(status, f"{map_id}.{source_route_id}.status"),
                "start_id": waypoint_ids[0],
                "target_id": waypoint_ids[-1],
                "intermediate_ids": required_ids,
            },
            "eval_setting": dict(eval_setting),
            "metadata": dict(metadata),
        }
        output.append(task)

    all_task_ids = [
        _text(row.get("task_id"), f"navigation.tasks[{index}].task_id")
        for index, row in enumerate(catalog_rows)
        if isinstance(row, dict)
    ]
    if len(all_task_ids) != len(set(all_task_ids)):
        raise SchemaError("navigation task catalog contains duplicate task IDs")
    return output


def endpoint_position(
    endpoint: Mapping[str, Any],
    waypoints: Mapping[str, Mapping[str, Any]],
) -> dict[str, float | int]:
    raw = endpoint.get("position_override")
    if raw is None:
        raw = waypoints[str(endpoint["waypoint_id"])]["position"]
    return validate_position(raw, "endpoint.position")


def resolved_task(map_id: str, task: Mapping[str, Any]) -> dict[str, Any]:
    waypoints = load_waypoints(map_id)
    start = dict(task["start"])
    target = dict(task["target"])
    start["position"] = endpoint_position(start, waypoints)
    target["position"] = endpoint_position(target, waypoints)
    target["name"] = str(waypoints[str(target["waypoint_id"])]["name"])
    required_waypoints: list[dict[str, Any]] = []
    for waypoint_value in task.get("required_waypoints", []):
        waypoint = dict(waypoint_value)
        waypoint["position"] = endpoint_position(waypoint, waypoints)
        waypoint["name"] = str(waypoints[str(waypoint["waypoint_id"])]["name"])
        required_waypoints.append(waypoint)
    return {
        **dict(task),
        "start": start,
        "target": target,
        "required_waypoints": required_waypoints,
    }


def task_digest(map_id: str, task: Mapping[str, Any]) -> str:
    return digest_json(resolved_task(map_id, task))


def load_references(map_id: str) -> dict[str, dict[str, Any]]:
    path = map_path(map_id).parent / "references.json"
    payload = load_json(path)
    _schema_version(payload, f"references[{map_id}]")
    if payload.get("map_id") != map_id:
        raise SchemaError(f"{path}: map_id mismatch")
    _text(payload.get("planner_version"), f"{map_id}.planner_version")
    fingerprint = _sha256(
        payload.get("map_fingerprint"),
        f"{map_id}.references.map_fingerprint",
    )
    expected_fingerprint = load_map(map_id)["world"]["expected_prepared_fingerprint"]
    if fingerprint != expected_fingerprint:
        raise SchemaError(f"{map_id}: references bind a stale map fingerprint")
    rows = _list(payload.get("references"), f"{map_id}.references")
    output: dict[str, dict[str, Any]] = {}
    tasks = {task["id"]: task for task in load_tasks(map_id)}
    task_ids = set(tasks)
    for index, value in enumerate(rows):
        row = _object(value, f"{map_id}.references[{index}]")
        task_id = _text(row.get("task_id"), f"{map_id}.references[{index}].task_id")
        if task_id not in task_ids:
            raise SchemaError(f"{map_id}: reference for unknown task {task_id}")
        if task_id in output:
            raise SchemaError(f"{map_id}: duplicate reference for {task_id}")
        status = _text(row.get("status"), f"{map_id}.{task_id}.reference.status")
        if status not in {"pending", "reachable", "unreachable", "planner_limit"}:
            raise SchemaError(f"{map_id}.{task_id}: unsupported reference status {status!r}")
        if status != "pending":
            expected_task_digest = task_digest(map_id, tasks[task_id])
            actual_task_digest = _sha256(
                row.get("task_digest"),
                f"{map_id}.{task_id}.task_digest",
            )
            if actual_task_digest != expected_task_digest:
                raise SchemaError(f"{map_id}.{task_id}: reference task digest is stale")
            expanded_nodes = _integer(
                row.get("expanded_nodes"),
                f"{map_id}.{task_id}.expanded_nodes",
            )
            if expanded_nodes < 0:
                raise SchemaError(
                    f"{map_id}.{task_id}.expanded_nodes must be non-negative"
                )
        if status == "reachable":
            length = _number(row.get("length_blocks"), f"{map_id}.{task_id}.length_blocks")
            if length <= 0:
                raise SchemaError(f"{map_id}.{task_id}.length_blocks must be positive")
            _sha256(row.get("route_digest"), f"{map_id}.{task_id}.route_digest")
        elif status in {"unreachable", "planner_limit"}:
            _text(row.get("reason"), f"{map_id}.{task_id}.reason")
        output[task_id] = row
    if set(output) != task_ids:
        missing = sorted(task_ids - set(output))
        raise SchemaError(f"{map_id}: missing reference rows: {', '.join(missing)}")
    return output


def load_profile(profile_id: str) -> dict[str, Any]:
    normalized = _safe_component(profile_id, "profile_id")
    path = PROFILES_ROOT / f"{normalized}.json"
    payload = load_json(path)
    _schema_version(payload, f"profile[{profile_id}]")
    if payload.get("profile_id") != profile_id:
        raise SchemaError(f"{path}: profile_id mismatch")
    minecraft = _object(payload.get("minecraft"), f"{profile_id}.minecraft")
    if minecraft.get("version") != "1.21.11" or minecraft.get("data_version") != 4671:
        raise SchemaError(f"{profile_id}: expected Minecraft 1.21.11 / DataVersion 4671")
    neoforge = _object(payload.get("neoforge"), f"{profile_id}.neoforge")
    if _text(neoforge.get("version"), f"{profile_id}.neoforge.version") != "21.11.44":
        raise SchemaError(f"{profile_id}: expected NeoForge 21.11.44")
    if (
        _text(neoforge.get("installer_url"), f"{profile_id}.neoforge.installer_url")
        != "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
        "21.11.44/neoforge-21.11.44-installer.jar"
    ):
        raise SchemaError(f"{profile_id}: unexpected NeoForge installer URL")
    _sha256(
        neoforge.get("installer_sha256"),
        f"{profile_id}.neoforge.installer_sha256",
    )
    portablemc = _object(payload.get("portablemc"), f"{profile_id}.portablemc")
    if (
        _text(
            portablemc.get("linux_arm64_lwjgl_version"),
            f"{profile_id}.portablemc.linux_arm64_lwjgl_version",
        )
        != "3.3.3"
    ):
        raise SchemaError(f"{profile_id}: expected PortableMC ARM64 LWJGL 3.3.3")
    agentbridge = _object(payload.get("agentbridge"), f"{profile_id}.agentbridge")
    if agentbridge.get("mod_id") != "agentbridge":
        raise SchemaError(f"{profile_id}.agentbridge.mod_id must be agentbridge")
    artifact_name = _text(
        agentbridge.get("artifact_name"),
        f"{profile_id}.agentbridge.artifact_name",
    )
    if artifact_name != "agentbridge-1.21.11.jar":
        raise SchemaError(f"{profile_id}: unexpected AgentBridge artifact name")
    _sha256(agentbridge.get("sha256"), f"{profile_id}.agentbridge.sha256")
    mods = _object(payload.get("client_mods"), f"{profile_id}.client_mods")
    required = _list(mods.get("required"), f"{profile_id}.client_mods.required")
    if not required:
        raise SchemaError(f"{profile_id}: required client mods must not be empty")
    names: set[str] = set()
    mod_ids: set[str] = set()
    agentbridge_mod: dict[str, Any] | None = None
    for index, value in enumerate(required):
        row = _object(value, f"{profile_id}.client_mods.required[{index}]")
        name = _text(row.get("name"), f"{profile_id}.client_mods.required[{index}].name")
        if name in names:
            raise SchemaError(f"{profile_id}: duplicate required mod {name}")
        names.add(name)
        mod_id = _text(
            row.get("mod_id"),
            f"{profile_id}.client_mods.required[{index}].mod_id",
        )
        if mod_id in mod_ids:
            raise SchemaError(f"{profile_id}: duplicate required mod ID {mod_id}")
        mod_ids.add(mod_id)
        if mod_id == "agentbridge":
            agentbridge_mod = row
        _text(
            row.get("artifact_name"),
            f"{profile_id}.client_mods.required[{index}].artifact_name",
        )
        _text(row.get("version"), f"{profile_id}.client_mods.required[{index}].version")
        if row.get("side") != "client":
            raise SchemaError(
                f"{profile_id}.client_mods.required[{index}].side must be client"
            )
        _sha256(
            row.get("sha256"),
            f"{profile_id}.client_mods.required[{index}].sha256",
        )
    if agentbridge_mod is None:
        raise SchemaError(f"{profile_id}: AgentBridge is not a required client mod")
    if mod_ids != {
        "agentbridge",
        "bocchud",
        "lambdynlights",
        "mafglib",
        "xaerominimap",
        "xaeroworldmap",
    }:
        raise SchemaError(
            f"{profile_id}: required mod IDs must be exactly AgentBridge, BoccHUD, "
            "MaFgLib, LambDynamicLights, and both Xaero mods"
        )
    if (
        agentbridge_mod.get("artifact_name") != agentbridge.get("artifact_name")
        or agentbridge_mod.get("sha256") != agentbridge.get("sha256")
    ):
        raise SchemaError(f"{profile_id}: AgentBridge profile/mod lock mismatch")
    optional_profiles = _object(
        mods.get("optional_profiles"),
        f"{profile_id}.client_mods.optional_profiles",
    )
    if set(optional_profiles) != {"guideline"}:
        raise SchemaError(
            f"{profile_id}: optional client mod profiles must be exactly guideline"
        )
    guideline_rows = _list(
        optional_profiles.get("guideline"),
        f"{profile_id}.client_mods.optional_profiles.guideline",
    )
    optional_ids: set[str] = set()
    optional_artifacts: set[str] = set()
    for index, value in enumerate(guideline_rows):
        label = f"{profile_id}.client_mods.optional_profiles.guideline[{index}]"
        row = _object(value, label)
        _text(row.get("name"), f"{label}.name")
        mod_id = _text(row.get("mod_id"), f"{label}.mod_id")
        artifact = _text(row.get("artifact_name"), f"{label}.artifact_name")
        if mod_id in optional_ids or artifact in optional_artifacts:
            raise SchemaError(f"{profile_id}: duplicate optional client mod")
        optional_ids.add(mod_id)
        optional_artifacts.add(artifact)
        _text(row.get("version"), f"{label}.version")
        if row.get("side") != "client":
            raise SchemaError(f"{label}.side must be client")
        _sha256(row.get("sha256"), f"{label}.sha256")
        if "source_url" not in row and "source" not in row:
            raise SchemaError(f"{label} must declare source or source_url")
    if optional_ids != {"ground_navigation", "baritoe"}:
        raise SchemaError(
            f"{profile_id}: guideline must contain Ground Navigation and Baritone"
        )
    forbidden = {
        _text(value, f"{profile_id}.client_mods.forbidden[{index}]")
        for index, value in enumerate(
            _list(mods.get("forbidden"), f"{profile_id}.client_mods.forbidden")
        )
    }
    if not optional_ids.issubset(forbidden):
        raise SchemaError(
            f"{profile_id}: optional guideline mods must be forbidden by default"
        )
    if mods.get("reject_unknown_active") is not True:
        raise SchemaError(f"{profile_id}: reject_unknown_active must be true")
    configs = _list(payload.get("client_configs"), f"{profile_id}.client_configs")
    if len(configs) != 1:
        raise SchemaError(f"{profile_id}: exactly one client config is required")
    config = _object(configs[0], f"{profile_id}.client_configs[0]")
    if config.get("source_path") != "config/minihud.json":
        raise SchemaError(f"{profile_id}: unexpected client config source")
    if config.get("artifact_name") != "minihud.json":
        raise SchemaError(f"{profile_id}: unexpected client config artifact")
    _sha256(config.get("sha256"), f"{profile_id}.client_configs[0].sha256")
    return payload


def load_setting(setting_id: str) -> dict[str, Any]:
    normalized = _safe_component(setting_id, "setting_id")
    path = SETTINGS_ROOT / f"{normalized}.json"
    payload = load_json(path)
    _schema_version(payload, f"setting[{setting_id}]")
    if payload.get("setting_id") != setting_id:
        raise SchemaError(f"{path}: setting_id mismatch")
    profile_id = _text(payload.get("profile_id"), f"{setting_id}.profile_id")
    load_profile(profile_id)
    arrival = _object(payload.get("arrival"), f"{setting_id}.arrival")
    if set(arrival) != {"radius_3d", "radius_y", "sample_interval_sec"}:
        raise SchemaError(f"{setting_id}.arrival has unexpected or missing fields")
    expected_arrival = {
        "radius_3d": 3.5,
        "radius_y": 1.5,
        "sample_interval_sec": 1.0,
    }
    actual_arrival = {
        "radius_3d": _number(arrival.get("radius_3d"), f"{setting_id}.arrival.radius_3d"),
        "radius_y": _number(arrival.get("radius_y"), f"{setting_id}.arrival.radius_y"),
        "sample_interval_sec": _number(
            arrival.get("sample_interval_sec"),
            f"{setting_id}.arrival.sample_interval_sec",
        ),
    }
    if actual_arrival != expected_arrival:
        raise SchemaError(f"{setting_id}: arrival policy must remain {expected_arrival}")

    completion = _object(payload.get("completion"), f"{setting_id}.completion")
    if set(completion) != {"action", "claim_attempt_limit"}:
        raise SchemaError(f"{setting_id}.completion has unexpected or missing fields")
    if completion.get("action") != "claim_done":
        raise SchemaError(f"{setting_id}.completion.action must be claim_done")
    attempts = _integer(
        completion.get("claim_attempt_limit"),
        f"{setting_id}.claim_attempt_limit",
    )
    if attempts <= 0:
        raise SchemaError(f"{setting_id}.claim_attempt_limit must be positive")
    task_eval_defaults = _object(
        payload.get("task_eval_defaults"), f"{setting_id}.task_eval_defaults"
    )
    if task_eval_defaults != TASK_EVAL_DEFAULTS:
        raise SchemaError(
            f"{setting_id}.task_eval_defaults must remain {TASK_EVAL_DEFAULTS}"
        )
    resolve_task_eval_setting(task_eval_defaults)
    runtime = _object(payload.get("runtime"), f"{setting_id}.runtime")
    expected_runtime = {
        "gamemode": "adventure",
        "clear_inventory": True,
        "disable_mobs": True,
        "difficulty": "peaceful",
        "allow_flight": False,
        "target_waypoint_profile": "xaero-target-only",
        "waypoint_in_world_max_distance": 32,
        "world_copy": "reflink-or-copy-never-hardlink",
    }
    for key in ("clear_inventory", "disable_mobs", "allow_flight"):
        _boolean(runtime.get(key), f"{setting_id}.runtime.{key}")
    for key in (
        "gamemode",
        "difficulty",
        "target_waypoint_profile",
        "world_copy",
    ):
        _text(runtime.get(key), f"{setting_id}.runtime.{key}")
    waypoint_distance = _integer(
        runtime.get("waypoint_in_world_max_distance"),
        f"{setting_id}.runtime.waypoint_in_world_max_distance",
    )
    if waypoint_distance != 32:
        raise SchemaError(
            f"{setting_id}.runtime.waypoint_in_world_max_distance must be 32"
        )
    time_policy = _object(runtime.get("time"), f"{setting_id}.runtime.time")
    if set(time_policy) != {"value", "freeze"}:
        raise SchemaError(f"{setting_id}.runtime.time has unexpected or missing fields")
    if _integer(time_policy.get("value"), f"{setting_id}.runtime.time.value") < 0:
        raise SchemaError(f"{setting_id}.runtime.time.value must be non-negative")
    _boolean(time_policy.get("freeze"), f"{setting_id}.runtime.time.freeze")
    weather_policy = _object(runtime.get("weather"), f"{setting_id}.runtime.weather")
    if set(weather_policy) != {"value", "freeze"}:
        raise SchemaError(f"{setting_id}.runtime.weather has unexpected or missing fields")
    if weather_policy.get("value") not in {"clear", "rain", "thunder"}:
        raise SchemaError(f"{setting_id}.runtime.weather.value is invalid")
    _boolean(weather_policy.get("freeze"), f"{setting_id}.runtime.weather.freeze")
    fixed_runtime = {
        key: value for key, value in runtime.items() if key not in {"time", "weather"}
    }
    if fixed_runtime != expected_runtime:
        raise SchemaError(f"{setting_id}: runtime policy differs from the formal contract")

    agent = _object(payload.get("agent"), f"{setting_id}.agent")
    expected_agent = {
        "observation_mode": "event_only",
        "observe_interval_sec": 5,
        "default_periodic_observe": False,
        "allow_model_observe_toggle": False,
        "bash_timeout_sec": 300,
        "llm_timeout_sec": 600,
        "llm_max_retries": 0,
        "max_consecutive_llm_failures": 3,
        "max_images_in_context": 100,
        "auto_summarize_turn_threshold": 100,
        "auto_summarize_token_threshold": 200000,
        "max_conversation_rounds": 0,
    }
    _boolean(agent.get("six_view_enabled"), f"{setting_id}.agent.six_view_enabled")
    fixed_agent = {
        key: value for key, value in agent.items() if key != "six_view_enabled"
    }
    if fixed_agent != expected_agent:
        raise SchemaError(f"{setting_id}: agent policy differs from the formal contract")

    limits = _object(payload.get("limits"), f"{setting_id}.limits")
    if set(limits) != {"watchdog_timeout_sec", "max_assistant_steps", "max_deaths"}:
        raise SchemaError(f"{setting_id}.limits has unexpected or missing fields")
    for key in ("watchdog_timeout_sec", "max_assistant_steps", "max_deaths"):
        if _integer(limits.get(key), f"{setting_id}.limits.{key}") <= 0:
            raise SchemaError(f"{setting_id}.limits.{key} must be positive")
    if limits != {"watchdog_timeout_sec": 21600, "max_assistant_steps": 500, "max_deaths": 1}:
        raise SchemaError(f"{setting_id}: run limits differ from the formal contract")
    return payload


def load_benchmark(benchmark_id: str) -> dict[str, Any]:
    normalized = _safe_component(benchmark_id, "benchmark_id")
    path = BENCHMARKS_ROOT / f"{normalized}.json"
    payload = load_json(path)
    _schema_version(payload, f"benchmark[{benchmark_id}]")
    if payload.get("benchmark_id") != benchmark_id:
        raise SchemaError(f"{path}: benchmark_id mismatch")
    profile_id = _text(payload.get("profile_id"), f"{benchmark_id}.profile_id")
    setting_id = _text(payload.get("setting_id"), f"{benchmark_id}.setting_id")
    load_profile(profile_id)
    setting = load_setting(setting_id)
    if setting.get("profile_id") != profile_id:
        raise SchemaError(f"{benchmark_id}: setting/profile mismatch")
    admission = _object(
        payload.get("task_admission"), f"{benchmark_id}.task_admission"
    )
    expected_admission = {
        "policy": "benchmark_catalog_owner_approved",
        "reference_required": False,
        "validation_receipt_required": False,
    }
    if admission != expected_admission:
        raise SchemaError(
            f"{benchmark_id}: task admission must remain {expected_admission}"
        )
    map_ids = _list(payload.get("maps"), f"{benchmark_id}.maps")
    if not map_ids:
        raise SchemaError(f"{benchmark_id}.maps must not be empty")
    seen: set[str] = set()
    seen_task_ids: set[str] = set()
    for index, value in enumerate(map_ids):
        map_id = _text(value, f"{benchmark_id}.maps[{index}]")
        if map_id in seen:
            raise SchemaError(f"{benchmark_id}: duplicate map {map_id}")
        seen.add(map_id)
        load_map(map_id)
        for task in load_tasks(map_id):
            task_id = str(task["id"])
            if task_id in seen_task_ids:
                raise SchemaError(
                    f"{benchmark_id}: duplicate task ID across maps: {task_id}"
                )
            seen_task_ids.add(task_id)
    expected = _integer(payload.get("task_count"), f"{benchmark_id}.task_count")
    actual = sum(len(load_tasks(map_id)) for map_id in seen)
    if expected != actual:
        raise SchemaError(f"{benchmark_id}: task_count={expected}, actual={actual}")
    return payload


def snapshot_cache_path(map_payload: Mapping[str, Any]) -> Path:
    version = str(map_payload["world"]["minecraft_version"])
    return SNAPSHOT_CACHE_ROOT / version / str(map_payload["map_id"])


def runtime_template_path(profile_payload: Mapping[str, Any]) -> Path:
    version = str(profile_payload["minecraft"]["version"])
    return RUNTIME_TEMPLATE_ROOT / version


def find_task(task_id: str, map_ids: Iterable[str]) -> tuple[str, dict[str, Any]]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for map_id in map_ids:
        for task in load_tasks(map_id):
            if task["id"] == task_id:
                matches.append((map_id, task))
    if not matches:
        raise SchemaError(f"unknown navigation task: {task_id}")
    if len(matches) != 1:
        raise SchemaError(f"ambiguous navigation task id: {task_id}")
    return matches[0]


def reference_digest(reference: Mapping[str, Any]) -> str:
    return digest_json(reference)


def validation_receipt_path(map_id: str, task_id: str) -> Path:
    map_name = _safe_component(map_id, "map_id")
    task_name = _safe_component(task_id, "task_id")
    return MAPS_ROOT / map_name / "validations" / f"{task_name}.json"


def validate_receipt(
    *,
    map_id: str,
    task: Mapping[str, Any],
    reference: Mapping[str, Any],
    setting: Mapping[str, Any],
    map_payload: Mapping[str, Any],
) -> dict[str, Any]:
    task_id = str(task["id"])
    path = validation_receipt_path(map_id, task_id)
    receipt = load_json(path)
    _schema_version(receipt, f"receipt[{task_id}]")
    if receipt.get("artifact_kind") != "navigation-manual-validation-receipt":
        raise SchemaError(f"{task_id}: invalid receipt artifact_kind")
    if receipt.get("status") != "verified":
        raise SchemaError(f"{task_id}: receipt is not verified")
    expected = {
        "map_id": map_id,
        "task_id": task_id,
        "map_fingerprint": map_payload["world"].get("expected_prepared_fingerprint"),
        "task_digest": task_digest(map_id, task),
        "reference_digest": reference_digest(reference),
        "setting_digest": digest_json(
            {
                "arrival": setting["arrival"],
                "completion": setting["completion"],
                "runtime": setting["runtime"],
            }
        ),
        "minecraft_version": map_payload["world"]["minecraft_version"],
        "data_version": map_payload["world"]["data_version"],
        "profile_id": setting["profile_id"],
        "setting_id": setting["setting_id"],
    }
    mismatches = [
        key
        for key, expected_value in expected.items()
        if receipt.get(key) != expected_value
    ]
    if mismatches:
        raise SchemaError(
            f"{task_id}: stale validation receipt fields: {', '.join(mismatches)}"
        )
    _text(receipt.get("reviewer"), f"{task_id}.receipt.reviewer")
    reviewed_at = _text(
        receipt.get("reviewed_at_utc"),
        f"{task_id}.receipt.reviewed_at_utc",
    )
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchemaError(f"{task_id}: invalid receipt reviewed_at_utc") from error
    if parsed_reviewed_at.utcoffset() is None:
        raise SchemaError(f"{task_id}: receipt reviewed_at_utc must include a timezone")
    _text(receipt.get("evidence"), f"{task_id}.receipt.evidence")
    return receipt

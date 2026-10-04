"""Prepare and verify immutable Minecraft 1.21.11 navigation snapshots."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Mapping

from .schema import (
    REPO_ROOT,
    atomic_write_json,
    digest_json,
    map_preparation_digest,
    snapshot_cache_path,
)


TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12
DEFAULT_NAVIGATION_RELEASE_MANIFEST = (
    REPO_ROOT
    / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
)


class SnapshotError(RuntimeError):
    """Raised when source map material cannot become an immutable snapshot."""


class NbtReader:
    """Small dependency-free NBT reader used for level.dat and Anvil metadata."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        if size < 0:
            raise SnapshotError("negative NBT read")
        end = self.offset + size
        if end > len(self.data):
            raise SnapshotError("truncated NBT payload")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def string(self) -> str:
        length = struct.unpack(">H", self.read(2))[0]
        raw = self.read(length)
        # Java NBT uses modified UTF-8: NUL is C0 80 and supplementary
        # characters are encoded as two UTF-16 surrogate code units.
        # Keep malformed byte sequences fatal; never replace map data.
        text = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", errors="surrogatepass")
        return text.encode("utf-16-le", errors="surrogatepass").decode(
            "utf-16-le", errors="surrogatepass"
        )

    def payload(self, tag: int) -> Any:
        if tag == TAG_BYTE:
            return self.i8()
        if tag == TAG_SHORT:
            return self.i16()
        if tag == TAG_INT:
            return self.i32()
        if tag == TAG_LONG:
            return self.i64()
        if tag == TAG_FLOAT:
            return struct.unpack(">f", self.read(4))[0]
        if tag == TAG_DOUBLE:
            return struct.unpack(">d", self.read(8))[0]
        if tag == TAG_BYTE_ARRAY:
            length = self.i32()
            return self.read(length)
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_LIST:
            subtype = self.u8()
            length = self.i32()
            if length < 0:
                raise SnapshotError("negative NBT list length")
            return [self.payload(subtype) for _ in range(length)]
        if tag == TAG_COMPOUND:
            result: dict[str, Any] = {}
            while True:
                subtype = self.u8()
                if subtype == TAG_END:
                    return result
                name = self.string()
                result[name] = self.payload(subtype)
        if tag == TAG_INT_ARRAY:
            length = self.i32()
            if length < 0:
                raise SnapshotError("negative NBT int-array length")
            return [self.i32() for _ in range(length)]
        if tag == TAG_LONG_ARRAY:
            length = self.i32()
            if length < 0:
                raise SnapshotError("negative NBT long-array length")
            return [self.i64() for _ in range(length)]
        raise SnapshotError(f"unsupported NBT tag: {tag}")

    def root(self) -> dict[str, Any]:
        tag = self.u8()
        if tag != TAG_COMPOUND:
            raise SnapshotError(f"NBT root must be a compound, got tag {tag}")
        self.string()
        result = self.payload(TAG_COMPOUND)
        if not isinstance(result, dict):
            raise SnapshotError("NBT root did not decode to an object")
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_navigation_release_manifest(
    path: Path = DEFAULT_NAVIGATION_RELEASE_MANIFEST,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"cannot load navigation release manifest: {path}") from exc
    if payload.get("artifact_kind") != "navigation-map-release-manifest":
        raise SnapshotError(f"not a navigation release manifest: {path}")
    if payload.get("minecraft_version") != "1.21.11":
        raise SnapshotError(f"release manifest does not target Minecraft 1.21.11: {path}")
    assets = payload.get("assets")
    if not isinstance(assets, list) or payload.get("asset_count") != len(assets):
        raise SnapshotError(f"release manifest asset count is invalid: {path}")
    map_ids: set[str] = set()
    asset_names: set[str] = set()
    for index, raw in enumerate(assets):
        if not isinstance(raw, dict):
            raise SnapshotError(f"release manifest asset {index} is not an object")
        map_id = raw.get("map_id")
        asset_name = raw.get("asset_name")
        fingerprint = raw.get("world_fingerprint")
        if not isinstance(map_id, str) or not map_id:
            raise SnapshotError(f"release manifest asset {index} has no map_id")
        if not isinstance(asset_name, str) or not asset_name.endswith(".zip"):
            raise SnapshotError(f"release manifest asset {map_id} has an invalid name")
        if map_id in map_ids or asset_name in asset_names:
            raise SnapshotError(f"release manifest has duplicate asset metadata: {map_id}")
        map_ids.add(map_id)
        asset_names.add(asset_name)
        if (
            not isinstance(raw.get("asset_bytes"), int)
            or int(raw["asset_bytes"]) <= 0
            or not isinstance(raw.get("asset_sha256"), str)
            or len(str(raw["asset_sha256"])) != 64
            or not isinstance(raw.get("world_root"), str)
            or not raw["world_root"]
            or raw.get("minecraft_version") != "1.21.11"
            or raw.get("data_version") != 4671
            or not isinstance(fingerprint, dict)
            or fingerprint.get("algorithm")
            != "sha256-relative-path-and-content-v1"
            or not isinstance(fingerprint.get("value"), str)
            or len(str(fingerprint["value"])) != 64
            or not isinstance(fingerprint.get("file_count"), int)
            or not isinstance(fingerprint.get("total_bytes"), int)
        ):
            raise SnapshotError(f"release manifest asset metadata is invalid: {map_id}")
    return payload


def navigation_release_asset(
    manifest: Mapping[str, Any], map_id: str
) -> dict[str, Any]:
    matches = [dict(row) for row in manifest["assets"] if row.get("map_id") == map_id]
    if len(matches) != 1:
        raise SnapshotError(f"release manifest has no unique asset for map {map_id}")
    return matches[0]


def read_level_version(path: Path) -> dict[str, Any]:
    try:
        raw = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as exc:
        raise SnapshotError(f"cannot decompress level.dat: {path}") from exc
    root = NbtReader(raw).root()
    data = root.get("Data", root)
    if not isinstance(data, dict):
        raise SnapshotError(f"level.dat lacks a Data compound: {path}")
    version = data.get("Version")
    if not isinstance(version, dict):
        raise SnapshotError(f"level.dat lacks Version metadata: {path}")
    data_version = data.get("DataVersion")
    name = version.get("Name")
    version_id = version.get("Id")
    if not isinstance(data_version, int) or not isinstance(name, str):
        raise SnapshotError(f"level.dat has invalid version metadata: {path}")
    return {
        "data_version": data_version,
        "version_name": name,
        "version_id": version_id,
    }


def _safe_zip_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise SnapshotError(f"unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotError(f"unsafe ZIP member path: {name!r}")
    return path


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def validate_archive(
    archive: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    world_root: str,
) -> list[zipfile.ZipInfo]:
    if not archive.is_file():
        raise SnapshotError(f"map archive does not exist: {archive}")
    actual_bytes = archive.stat().st_size
    if actual_bytes != expected_bytes:
        raise SnapshotError(
            f"archive size mismatch for {archive.name}: expected {expected_bytes}, got {actual_bytes}"
        )
    actual_sha256 = sha256_file(archive)
    if actual_sha256 != expected_sha256:
        raise SnapshotError(
            f"archive SHA-256 mismatch for {archive.name}: expected {expected_sha256}, got {actual_sha256}"
        )

    normalized_root = world_root.rstrip("/") + "/"
    selected: list[zipfile.ZipInfo] = []
    try:
        with zipfile.ZipFile(archive) as handle:
            bad_member = handle.testzip()
            if bad_member is not None:
                raise SnapshotError(f"ZIP CRC failure in {archive.name}: {bad_member}")
            for info in handle.infolist():
                member = _safe_zip_path(info.filename)
                if _is_zip_symlink(info):
                    raise SnapshotError(f"ZIP symlink is not allowed: {info.filename}")
                if member.as_posix() == normalized_root.rstrip("/") or info.is_dir():
                    continue
                if member.as_posix().startswith(normalized_root):
                    selected.append(info)
    except zipfile.BadZipFile as exc:
        raise SnapshotError(f"invalid ZIP archive: {archive}") from exc

    if not selected:
        raise SnapshotError(
            f"archive {archive.name} contains no files below world root {world_root!r}"
        )
    relative_name_rows = [
        PurePosixPath(info.filename).as_posix()[len(normalized_root) :]
        for info in selected
    ]
    if len(relative_name_rows) != len(set(relative_name_rows)):
        raise SnapshotError(f"archive {archive.name} has duplicate world paths")
    casefolded = [name.casefold() for name in relative_name_rows]
    if len(casefolded) != len(set(casefolded)):
        raise SnapshotError(
            f"archive {archive.name} has case-colliding world paths"
        )
    relative_names = set(relative_name_rows)
    if "level.dat" not in relative_names:
        raise SnapshotError(
            f"archive {archive.name} world root {world_root!r} has no level.dat"
        )
    return selected


def _copy_member(source: BinaryIO, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def extract_world(
    archive: Path,
    *,
    world_root: str,
    selected: Iterable[zipfile.ZipInfo],
    destination: Path,
) -> None:
    normalized_root = world_root.rstrip("/") + "/"
    destination.mkdir(parents=True, exist_ok=False)
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as handle:
        for info in selected:
            relative_text = PurePosixPath(info.filename).as_posix()[len(normalized_root) :]
            relative = _safe_zip_path(relative_text)
            target = destination.joinpath(*relative.parts)
            resolved_parent = target.parent.resolve()
            try:
                resolved_parent.relative_to(destination_resolved)
            except ValueError as exc:
                raise SnapshotError(f"ZIP member escapes destination: {info.filename}") from exc
            with handle.open(info, "r") as source:
                _copy_member(source, target)


def file_tree_rows(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise SnapshotError(f"file tree does not exist: {root}")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SnapshotError(f"file tree contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        rows.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return rows


def world_file_rows(world_dir: Path) -> list[dict[str, Any]]:
    if not (world_dir / "level.dat").is_file():
        raise SnapshotError(f"world directory lacks level.dat: {world_dir}")
    return file_tree_rows(world_dir)


def fingerprint_world_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    digest = hashlib.sha256()
    total_bytes = 0
    for row in materialized:
        digest.update(f"{row['sha256']}  {row['path']}\n".encode("utf-8"))
        total_bytes += int(row["bytes"])
    return {
        "algorithm": "sha256-relative-path-and-content-v1",
        "value": digest.hexdigest(),
        "file_count": len(materialized),
        "total_bytes": total_bytes,
    }


def _json_safe_nbt(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_nbt(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_nbt(item) for item in value]
    if isinstance(value, bytes):
        return {"byte_array_sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    return value


def fingerprint_upgraded_world(
    world_dir: Path, rows: Iterable[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Fingerprint an upgraded world without NBT compound serialization noise."""

    materialized = [dict(row) for row in (rows or world_file_rows(world_dir))]
    level_path = world_dir / "level.dat"
    level = NbtReader(gzip.decompress(level_path.read_bytes())).root()
    semantic_level = digest_json(_json_safe_nbt(level))
    digest = hashlib.sha256()
    total_bytes = 0
    for row in materialized:
        row_digest = semantic_level if row["path"] == "level.dat" else str(row["sha256"])
        digest.update(f"{row_digest}  {row['path']}\n".encode("utf-8"))
        total_bytes += int(row["bytes"])
    return {
        "algorithm": "sha256-relative-path-content-and-semantic-level-nbt-v1",
        "value": digest.hexdigest(),
        "file_count": len(materialized),
        "total_bytes": total_bytes,
    }


def prepared_world_fingerprint(
    map_payload: Mapping[str, Any],
    world_dir: Path,
    rows: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if map_payload["world"]["preparation"]["id"] == "minecraft-server-force-upgrade-1.21.11-v1":
        return fingerprint_upgraded_world(world_dir, rows)
    return fingerprint_world_rows(rows or world_file_rows(world_dir))


def world_fingerprint(world_dir: Path) -> dict[str, Any]:
    return fingerprint_world_rows(world_file_rows(world_dir))


def write_world_manifest(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    lines = [f"{row['sha256']}  {row['path']}" for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chmod_world_read_only(world_dir: Path) -> None:
    for path in sorted(world_dir.rglob("*"), reverse=True):
        if path.is_symlink():
            raise SnapshotError(f"refusing to chmod snapshot symlink: {path}")
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    world_dir.chmod(0o555)


def make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            current = stat.S_IMODE(path.stat().st_mode)
            path.chmod(current | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
        except FileNotFoundError:
            continue


def _assert_no_hardlinks(source: Path, destination: Path) -> None:
    for source_file in source.rglob("*"):
        if not source_file.is_file() or source_file.is_symlink():
            continue
        target = destination / source_file.relative_to(source)
        if not target.is_file():
            raise SnapshotError(f"prepared copy is incomplete; missing {target}")
        source_stat = source_file.stat()
        target_stat = target.stat()
        if (
            source_stat.st_dev == target_stat.st_dev
            and source_stat.st_ino == target_stat.st_ino
        ):
            raise SnapshotError(f"hardlinked prepared file is forbidden: {target}")


def _clone_tree(source: Path, destination: Path) -> str:
    if not source.is_dir():
        raise SnapshotError(f"prepared copy source is missing: {source}")
    if destination.exists():
        raise SnapshotError(f"prepared copy destination already exists: {destination}")
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


def _install_tree(source: Path, destination: Path) -> None:
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


def _download_release_asset(
    *,
    repository: str,
    release_id: int,
    asset_name: str,
    destination: Path,
) -> None:
    try:
        release = subprocess.run(
            ["gh", "api", f"repos/{repository}/releases/{release_id}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SnapshotError("gh is required for --download-missing") from exc
    except subprocess.CalledProcessError as exc:
        raise SnapshotError(
            f"failed to inspect private GitHub release: {exc.stderr.strip()}"
        ) from exc
    try:
        payload = json.loads(release.stdout)
        assets = payload["assets"]
        asset = next(row for row in assets if row.get("name") == asset_name)
        asset_id = int(asset["id"])
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise SnapshotError(
            f"release {release_id} has no asset named {asset_name!r}"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{asset_name}.",
        suffix=".download",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as output:
            process = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/octet-stream",
                    f"repos/{repository}/releases/assets/{asset_id}",
                ],
                stdout=output,
                stderr=subprocess.PIPE,
            )
        if process.returncode != 0:
            raise SnapshotError(
                f"failed to download {asset_name}: "
                f"{process.stderr.decode('utf-8', errors='replace').strip()}"
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_config_digest(map_payload: Mapping[str, Any]) -> str:
    world = map_payload["world"]
    source_version = world.get(
        "source_version",
        {"version_name": world["version_name"], "data_version": world["data_version"]},
    )
    return digest_json(
        {
            "map_id": map_payload["map_id"],
            "source": map_payload["source"],
            "world": {
                "source_version": source_version,
                "expected_source_fingerprint": world[
                    "expected_source_fingerprint"
                ],
            },
        }
    )


def _expected_prepared_world_rows(
    map_payload: Mapping[str, Any],
    source_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    preparation = map_payload["world"]["preparation"]
    source_datapack = preparation["source_datapack"]
    source_prefix = str(source_datapack["relative_path"]).rstrip("/") + "/"
    expected_source_files = {
        str(path): str(digest)
        for path, digest in source_datapack["files"].items()
    }
    materialized_source_rows = [dict(row) for row in source_rows]
    actual_source_files = {
        str(row["path"])[len(source_prefix) :]: str(row["sha256"])
        for row in materialized_source_rows
        if str(row["path"]).startswith(source_prefix)
    }
    if actual_source_files != expected_source_files:
        raise SnapshotError(
            f"source heights datapack differs from its lock for "
            f"{map_payload['map_id']}"
        )

    replacement = preparation["replacement_datapack"]
    replacement_source = REPO_ROOT / str(replacement["source_path"])
    replacement_rows = file_tree_rows(replacement_source)
    install_prefix = str(replacement["install_path"]).rstrip("/") + "/"
    output = [
        row
        for row in materialized_source_rows
        if not str(row["path"]).startswith(source_prefix)
    ]
    output.extend(
        {
            **row,
            "path": install_prefix + str(row["path"]),
        }
        for row in replacement_rows
    )
    output.sort(key=lambda row: str(row["path"]))
    return output


def _expected_compatibility_world_rows(
    map_payload: Mapping[str, Any],
    source_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    preparation = map_payload["world"]["preparation"]
    remove_paths = [str(value).rstrip("/") for value in preparation["remove_paths"]]
    output = [
        dict(row)
        for row in source_rows
        if not any(
            str(row["path"]) == prefix
            or str(row["path"]).startswith(prefix + "/")
            for prefix in remove_paths
        )
    ]
    replacement = preparation["replacement_datapack"]
    replacement_rows = file_tree_rows(REPO_ROOT / str(replacement["source_path"]))
    install_prefix = str(replacement["install_path"]).rstrip("/") + "/"
    output.extend(
        {**row, "path": install_prefix + str(row["path"])}
        for row in replacement_rows
    )
    output.sort(key=lambda row: str(row["path"]))
    return output


def _verify_tree_metadata(
    *,
    map_id: str,
    label: str,
    world_dir: Path,
    fingerprint_path: Path,
    manifest_path: Path,
    map_payload: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = world_file_rows(world_dir)
    actual = (
        prepared_world_fingerprint(map_payload, world_dir, rows)
        if map_payload is not None
        else fingerprint_world_rows(rows)
    )
    stored = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    if actual != stored:
        raise SnapshotError(f"{label} content drift detected for {map_id}")
    expected_manifest = "\n".join(
        f"{row['sha256']}  {row['path']}" for row in rows
    ) + "\n"
    if manifest_path.read_text(encoding="utf-8") != expected_manifest:
        raise SnapshotError(f"{label} manifest drift detected for {map_id}")
    return rows, actual


def _release_manifest_receipt_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_release_manifest_receipt_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise SnapshotError("release snapshot receipt has no manifest path")
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _release_source_fingerprint(map_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": "sha256-relative-path-and-content-v1",
        "value": str(map_payload["world"]["expected_source_fingerprint"]),
    }


def verify_release_snapshot(
    map_payload: Mapping[str, Any],
    *,
    require_expected_fingerprint: bool = True,
) -> dict[str, Any]:
    map_id = str(map_payload["map_id"])
    cache = snapshot_cache_path(map_payload) / "prepared"
    world_dir = cache / "world"
    receipt_path = cache / "release-receipt.json"
    fingerprint_path = cache / "fingerprint.json"
    manifest_path = cache / "world-files.sha256"
    for path in (world_dir, receipt_path, fingerprint_path, manifest_path):
        if not path.exists():
            raise SnapshotError(f"incomplete release snapshot; missing {path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("artifact_kind") != "navigation-release-snapshot-receipt":
        raise SnapshotError(f"invalid release snapshot receipt for {map_id}")
    if receipt.get("map_id") != map_id:
        raise SnapshotError(f"release snapshot receipt map mismatch for {map_id}")

    release_manifest_path = _resolve_release_manifest_receipt_path(
        receipt.get("release_manifest_path")
    )
    release_manifest = load_navigation_release_manifest(release_manifest_path)
    release_manifest_digest = digest_json(release_manifest)
    if receipt.get("release_manifest_digest") != release_manifest_digest:
        raise SnapshotError(f"release manifest binding is stale for {map_id}")
    asset = navigation_release_asset(release_manifest, map_id)
    release_asset = receipt.get("release_asset")
    expected_release_asset = {
        "name": asset["asset_name"],
        "bytes": asset["asset_bytes"],
        "sha256": asset["asset_sha256"],
        "world_root": asset["world_root"],
    }
    if release_asset != expected_release_asset:
        raise SnapshotError(f"release asset binding is stale for {map_id}")
    if receipt.get("release_tag") != release_manifest.get("release_tag"):
        raise SnapshotError(f"release tag binding is stale for {map_id}")
    if receipt.get("preparation_id") != map_payload["world"]["preparation"]["id"]:
        raise SnapshotError(f"release preparation binding is stale for {map_id}")
    if receipt.get("preparation_config_digest") != map_preparation_digest(map_payload):
        raise SnapshotError(f"release preparation digest is stale for {map_id}")

    level = read_level_version(world_dir / "level.dat")
    if (
        level["version_name"] != asset["minecraft_version"]
        or level["data_version"] != asset["data_version"]
    ):
        raise SnapshotError(f"release world version mismatch for {map_id}: {level}")
    rows, actual = _verify_tree_metadata(
        map_id=map_id,
        label="release snapshot",
        world_dir=world_dir,
        fingerprint_path=fingerprint_path,
        manifest_path=manifest_path,
    )
    expected = dict(asset["world_fingerprint"])
    if actual != expected:
        raise SnapshotError(
            f"release world fingerprint mismatch for {map_id}: "
            f"expected {expected['value']}, got {actual['value']}"
        )
    if receipt.get("fingerprint") != actual:
        raise SnapshotError(f"release snapshot receipt fingerprint is stale for {map_id}")
    source_fingerprint = _release_source_fingerprint(map_payload)
    if receipt.get("source_fingerprint") != source_fingerprint:
        raise SnapshotError(f"release source provenance is stale for {map_id}")
    if receipt.get("mutation_policy") != "exact_release_archive_extraction":
        raise SnapshotError(f"release snapshot mutation policy is invalid for {map_id}")
    if require_expected_fingerprint and not actual.get("value"):
        raise SnapshotError(f"release snapshot does not pin a fingerprint for {map_id}")
    return {
        "cache": str(cache),
        "world_dir": str(world_dir),
        "level": level,
        "fingerprint": actual,
        "file_rows": rows,
        "receipt": receipt,
        "source": {
            "world_dir": None,
            "fingerprint": source_fingerprint,
            "receipt": {
                "artifact_kind": "navigation-release-source-provenance",
                "map_id": map_id,
                "release_manifest_digest": release_manifest_digest,
            },
        },
    }


def import_release_snapshot(
    map_payload: Mapping[str, Any],
    *,
    manifest_path: Path = DEFAULT_NAVIGATION_RELEASE_MANIFEST,
    downloads_dir: Path,
    replace: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    release_manifest = load_navigation_release_manifest(manifest_path)
    map_id = str(map_payload["map_id"])
    asset = navigation_release_asset(release_manifest, map_id)
    archive = downloads_dir.expanduser().resolve() / str(asset["asset_name"])
    prepared_cache = snapshot_cache_path(map_payload) / "prepared"
    if prepared_cache.exists() and not replace:
        return verify_release_snapshot(map_payload)
    selected = validate_archive(
        archive,
        expected_sha256=str(asset["asset_sha256"]),
        expected_bytes=int(asset["asset_bytes"]),
        world_root=str(asset["world_root"]),
    )
    prepared_cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{map_id}.release.",
        dir=prepared_cache.parent,
    ) as temporary_name:
        temporary = Path(temporary_name)
        world_dir = temporary / "world"
        extract_world(
            archive,
            world_root=str(asset["world_root"]),
            selected=selected,
            destination=world_dir,
        )
        level = read_level_version(world_dir / "level.dat")
        if (
            level["version_name"] != asset["minecraft_version"]
            or level["data_version"] != asset["data_version"]
        ):
            raise SnapshotError(f"release world version mismatch for {map_id}: {level}")
        rows = world_file_rows(world_dir)
        fingerprint = fingerprint_world_rows(rows)
        expected_fingerprint = dict(asset["world_fingerprint"])
        if fingerprint != expected_fingerprint:
            raise SnapshotError(
                f"release world fingerprint mismatch for {map_id}: "
                f"expected {expected_fingerprint['value']}, got {fingerprint['value']}"
            )
        write_world_manifest(temporary / "world-files.sha256", rows)
        atomic_write_json(temporary / "fingerprint.json", fingerprint)
        receipt = {
            "schema_version": 1,
            "artifact_kind": "navigation-release-snapshot-receipt",
            "imported_at_utc": utc_now(),
            "map_id": map_id,
            "release_manifest_path": _release_manifest_receipt_path(manifest_path),
            "release_manifest_digest": digest_json(release_manifest),
            "release_tag": release_manifest["release_tag"],
            "release_url": release_manifest["release_url"],
            "release_asset": {
                "name": asset["asset_name"],
                "bytes": asset["asset_bytes"],
                "sha256": asset["asset_sha256"],
                "world_root": asset["world_root"],
            },
            "level": level,
            "fingerprint": fingerprint,
            "source_fingerprint": _release_source_fingerprint(map_payload),
            "preparation_id": map_payload["world"]["preparation"]["id"],
            "preparation_config_digest": map_preparation_digest(map_payload),
            "mutation_policy": "exact_release_archive_extraction",
        }
        atomic_write_json(temporary / "release-receipt.json", receipt)
        _chmod_world_read_only(world_dir)
        _install_tree(temporary, prepared_cache)
    return verify_release_snapshot(map_payload)


def verify_source_snapshot(
    map_payload: Mapping[str, Any],
    *,
    require_expected_fingerprint: bool = True,
) -> dict[str, Any]:
    cache = snapshot_cache_path(map_payload)
    world_dir = cache / "world"
    receipt_path = cache / "source-receipt.json"
    fingerprint_path = cache / "fingerprint.json"
    manifest_path = cache / "world-files.sha256"
    for path in (world_dir, receipt_path, fingerprint_path, manifest_path):
        if not path.exists():
            raise SnapshotError(f"incomplete source snapshot; missing {path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("artifact_kind") != "navigation-source-snapshot-receipt":
        raise SnapshotError(f"invalid source receipt for {map_payload['map_id']}")
    if receipt.get("map_id") != map_payload["map_id"]:
        raise SnapshotError(f"source receipt map mismatch for {map_payload['map_id']}")
    if receipt.get("map_source_config_digest") != _source_config_digest(map_payload):
        raise SnapshotError(
            f"source snapshot receipt is stale for map {map_payload['map_id']}"
        )
    level = read_level_version(world_dir / "level.dat")
    expected_world = map_payload["world"]
    expected_source = expected_world.get(
        "source_version",
        {
            "data_version": expected_world["data_version"],
            "version_name": expected_world["version_name"],
        },
    )
    if (
        level["data_version"] != expected_source["data_version"]
        or level["version_name"] != expected_source["version_name"]
        or (
            "version_id" in expected_source
            and level["version_id"] != expected_source["version_id"]
        )
    ):
        raise SnapshotError(
            f"source level.dat version mismatch for {map_payload['map_id']}: {level}"
        )
    rows, actual = _verify_tree_metadata(
        map_id=str(map_payload["map_id"]),
        label="source snapshot",
        world_dir=world_dir,
        fingerprint_path=fingerprint_path,
        manifest_path=manifest_path,
    )
    expected = expected_world.get("expected_source_fingerprint")
    if require_expected_fingerprint and not expected:
        raise SnapshotError(
            f"map {map_payload['map_id']} does not pin a source fingerprint"
        )
    if expected and actual["value"] != expected:
        raise SnapshotError(
            f"source snapshot fingerprint mismatch for {map_payload['map_id']}: "
            f"expected {expected}, got {actual['value']}"
        )
    if receipt.get("fingerprint") != actual:
        raise SnapshotError(
            f"source snapshot receipt fingerprint is stale for {map_payload['map_id']}"
        )
    if receipt.get("mutation_policy") != "exact_archive_extraction_no_world_mutation":
        raise SnapshotError(
            f"source snapshot mutation policy is invalid for {map_payload['map_id']}"
        )
    return {
        "cache": str(cache),
        "world_dir": str(world_dir),
        "level": level,
        "fingerprint": actual,
        "file_rows": rows,
        "receipt": receipt,
    }


def verify_prepared_snapshot(
    map_payload: Mapping[str, Any],
    *,
    require_expected_fingerprint: bool = True,
) -> dict[str, Any]:
    source = verify_source_snapshot(
        map_payload,
        require_expected_fingerprint=require_expected_fingerprint,
    )
    cache = snapshot_cache_path(map_payload) / "prepared"
    world_dir = cache / "world"
    receipt_path = cache / "prepared-receipt.json"
    fingerprint_path = cache / "fingerprint.json"
    manifest_path = cache / "world-files.sha256"
    for path in (world_dir, receipt_path, fingerprint_path, manifest_path):
        if not path.exists():
            raise SnapshotError(f"incomplete prepared snapshot; missing {path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("artifact_kind") != "navigation-prepared-snapshot-receipt":
        raise SnapshotError(f"invalid prepared receipt for {map_payload['map_id']}")
    if receipt.get("map_id") != map_payload["map_id"]:
        raise SnapshotError(f"prepared receipt map mismatch for {map_payload['map_id']}")
    preparation = map_payload["world"]["preparation"]
    if receipt.get("preparation_id") != preparation["id"]:
        raise SnapshotError(
            f"prepared snapshot transform is stale for map {map_payload['map_id']}"
        )
    if (
        receipt.get("preparation_config_digest")
        != map_preparation_digest(map_payload)
    ):
        raise SnapshotError(
            f"prepared snapshot receipt is stale for map {map_payload['map_id']}"
        )
    if receipt.get("source_fingerprint") != source["fingerprint"]["value"]:
        raise SnapshotError(
            f"prepared snapshot source binding is stale for {map_payload['map_id']}"
        )
    level = read_level_version(world_dir / "level.dat")
    preparation_id = preparation["id"]
    if preparation_id in {
        "heights-datapack-minecraft-1.21.11-v1",
        "height-datapack-compat-minecraft-1.21.11-v1",
        "identity-minecraft-1.21.11-v1",
    }:
        if level != source["level"]:
            raise SnapshotError(
                f"prepared level.dat differs from source for {map_payload['map_id']}"
            )
    elif (
        level["data_version"] != map_payload["world"]["data_version"]
        or level["version_name"] != map_payload["world"]["version_name"]
    ):
        raise SnapshotError(
            f"prepared world was not upgraded to 1.21.11 for {map_payload['map_id']}: {level}"
        )
    rows, actual = _verify_tree_metadata(
        map_id=str(map_payload["map_id"]),
        label="prepared snapshot",
        world_dir=world_dir,
        fingerprint_path=fingerprint_path,
        manifest_path=manifest_path,
        map_payload=map_payload,
    )
    if preparation_id in {
        "heights-datapack-minecraft-1.21.11-v1",
        "height-datapack-compat-minecraft-1.21.11-v1",
        "identity-minecraft-1.21.11-v1",
    }:
        if preparation_id == "heights-datapack-minecraft-1.21.11-v1":
            expected_rows = _expected_prepared_world_rows(
                map_payload, source["file_rows"]
            )
        elif preparation_id == "height-datapack-compat-minecraft-1.21.11-v1":
            expected_rows = _expected_compatibility_world_rows(
                map_payload, source["file_rows"]
            )
        else:
            expected_rows = source["file_rows"]
        if rows != expected_rows:
            raise SnapshotError(
                f"prepared snapshot contains changes outside the locked datapack "
                f"replacement for {map_payload['map_id']}"
            )
    expected = map_payload["world"].get("expected_prepared_fingerprint")
    if require_expected_fingerprint and not expected:
        raise SnapshotError(
            f"map {map_payload['map_id']} does not pin a prepared fingerprint"
        )
    if expected and actual["value"] != expected:
        raise SnapshotError(
            f"prepared snapshot fingerprint mismatch for {map_payload['map_id']}: "
            f"expected {expected}, got {actual['value']}"
        )
    if receipt.get("fingerprint") != actual:
        raise SnapshotError(
            f"prepared snapshot receipt fingerprint is stale for "
            f"{map_payload['map_id']}"
        )
    if preparation_id in {
        "heights-datapack-minecraft-1.21.11-v1",
        "height-datapack-compat-minecraft-1.21.11-v1",
    }:
        replacement = preparation["replacement_datapack"]
        if receipt.get("replacement_manifest_sha256") != replacement["manifest_sha256"]:
            raise SnapshotError(
                f"prepared datapack manifest binding is stale for {map_payload['map_id']}"
            )
        expected_policy = "source_clone_with_locked_heights_datapack_replacement_only"
    elif preparation_id == "identity-minecraft-1.21.11-v1":
        expected_policy = "exact_source_clone_no_world_mutation"
    else:
        expected_policy = "source_clone_upgraded_once_by_pinned_minecraft_server"
        upgrade_log = cache / "world.upgrade.log"
        if not upgrade_log.is_file() or receipt.get("upgrade_log_sha256") != sha256_file(upgrade_log):
            raise SnapshotError(
                f"prepared upgrade log is missing or stale for {map_payload['map_id']}"
            )
    if receipt.get("mutation_policy") != expected_policy:
        raise SnapshotError(
            f"prepared snapshot mutation policy is invalid for "
            f"{map_payload['map_id']}"
        )
    return {
        "cache": str(cache),
        "world_dir": str(world_dir),
        "level": level,
        "fingerprint": actual,
        "file_rows": rows,
        "receipt": receipt,
        "source": source,
    }


def verify_snapshot(
    map_payload: Mapping[str, Any],
    *,
    require_expected_fingerprint: bool = True,
) -> dict[str, Any]:
    """Verify and return the immutable execution-ready prepared snapshot."""

    release_receipt = (
        snapshot_cache_path(map_payload) / "prepared" / "release-receipt.json"
    )
    if release_receipt.is_file():
        return verify_release_snapshot(
            map_payload,
            require_expected_fingerprint=require_expected_fingerprint,
        )
    return verify_prepared_snapshot(
        map_payload,
        require_expected_fingerprint=require_expected_fingerprint,
    )


def _prepare_prepared_snapshot(
    map_payload: Mapping[str, Any],
    source_snapshot: Mapping[str, Any],
    *,
    replace: bool,
) -> dict[str, Any]:
    cache = snapshot_cache_path(map_payload)
    prepared_cache = cache / "prepared"
    if prepared_cache.exists() and not replace:
        return verify_prepared_snapshot(map_payload)
    with tempfile.TemporaryDirectory(
        prefix=".prepared.",
        dir=cache,
    ) as temporary_name:
        temporary = Path(temporary_name) / "prepared"
        temporary.mkdir()
        preparation = map_payload["world"]["preparation"]
        world_dir = temporary / "world"
        if preparation["id"] == "minecraft-server-force-upgrade-1.21.11-v1":
            command = [
                sys.executable,
                str(REPO_ROOT / "scripts/snapshot/upgrade-navigation-world.py"),
                "--source-world",
                str(source_snapshot["world_dir"]),
                "--output-world",
                str(world_dir),
            ]
            process = subprocess.run(
                command,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if process.returncode != 0:
                raise SnapshotError(
                    f"Minecraft 1.21.11 upgrade failed for {map_payload['map_id']}: "
                    f"{process.stderr.strip() or process.stdout.strip()}"
                )
            copy_method = "minecraft-server-force-upgrade"
            upgrade_log = temporary / "world.upgrade.log"
            upgrade_log_digest = sha256_file(upgrade_log) if upgrade_log.is_file() else None
        else:
            copy_method = _clone_tree(
                Path(str(source_snapshot["world_dir"])),
                world_dir,
            )
            upgrade_log_digest = None
        if preparation["id"] == "heights-datapack-minecraft-1.21.11-v1":
            source_datapack = (
                world_dir / str(preparation["source_datapack"]["relative_path"])
            )
            if not source_datapack.is_dir() or source_datapack.is_symlink():
                raise SnapshotError(
                    f"source heights datapack is missing for {map_payload['map_id']}"
                )
            shutil.rmtree(source_datapack)
            replacement = preparation["replacement_datapack"]
            replacement_source = REPO_ROOT / str(replacement["source_path"])
            shutil.copytree(replacement_source, source_datapack)
        elif preparation["id"] == "height-datapack-compat-minecraft-1.21.11-v1":
            for relative in preparation["remove_paths"]:
                target = world_dir / str(relative)
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            replacement = preparation["replacement_datapack"]
            replacement_source = REPO_ROOT / str(replacement["source_path"])
            install_path = world_dir / str(replacement["install_path"])
            install_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(replacement_source, install_path)

        rows = world_file_rows(world_dir)
        if preparation["id"] in {
            "heights-datapack-minecraft-1.21.11-v1",
            "height-datapack-compat-minecraft-1.21.11-v1",
            "identity-minecraft-1.21.11-v1",
        }:
            if preparation["id"] == "heights-datapack-minecraft-1.21.11-v1":
                expected_rows = _expected_prepared_world_rows(
                    map_payload, source_snapshot["file_rows"]
                )
            elif preparation["id"] == "height-datapack-compat-minecraft-1.21.11-v1":
                expected_rows = _expected_compatibility_world_rows(
                    map_payload, source_snapshot["file_rows"]
                )
            else:
                expected_rows = source_snapshot["file_rows"]
            if rows != expected_rows:
                raise SnapshotError(
                    f"prepared world differs from its deterministic transform for "
                    f"{map_payload['map_id']}"
                )
        fingerprint = prepared_world_fingerprint(map_payload, world_dir, rows)
        expected_fingerprint = map_payload["world"].get(
            "expected_prepared_fingerprint"
        )
        if expected_fingerprint and fingerprint["value"] != expected_fingerprint:
            raise SnapshotError(
                f"prepared world fingerprint mismatch for {map_payload['map_id']}: "
                f"expected {expected_fingerprint}, got {fingerprint['value']}"
            )
        write_world_manifest(temporary / "world-files.sha256", rows)
        atomic_write_json(temporary / "fingerprint.json", fingerprint)
        receipt = {
            "schema_version": 1,
            "artifact_kind": "navigation-prepared-snapshot-receipt",
            "prepared_at_utc": utc_now(),
            "map_id": map_payload["map_id"],
            "preparation_id": preparation["id"],
            "preparation_config_digest": map_preparation_digest(map_payload),
            "source_fingerprint": source_snapshot["fingerprint"]["value"],
            "copy_method": copy_method,
            "level": read_level_version(world_dir / "level.dat"),
            "fingerprint": fingerprint,
            "mutation_policy": (
                "source_clone_with_locked_heights_datapack_replacement_only"
                if preparation["id"] in {
                    "heights-datapack-minecraft-1.21.11-v1",
                    "height-datapack-compat-minecraft-1.21.11-v1",
                }
                else (
                    "exact_source_clone_no_world_mutation"
                    if preparation["id"] == "identity-minecraft-1.21.11-v1"
                    else "source_clone_upgraded_once_by_pinned_minecraft_server"
                )
            ),
        }
        if preparation["id"] in {
            "heights-datapack-minecraft-1.21.11-v1",
            "height-datapack-compat-minecraft-1.21.11-v1",
        }:
            receipt["replacement_manifest_sha256"] = replacement["manifest_sha256"]
        elif preparation["id"] == "minecraft-server-force-upgrade-1.21.11-v1":
            receipt["server_arguments"] = preparation["server_arguments"]
            receipt["runtime_profile"] = preparation["runtime_profile"]
            receipt["upgrade_log_sha256"] = upgrade_log_digest
            receipt["compatibility_warnings"] = [
                line.strip()
                for line in upgrade_log.read_text(encoding="utf-8").splitlines()
                if any(
                    marker in line
                    for marker in (
                        "MISSING",
                        "Missing data pack",
                        "Failed to parse saved data",
                    )
                )
            ]
        atomic_write_json(temporary / "prepared-receipt.json", receipt)
        _chmod_world_read_only(world_dir)
        _install_tree(temporary, prepared_cache)
    return verify_prepared_snapshot(map_payload)


def prepare_snapshot(
    map_payload: Mapping[str, Any],
    *,
    downloads_dir: Path | None = None,
    replace: bool = False,
    download_missing: bool = False,
) -> dict[str, Any]:
    downloads = downloads_dir or REPO_ROOT / "downloads"
    source = map_payload["source"]
    archive = downloads / str(source.get("archive_path", source["asset_name"]))
    if not archive.is_file() and download_missing:
        if "release_id" not in source:
            raise SnapshotError(
                f"map {map_payload['map_id']} has no downloadable release asset"
            )
        _download_release_asset(
            repository=str(source["repository"]),
            release_id=int(source["release_id"]),
            asset_name=str(source["asset_name"]),
            destination=archive,
        )

    cache = snapshot_cache_path(map_payload)
    if cache.exists() and not replace:
        source_snapshot = verify_source_snapshot(map_payload)
        return _prepare_prepared_snapshot(
            map_payload,
            source_snapshot,
            replace=False,
        )
    selected = validate_archive(
        archive,
        expected_sha256=str(source["archive_sha256"]),
        expected_bytes=int(source["archive_bytes"]),
        world_root=str(source["world_root"]),
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{map_payload['map_id']}.",
        dir=cache.parent,
    ) as temporary_name:
        temporary = Path(temporary_name)
        world_dir = temporary / "world"
        extract_world(
            archive,
            world_root=str(source["world_root"]),
            selected=selected,
            destination=world_dir,
        )
        level = read_level_version(world_dir / "level.dat")
        expected_world = map_payload["world"]
        expected_source = expected_world.get(
            "source_version",
            {
                "data_version": expected_world["data_version"],
                "version_name": expected_world["version_name"],
            },
        )
        if (
            level["data_version"] != expected_source["data_version"]
            or level["version_name"] != expected_source["version_name"]
            or (
                "version_id" in expected_source
                and level["version_id"] != expected_source["version_id"]
            )
        ):
            raise SnapshotError(
                f"source world version mismatch for {map_payload['map_id']}: {level}"
            )
        rows = world_file_rows(world_dir)
        fingerprint = fingerprint_world_rows(rows)
        expected_fingerprint = expected_world.get("expected_source_fingerprint")
        if expected_fingerprint and fingerprint["value"] != expected_fingerprint:
            raise SnapshotError(
                f"source world fingerprint mismatch for {map_payload['map_id']}: "
                f"expected {expected_fingerprint}, got {fingerprint['value']}"
            )
        write_world_manifest(temporary / "world-files.sha256", rows)
        atomic_write_json(temporary / "fingerprint.json", fingerprint)
        receipt = {
            "schema_version": 1,
            "artifact_kind": "navigation-source-snapshot-receipt",
            "extracted_at_utc": utc_now(),
            "map_id": map_payload["map_id"],
            "map_source_config_digest": _source_config_digest(map_payload),
            "source_archive": {
                "name": source["asset_name"],
                "bytes": source["archive_bytes"],
                "sha256": source["archive_sha256"],
                "world_root": source["world_root"],
            },
            "level": level,
            "fingerprint": fingerprint,
            "mutation_policy": "exact_archive_extraction_no_world_mutation",
        }
        atomic_write_json(temporary / "source-receipt.json", receipt)
        _chmod_world_read_only(world_dir)
        _install_tree(temporary, cache)

    source_snapshot = verify_source_snapshot(map_payload)
    return _prepare_prepared_snapshot(
        map_payload,
        source_snapshot,
        replace=False,
    )

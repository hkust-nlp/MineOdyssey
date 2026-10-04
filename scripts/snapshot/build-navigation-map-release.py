#!/usr/bin/env python3
"""Build deterministic Minecraft 1.21.11 navigation release archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.snapshots import (  # noqa: E402
    SnapshotError,
    extract_world,
    fingerprint_world_rows,
    read_level_version,
    sha256_file,
    world_file_rows,
)


DOWNLOADS = REPO_ROOT / "downloads"
MAPS_ROOT = REPO_ROOT / "eval/navigation/maps"
CACHE_ROOT = REPO_ROOT / "eval/snapshots/_cache/navigation/1.21.11"
HEIGHTS_PACK = REPO_ROOT / "eval/navigation/compatibility/heights-1.21.11"
DEFAULT_OUTPUT = REPO_ROOT / "eval/snapshots/_work/navigation-map-release-1.21.11-v1"
TRACKED_RELEASE_MANIFEST = (
    REPO_ROOT / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
)
RELEASE_TAG = "navigation-maps-1.21.11-v1"
RELEASE_URL = f"https://example.invalid/anonymous-source"
LEGACY_HEIGHT_PACKS = {
    "heights",
    "heights.zip",
    "world-height-datapack.zip",
}
FIXED_ZIP_TIME = (2026, 8, 15, 0, 0, 0)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def slugify(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    ascii_value = unicodedata.normalize("NFKD", separated).encode(
        "ascii", "ignore"
    ).decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    if not normalized:
        raise SnapshotError(f"cannot derive map id from {value!r}")
    return normalized


def existing_map_sources() -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for path in sorted(MAPS_ROOT.glob("*/map.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = payload["source"]
        archive_name = str(source.get("archive_path", source.get("asset_name", "")))
        if archive_name:
            output[archive_name] = payload
    return output


def ordered_source_archives() -> list[Path]:
    """Preserve existing release order and append newly added source ZIPs."""
    archives_by_name = {path.name: path for path in DOWNLOADS.glob("*.zip")}
    if not TRACKED_RELEASE_MANIFEST.is_file():
        return sorted(archives_by_name.values())
    payload = json.loads(TRACKED_RELEASE_MANIFEST.read_text(encoding="utf-8"))
    existing_names = [row["upstream_archive"] for row in payload["assets"]]
    missing = [name for name in existing_names if name not in archives_by_name]
    if missing:
        raise SnapshotError(f"release source ZIPs are missing: {missing}")
    appended_names = sorted(set(archives_by_name) - set(existing_names))
    return [archives_by_name[name] for name in existing_names + appended_names]


def safe_member(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise SnapshotError(f"unsafe ZIP member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotError(f"unsafe ZIP member path: {name!r}")
    return path


def zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def inspect_source_archive(archive: Path) -> tuple[str, list[zipfile.ZipInfo]]:
    with zipfile.ZipFile(archive) as handle:
        bad = handle.testzip()
        if bad is not None:
            raise SnapshotError(f"ZIP CRC failure in {archive.name}: {bad}")
        files: list[zipfile.ZipInfo] = []
        level_candidates: list[PurePosixPath] = []
        for info in handle.infolist():
            member = safe_member(info.filename)
            if zip_is_symlink(info):
                raise SnapshotError(f"ZIP symlink is forbidden: {info.filename}")
            if info.is_dir():
                continue
            files.append(info)
            if member.name == "level.dat":
                level_candidates.append(member)
    if not level_candidates:
        raise SnapshotError(f"{archive.name} has no level.dat")
    shallowest = min(len(path.parts) for path in level_candidates)
    roots = {
        PurePosixPath(*path.parts[:-1]).as_posix()
        for path in level_candidates
        if len(path.parts) == shallowest
    }
    if len(roots) != 1 or "" in roots:
        raise SnapshotError(f"{archive.name} has ambiguous world roots: {sorted(roots)}")
    root = next(iter(roots))
    prefix = root.rstrip("/") + "/"
    selected = [info for info in files if info.filename.startswith(prefix)]
    return root, selected


def install_locked_heights_pack(world: Path) -> list[str]:
    datapacks = world / "datapacks"
    datapacks.mkdir(exist_ok=True)
    removed: list[str] = []
    for name in sorted(LEGACY_HEIGHT_PACKS):
        target = datapacks / name
        if not target.exists() and not target.is_symlink():
            continue
        removed.append(name)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copytree(HEIGHTS_PACK, datapacks / "heights")
    return removed


def release_world_rows(world: Path) -> list[dict[str, object]]:
    return [row for row in world_file_rows(world) if row["path"] != "session.lock"]


def write_deterministic_archive(
    world: Path,
    *,
    map_id: str,
    destination: Path,
) -> None:
    temporary = destination.with_suffix(".zip.building")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as handle:
            for row in release_world_rows(world):
                relative = str(row["path"])
                source = world / relative
                info = zipfile.ZipInfo(f"{map_id}/{relative}", FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.create_system = 3
                with source.open("rb") as input_handle, handle.open(info, "w") as output:
                    shutil.copyfileobj(input_handle, output, length=1024 * 1024)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def build(output: Path, *, replace: bool) -> dict[str, object]:
    if output.exists():
        if not replace:
            raise SnapshotError(f"output already exists (use --replace): {output}")
        shutil.rmtree(output)
    assets_dir = output / "assets"
    assets_dir.mkdir(parents=True)
    known_sources = existing_map_sources()
    archives = ordered_source_archives()

    entries: list[dict[str, object]] = []
    map_ids: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="mcbots-map-release-") as temporary_name:
        temporary_root = Path(temporary_name)
        for index, archive in enumerate(archives, 1):
            source_root, selected = inspect_source_archive(archive)
            known = known_sources.get(archive.name)
            map_id = str(known["map_id"]) if known else slugify(Path(source_root).name)
            if map_id in map_ids:
                raise SnapshotError(f"duplicate release map id: {map_id}")
            map_ids.add(map_id)

            prepared = CACHE_ROOT / map_id / "prepared/world"
            removed_packs: list[str] = []
            if prepared.is_dir():
                world = prepared
                preparation = "verified_prepared_snapshot"
            else:
                world = temporary_root / map_id
                extract_world(
                    archive,
                    world_root=source_root,
                    selected=selected,
                    destination=world,
                )
                removed_packs = install_locked_heights_pack(world)
                preparation = "source_clone_with_locked_heights_datapack"

            level = read_level_version(world / "level.dat")
            if level["version_name"] != "1.21.11" or level["data_version"] != 4671:
                raise SnapshotError(f"{map_id} is not a prepared 1.21.11 world: {level}")
            rows = release_world_rows(world)
            fingerprint = fingerprint_world_rows(rows)
            asset_name = f"navigation-1.21.11-{map_id}.zip"
            destination = assets_dir / asset_name
            write_deterministic_archive(world, map_id=map_id, destination=destination)
            with zipfile.ZipFile(destination) as handle:
                bad = handle.testzip()
                if bad is not None:
                    raise SnapshotError(f"release ZIP CRC failure: {asset_name}: {bad}")
            entries.append(
                {
                    "map_id": map_id,
                    "display_name": (
                        str(known["display_name"])
                        if known
                        else Path(source_root).name
                    ),
                    "environment": (
                        str(known.get("environment", "outdoor"))
                        if known
                        else "outdoor"
                    ),
                    "asset_name": asset_name,
                    "asset_bytes": destination.stat().st_size,
                    "asset_sha256": sha256_file(destination),
                    "world_root": map_id,
                    "minecraft_version": "1.21.11",
                    "data_version": 4671,
                    "world_fingerprint": fingerprint,
                    "preparation": preparation,
                    "removed_legacy_height_packs": removed_packs,
                    "upstream_archive": archive.name,
                    "upstream_archive_bytes": archive.stat().st_size,
                    "upstream_archive_sha256": sha256_file(archive),
                }
            )
            print(
                f"built {index:02d}/{len(archives)} {asset_name} "
                f"({destination.stat().st_size / 1024 / 1024:.1f} MiB)",
                flush=True,
            )

    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "navigation-map-release-manifest",
        "release_tag": RELEASE_TAG,
        "release_url": RELEASE_URL,
        "repository": "anonymous/source",
        "built_at_utc": utc_now(),
        "minecraft_version": "1.21.11",
        "data_version": 4671,
        "asset_count": len(entries),
        "assets": entries,
    }
    manifest = output / "navigation-maps-1.21.11-v1.json"
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    payload = build(args.output.resolve(), replace=args.replace)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "asset_count": payload["asset_count"],
                "release_tag": payload["release_tag"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

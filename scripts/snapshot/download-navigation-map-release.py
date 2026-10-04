#!/usr/bin/env python3
"""Download and verify the unified Minecraft 1.21.11 navigation maps."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.snapshots import (  # noqa: E402
    SnapshotError,
    sha256_file,
    validate_archive,
)


DEFAULT_MANIFEST = (
    REPO_ROOT
    / "eval/navigation/releases/navigation-maps-1.21.11-v1.json"
)
DEFAULT_OUTPUT = REPO_ROOT / "downloads/navigation-maps-1.21.11-v1"


def load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("artifact_kind") != "navigation-map-release-manifest":
        raise SnapshotError(f"not a navigation release manifest: {path}")
    if payload.get("minecraft_version") != "1.21.11":
        raise SnapshotError(f"manifest does not target Minecraft 1.21.11: {path}")
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) != payload.get("asset_count"):
        raise SnapshotError(f"manifest asset count is invalid: {path}")
    return payload


def verify_asset(path: Path, row: dict[str, object]) -> None:
    validate_archive(
        path,
        expected_sha256=str(row["asset_sha256"]),
        expected_bytes=int(row["asset_bytes"]),
        world_root=str(row["world_root"]),
    )


def download_asset(
    *,
    repository: str,
    release_tag: str,
    row: dict[str, object],
    output: Path,
    replace: bool,
) -> None:
    asset_name = str(row["asset_name"])
    destination = output / asset_name
    if destination.is_file() and not replace:
        verify_asset(destination, row)
        print(f"verified {row['map_id']}: {destination.name}", flush=True)
        return
    if repository == "anonymous/source":
        raise SnapshotError(
            "The source repository was redacted for anonymous review. "
            "Place the hash-matched archive in the output directory, or use a "
            "manifest pointing to an approved anonymous asset mirror."
        )
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcbots-map-download-", dir=output) as name:
        temporary_dir = Path(name)
        completed = subprocess.run(
            [
                "gh",
                "release",
                "download",
                release_tag,
                "--repo",
                repository,
                "--pattern",
                asset_name,
                "--dir",
                str(temporary_dir),
            ],
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise SnapshotError(
                f"failed to download {asset_name}: {completed.stderr.strip()}"
            )
        downloaded = temporary_dir / asset_name
        verify_asset(downloaded, row)
        destination.unlink(missing_ok=True)
        shutil.move(downloaded, destination)
    print(f"downloaded {row['map_id']}: {destination.name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--map", action="append", default=[])
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    payload = load_manifest(args.manifest.resolve())
    assets = [dict(row) for row in payload["assets"]]
    requested = set(args.map)
    known = {str(row["map_id"]) for row in assets}
    unknown = requested - known
    if unknown:
        raise SnapshotError(f"unknown map ids: {', '.join(sorted(unknown))}")
    selected = [row for row in assets if not requested or row["map_id"] in requested]
    for row in selected:
        download_asset(
            repository=str(payload["repository"]),
            release_tag=str(payload["release_tag"]),
            row=row,
            output=args.output.resolve(),
            replace=args.replace,
        )
    print(f"ready: {len(selected)} map archive(s) in {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

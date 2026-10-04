#!/usr/bin/env python3
"""Import ready-to-run release worlds into the navigation snapshot cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.schema import load_benchmark, load_map  # noqa: E402
from eval.navigation.snapshots import (  # noqa: E402
    DEFAULT_NAVIGATION_RELEASE_MANIFEST,
    import_release_snapshot,
    load_navigation_release_manifest,
    navigation_release_asset,
    verify_release_snapshot,
)


DEFAULT_DOWNLOADS = REPO_ROOT / "downloads/navigation-maps-1.21.11-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_NAVIGATION_RELEASE_MANIFEST,
    )
    parser.add_argument("--downloads-dir", type=Path, default=DEFAULT_DOWNLOADS)
    parser.add_argument("--benchmark", default="finalpool-navigation-v1")
    parser.add_argument(
        "--map",
        dest="map_ids",
        action="append",
        help="Map ID to import; repeat for multiple maps (default: benchmark maps).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing prepared caches with verified release worlds.",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify already imported release snapshots without extracting ZIPs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    downloads_dir = args.downloads_dir.expanduser().resolve()
    manifest = load_navigation_release_manifest(manifest_path)
    benchmark = load_benchmark(args.benchmark)
    map_ids = list(args.map_ids or benchmark["maps"])
    if len(map_ids) != len(set(map_ids)):
        raise SystemExit("duplicate --map selection is not allowed")
    known = {str(row["map_id"]) for row in manifest["assets"]}
    unknown = set(map_ids) - known
    if unknown:
        raise SystemExit(
            "release manifest has no assets for: " + ", ".join(sorted(unknown))
        )

    ready: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for index, map_id in enumerate(map_ids, 1):
        try:
            map_payload = load_map(map_id)
            asset = navigation_release_asset(manifest, map_id)
            if args.verify_only:
                result = verify_release_snapshot(map_payload)
                action = "verified"
            else:
                result = import_release_snapshot(
                    map_payload,
                    manifest_path=manifest_path,
                    downloads_dir=downloads_dir,
                    replace=args.replace,
                )
                action = "replaced" if args.replace else "imported_or_reused"
            row = {
                "map_id": map_id,
                "action": action,
                "asset_name": asset["asset_name"],
                "world_dir": result["world_dir"],
                "fingerprint": result["fingerprint"],
            }
            ready.append(row)
            print(
                f"[{index}/{len(map_ids)}] {action}: {map_id} "
                f"{result['fingerprint']['value']}",
                flush=True,
            )
        except Exception as error:
            errors.append(
                {
                    "map_id": map_id,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print(f"[{index}/{len(map_ids)}] failed: {map_id}: {error}", flush=True)

    summary = {
        "artifact_kind": "navigation-release-import-summary",
        "benchmark_id": benchmark["benchmark_id"],
        "release_tag": manifest["release_tag"],
        "mode": "verify_only" if args.verify_only else "import",
        "requested_maps": len(map_ids),
        "ready_maps": len(ready),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors and len(ready) == len(map_ids) else 3


if __name__ == "__main__":
    raise SystemExit(main())

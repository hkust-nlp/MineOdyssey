#!/usr/bin/env python3
"""Prepare and preflight immutable 1.21.11 navigation map snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.schema import load_benchmark, load_map  # noqa: E402
from eval.navigation.snapshots import prepare_snapshot, verify_snapshot  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map",
        dest="map_ids",
        action="append",
        help="Map ID to prepare; repeat for multiple maps (default: benchmark maps).",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=REPO_ROOT / "downloads",
        help="Directory containing original release ZIPs.",
    )
    parser.add_argument(
        "--download-missing",
        action="store_true",
        help="Download a missing private-release asset through authenticated gh.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--replace",
        action="store_true",
        help="Rebuild every selected cache after revalidating its source archive.",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip preparation and run only the final immutable-cache preflight.",
    )
    return parser.parse_args()


def _snapshot_summary(
    map_id: str,
    result: Mapping[str, Any],
    *,
    action: str,
) -> dict[str, object]:
    return {
        "map_id": map_id,
        "action": action,
        "level": result["level"],
        "source": {
            "world_dir": result["source"]["world_dir"],
            "fingerprint": result["source"]["fingerprint"],
        },
        "prepared": {
            "world_dir": result["world_dir"],
            "fingerprint": result["fingerprint"],
            "preparation_id": result["receipt"]["preparation_id"],
            "runtime_profile": result["receipt"].get("runtime_profile"),
            "replacement_manifest_sha256": result["receipt"].get(
                "replacement_manifest_sha256"
            ),
        },
    }


def _error_summary(map_id: str, error: Exception) -> dict[str, str]:
    return {
        "map_id": map_id,
        "error": f"{type(error).__name__}: {error}",
    }


def main() -> int:
    args = parse_args()
    benchmark = load_benchmark("finalpool-navigation-v1")
    map_ids = list(args.map_ids or benchmark["maps"])
    if len(map_ids) != len(set(map_ids)):
        raise SystemExit("duplicate --map selection is not allowed")

    downloads_dir = args.downloads_dir.resolve()
    preparation: list[dict[str, object]] = []
    preparation_errors: list[dict[str, str]] = []

    if not args.verify_only:
        for map_id in map_ids:
            map_payload = load_map(map_id)
            try:
                if args.replace:
                    result = prepare_snapshot(
                        map_payload,
                        downloads_dir=downloads_dir,
                        replace=True,
                        download_missing=args.download_missing,
                    )
                    action = "replaced"
                else:
                    try:
                        result = verify_snapshot(map_payload)
                        action = "reused"
                    except Exception:
                        result = prepare_snapshot(
                            map_payload,
                            downloads_dir=downloads_dir,
                            replace=True,
                            download_missing=args.download_missing,
                        )
                        action = "repaired"
                result = verify_snapshot(map_payload)
                preparation.append(
                    _snapshot_summary(map_id, result, action=action)
                )
            except Exception as error:
                preparation_errors.append(_error_summary(map_id, error))

    preflight: list[dict[str, object]] = []
    ready_maps = 0
    for map_id in map_ids:
        try:
            result = verify_snapshot(load_map(map_id))
            preflight.append(
                _snapshot_summary(map_id, result, action="verified")
            )
            ready_maps += 1
        except Exception as error:
            preflight.append(
                {
                    "map_id": map_id,
                    "ready": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    all_ready = ready_maps == len(map_ids)
    output = {
        "benchmark_id": benchmark["benchmark_id"],
        "mode": "verify_only" if args.verify_only else "prepare_and_preflight",
        "requested_maps": len(map_ids),
        "prepared_or_reused_maps": len(preparation),
        "preparation_errors": preparation_errors,
        "preflight": {
            "ready": all_ready,
            "ready_maps": ready_maps,
            "total_maps": len(map_ids),
            "maps": preflight,
        },
        "preparation": preparation,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all_ready else 3


if __name__ == "__main__":
    raise SystemExit(main())

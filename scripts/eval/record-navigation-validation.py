#!/usr/bin/env python3
"""Record a fingerprint-bound manual navigation validation receipt."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.navigation.schema import (  # noqa: E402
    atomic_write_json,
    digest_json,
    find_task,
    load_benchmark,
    load_map,
    load_references,
    load_setting,
    reference_digest,
    task_digest,
    validation_receipt_path,
)
from eval.navigation.snapshots import verify_snapshot  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--status", choices=("verified", "rejected"), required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing receipt for this task.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reviewer = args.reviewer.strip()
    evidence = args.evidence.strip()
    if not reviewer:
        raise SystemExit("--reviewer must not be empty")
    if not evidence:
        raise SystemExit("--evidence must not be empty")
    benchmark = load_benchmark("finalpool-navigation-v1")
    map_id, task = find_task(args.task, benchmark["maps"])
    map_payload = load_map(map_id)
    snapshot = verify_snapshot(map_payload)
    reference = load_references(map_id)[task["id"]]
    if reference["status"] != "reachable":
        raise SystemExit(
            f"{task['id']}: manual receipt requires a reachable reference, "
            f"got {reference['status']!r}"
        )
    setting = load_setting(benchmark["setting_id"])
    path = validation_receipt_path(map_id, task["id"])
    if path.exists() and not args.replace:
        raise SystemExit(f"receipt already exists: {path}; pass --replace to supersede it")
    receipt = {
        "schema_version": 1,
        "artifact_kind": "navigation-manual-validation-receipt",
        "status": args.status,
        "map_id": map_id,
        "task_id": task["id"],
        "map_fingerprint": snapshot["fingerprint"]["value"],
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
        "profile_id": benchmark["profile_id"],
        "setting_id": benchmark["setting_id"],
        "reviewer": reviewer,
        "reviewed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "evidence": evidence,
        "rules": {
            "gamemode": setting["runtime"]["gamemode"],
            "arrival": setting["arrival"],
            "completion": setting["completion"],
        },
    }
    atomic_write_json(path, receipt)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{utc_ts()}][snapshot-groups] {msg}", flush=True)


def load_json_obj(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def task_slug(task_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", task_name.strip())
    slug = slug.strip("._-")
    return slug or "task"


def version_slug(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", version).strip("_") or "version"


def normalize_positions_from_group(group: Dict[str, Any]) -> Tuple[List[Tuple[int, int, int]], int]:
    coverage = group.get("coverage")
    if not isinstance(coverage, dict):
        return [], 0
    raw_points = coverage.get("points")
    if not isinstance(raw_points, list):
        return [], 0
    positions: List[Tuple[int, int, int]] = []
    max_radius = 0
    seen: set[Tuple[int, int, int]] = set()
    for item in raw_points:
        if not isinstance(item, dict):
            continue
        pos = item.get("position")
        if not isinstance(pos, list) or len(pos) < 3:
            continue
        try:
            xyz = (int(pos[0]), int(pos[1]), int(pos[2]))
        except Exception:
            continue
        if xyz not in seen:
            seen.add(xyz)
            positions.append(xyz)
        try:
            r = int(item.get("pregen_radius_chunks", 0))
        except Exception:
            r = 0
        if r > max_radius:
            max_radius = r
    return positions, max_radius


def build_child_command(
    *,
    python_exe: str,
    project_root: Path,
    group: Dict[str, Any],
    args: argparse.Namespace,
) -> List[str]:
    group_id = str(group.get("group_id", "group"))
    group_key = group.get("group_key")
    if not isinstance(group_key, dict):
        raise RuntimeError(f"group {group_id} missing group_key")
    tasks = group.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError(f"group {group_id} has no tasks[]")
    rep = tasks[0]
    if not isinstance(rep, dict):
        raise RuntimeError(f"group {group_id} tasks[0] invalid")

    rep_task_name = str(rep.get("task_name"))
    rep_config_path = rep.get("config_path")
    if not isinstance(rep_config_path, str) or not rep_config_path:
        raise RuntimeError(f"group {group_id} representative task missing config_path")
    seed = group_key.get("seed")
    if seed is None:
        raise RuntimeError(f"group {group_id} missing group_key.seed")
    target_dimension = str(group_key.get("dimension", "overworld"))

    positions, group_max_radius = normalize_positions_from_group(group)
    radius = args.target_pregen_radius_chunks if args.target_pregen_radius_chunks is not None else group_max_radius

    cmd = [
        python_exe,
        "scripts/snapshot/build-seed-snapshot.py",
        "--task-config",
        rep_config_path,
        "--task-name",
        rep_task_name,
        "--seed",
        str(int(seed)),
        "--snapshot-slug",
        group_id,
        "--raw-version",
        args.raw_version,
        "--upgraded-version",
        args.upgraded_version,
        "--java-legacy",
        args.java_legacy,
        "--java-modern",
        args.java_modern,
        "--legacy-memory",
        args.legacy_memory,
        "--modern-memory",
        args.modern_memory,
        "--startup-timeout-sec",
        str(args.startup_timeout_sec),
        "--target-pregen-radius-chunks",
        str(int(max(0, radius))),
        "--target-dimension",
        target_dimension,
        "--snapshot-root",
        str(args.snapshot_root),
    ]
    if args.cache_root:
        cmd += ["--cache-root", str(args.cache_root)]
    if args.work_root:
        cmd += ["--work-root", str(args.work_root)]
    if args.force:
        cmd.append("--force")
    if args.keep_work:
        cmd.append("--keep-work")
    if args.child_dry_run:
        cmd.append("--dry-run")
    for x, y, z in positions:
        # Use --opt=value so negative coordinates are never parsed as a new flag.
        cmd.append(f"--target-position={x},{y},{z}")
    return cmd


def expected_snapshot_paths_for_group(
    *,
    group: Dict[str, Any],
    project_root: Path,
    snapshot_root: Path,
    raw_version: str,
    upgraded_version: str,
) -> Dict[str, Path]:
    group_id = str(group.get("group_id", "group"))
    group_key = group.get("group_key")
    if not isinstance(group_key, dict):
        raise RuntimeError(f"group {group_id} missing group_key")
    seed = group_key.get("seed")
    if seed is None:
        raise RuntimeError(f"group {group_id} missing group_key.seed")
    slug = task_slug(group_id)

    root = snapshot_root if snapshot_root.is_absolute() else (project_root / snapshot_root)
    raw_parent = root / f"seed_raw_{version_slug(raw_version)}" / slug / str(int(seed))
    upgraded_parent = root / f"seed_upgraded_{version_slug(upgraded_version)}" / slug / str(int(seed))
    return {
        "raw_parent": raw_parent,
        "raw_world": raw_parent / "world",
        "raw_meta": raw_parent / "snapshot_meta.json",
        "upgraded_parent": upgraded_parent,
        "upgraded_world": upgraded_parent / "world",
        "upgraded_meta": upgraded_parent / "snapshot_meta.json",
    }


def should_skip_group_existing(
    *,
    paths: Dict[str, Path],
    world_kind: str,
    require_meta: bool,
) -> bool:
    def _check(prefix: str) -> bool:
        world_ok = paths[f"{prefix}_world"].exists()
        if not world_ok:
            return False
        if not require_meta:
            return True
        return paths[f"{prefix}_meta"].exists()

    if world_kind == "raw":
        return _check("raw")
    if world_kind == "upgraded":
        return _check("upgraded")
    if world_kind == "both":
        return _check("raw") and _check("upgraded")
    raise RuntimeError(f"Unsupported world_kind: {world_kind}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build shared seed snapshots from snapshot-group plan JSON.")
    p.add_argument("--plan", type=Path, required=True, help="Path to snapshot group plan JSON.")
    p.add_argument("--group-id", action="append", default=[], help="Run only selected group_id(s).")
    p.add_argument("--group-id-regex", default="", help="Regex filter on group_id after exact --group-id filtering.")
    p.add_argument("--family", action="append", default=[], help="Run only groups containing these family names.")
    p.add_argument("--dimension", action="append", default=[], help="Run only groups with these dimensions.")
    p.add_argument("--max-groups", type=int, default=0, help="Limit selected groups (0 = all).")
    p.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root used to run child script.",
    )
    p.add_argument("--python", dest="python_exe", default=sys.executable)
    p.add_argument("--raw-version", default="1.16.5")
    p.add_argument("--upgraded-version", default="1.21.1")
    p.add_argument("--java-legacy", default="java")
    p.add_argument("--java-modern", default="java")
    p.add_argument("--legacy-memory", default="2G")
    p.add_argument("--modern-memory", default="2G")
    p.add_argument("--startup-timeout-sec", type=float, default=600.0)
    p.add_argument("--target-pregen-radius-chunks", type=int, default=None, help="Override all group radii.")
    p.add_argument("--snapshot-root", type=Path, default=Path("eval/snapshots"))
    p.add_argument("--cache-root", type=Path, default=None)
    p.add_argument("--work-root", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip groups whose snapshot outputs already exist under --snapshot-root.",
    )
    p.add_argument(
        "--skip-existing-world-kind",
        choices=["raw", "upgraded", "both"],
        default="upgraded",
        help="Which snapshot output(s) to check for --skip-existing.",
    )
    p.add_argument(
        "--skip-existing-require-meta",
        action="store_true",
        help="When used with --skip-existing, also require snapshot_meta.json to consider a group already built.",
    )
    p.add_argument("--keep-work", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Print child commands but do not execute.")
    p.add_argument(
        "--child-dry-run",
        action="store_true",
        help="Execute child script in --dry-run mode (useful to validate arguments end-to-end).",
    )
    p.add_argument(
        "--post-validate-coverage",
        action="store_true",
        help="After build, run scripts/snapshot/validate-snapshot-group-coverage.py on the selected groups.",
    )
    p.add_argument(
        "--post-validate-world-kind",
        choices=["raw", "upgraded", "both"],
        default="upgraded",
        help="World kind passed to post coverage validator.",
    )
    p.add_argument(
        "--post-validate-check-meta",
        action="store_true",
        help="Also check snapshot_meta.json in post coverage validation.",
    )
    p.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep building remaining groups after a child command failure.",
    )
    p.add_argument("--max-failures", type=int, default=0, help="Stop after N failures when --continue-on-error is used (0 = no limit).")
    p.add_argument("--summary-json", type=Path, default=None, help="Write execution summary JSON.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    plan_path = args.plan.resolve()
    summary_path = args.summary_json
    if summary_path is not None:
        if not summary_path.is_absolute():
            summary_path = (project_root / summary_path).resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
    plan = load_json_obj(plan_path)
    groups = plan.get("groups")
    if not isinstance(groups, list):
        raise RuntimeError(f"Invalid plan file (missing groups[]): {plan_path}")

    selected = set(args.group_id) if args.group_id else None
    selected_families = set(args.family) if args.family else None
    selected_dimensions = {d.strip().lower() for d in args.dimension if str(d).strip()} if args.dimension else None
    group_id_re = re.compile(args.group_id_regex) if args.group_id_regex else None
    run_groups: List[Dict[str, Any]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id", ""))
        if selected and gid not in selected:
            continue
        if group_id_re and not group_id_re.search(gid):
            continue
        if selected_dimensions:
            gk = g.get("group_key")
            if not isinstance(gk, dict):
                continue
            dim = str(gk.get("dimension", "overworld")).strip().lower() or "overworld"
            if dim not in selected_dimensions:
                continue
        if selected_families:
            stats = g.get("stats")
            fams: List[str] = []
            if isinstance(stats, dict) and isinstance(stats.get("families"), list):
                fams = [str(x) for x in stats.get("families", [])]
            elif isinstance(g.get("tasks"), list):
                fams = [str(t.get("family")) for t in g.get("tasks", []) if isinstance(t, dict) and t.get("family")]
            if not any(f in selected_families for f in fams):
                continue
        run_groups.append(g)

    if args.max_groups > 0:
        run_groups = run_groups[: args.max_groups]

    if selected:
        missing = sorted(selected - {str(g.get("group_id", "")) for g in run_groups})
        if missing:
            raise RuntimeError(f"group_id not found in plan: {missing}")

    log(f"plan={plan_path}")
    log(f"groups_selected={len(run_groups)} total_groups={len(groups)}")
    if selected_families:
        log(f"family_filter={sorted(selected_families)}")
    if selected_dimensions:
        log(f"dimension_filter={sorted(selected_dimensions)}")
    if group_id_re:
        log(f"group_id_regex={args.group_id_regex}")

    execution_records: List[Dict[str, Any]] = []
    built_count = 0
    skipped_count = 0
    failed_count = 0
    post_validate_status = "not_requested"
    post_validate_returncode: Optional[int] = None
    post_validate_group_count = 0
    post_validate_skipped_groups = 0

    for idx, group in enumerate(run_groups, start=1):
        gid = str(group.get("group_id", "group"))
        group_key = group.get("group_key") if isinstance(group.get("group_key"), dict) else {}
        paths = expected_snapshot_paths_for_group(
            group=group,
            project_root=project_root,
            snapshot_root=args.snapshot_root,
            raw_version=args.raw_version,
            upgraded_version=args.upgraded_version,
        )
        if args.skip_existing and should_skip_group_existing(
            paths=paths,
            world_kind=args.skip_existing_world_kind,
            require_meta=args.skip_existing_require_meta,
        ):
            skipped_count += 1
            log(
                f"[{idx}/{len(run_groups)}] group_id={gid} "
                f"skip=existing ({args.skip_existing_world_kind})"
            )
            execution_records.append(
                {
                    "group_id": gid,
                    "status": "skipped_existing",
                    "group_key": group_key,
                    "raw_world": str(paths["raw_world"]),
                    "upgraded_world": str(paths["upgraded_world"]),
                }
            )
            continue

        cmd = build_child_command(python_exe=args.python_exe, project_root=project_root, group=group, args=args)
        log(f"[{idx}/{len(run_groups)}] group_id={gid}")
        log("cmd: " + " ".join(subprocess.list2cmdline([part]) for part in cmd))
        if args.dry_run:
            execution_records.append(
                {
                    "group_id": gid,
                    "status": "planned",
                    "group_key": group_key,
                    "raw_world": str(paths["raw_world"]),
                    "upgraded_world": str(paths["upgraded_world"]),
                    "cmd": cmd,
                }
            )
            continue
        try:
            subprocess.run(cmd, cwd=str(project_root), check=True)
            built_count += 1
            execution_records.append(
                {
                    "group_id": gid,
                    "status": "built",
                    "group_key": group_key,
                    "raw_world": str(paths["raw_world"]),
                    "upgraded_world": str(paths["upgraded_world"]),
                }
            )
        except subprocess.CalledProcessError as e:
            failed_count += 1
            log(f"group failed: group_id={gid} rc={e.returncode}")
            execution_records.append(
                {
                    "group_id": gid,
                    "status": "failed",
                    "returncode": int(e.returncode),
                    "group_key": group_key,
                    "raw_world": str(paths["raw_world"]),
                    "upgraded_world": str(paths["upgraded_world"]),
                    "cmd": cmd,
                }
            )
            if not args.continue_on_error:
                if summary_path is not None:
                    summary = {
                        "generated_at_utc": utc_ts(),
                        "plan": str(plan_path),
                        "selected_groups": len(run_groups),
                        "executed_groups": len(execution_records),
                        "built": built_count,
                        "skipped_existing": skipped_count,
                        "failed": failed_count,
                        "aborted_early": True,
                        "dry_run": bool(args.dry_run),
                        "records": execution_records,
                    }
                    with summary_path.open("w", encoding="utf-8") as f:
                        json.dump(summary, f, indent=2, ensure_ascii=True)
                        f.write("\n")
                    log(f"summary saved: {summary_path}")
                raise
            if args.max_failures > 0 and failed_count >= args.max_failures:
                log(f"max failures reached ({args.max_failures}); stopping early")
                break

    if args.post_validate_coverage:
        post_validate_status = "dry_run_planned" if args.dry_run else "pending"
        validate_group_ids: List[str] = []
        if args.dry_run:
            for group in run_groups:
                gid = str(group.get("group_id", ""))
                if gid:
                    validate_group_ids.append(gid)
        else:
            seen_validate_ids: set[str] = set()
            for rec in execution_records:
                if str(rec.get("status", "")) not in {"built", "skipped_existing"}:
                    continue
                gid = str(rec.get("group_id", ""))
                if not gid or gid in seen_validate_ids:
                    continue
                seen_validate_ids.add(gid)
                validate_group_ids.append(gid)
            post_validate_skipped_groups = max(0, len(run_groups) - len(validate_group_ids))
            if post_validate_skipped_groups > 0:
                log(
                    "post-validate skip groups without built/existing snapshots: "
                    f"{post_validate_skipped_groups}"
                )

        post_validate_group_count = len(validate_group_ids)
        validate_cmd = [
            args.python_exe,
            "scripts/snapshot/validate-snapshot-group-coverage.py",
            "--plan",
            str(plan_path),
            "--snapshot-root",
            str(args.snapshot_root),
            "--world-kind",
            args.post_validate_world_kind,
        ]
        if args.post_validate_check_meta:
            validate_cmd.append("--check-meta")
        for gid in validate_group_ids:
            validate_cmd += ["--group-id", gid]

        if not args.dry_run and not validate_group_ids:
            post_validate_status = "skipped_no_eligible_groups"
            log("post-validate skipped: no built/skipped-existing groups to validate")
        else:
            log("post-validate cmd: " + " ".join(subprocess.list2cmdline([part]) for part in validate_cmd))
            if not args.dry_run:
                try:
                    subprocess.run(validate_cmd, cwd=str(project_root), check=True)
                    post_validate_status = "ok"
                except subprocess.CalledProcessError as e:
                    post_validate_status = "failed"
                    post_validate_returncode = int(e.returncode)
                    log(f"post-validate failed: rc={e.returncode}")
            else:
                post_validate_status = "dry_run_planned"

    if summary_path is not None:
        summary = {
            "generated_at_utc": utc_ts(),
            "plan": str(plan_path),
            "selected_groups": len(run_groups),
            "executed_groups": len(execution_records),
            "built": built_count,
            "skipped_existing": skipped_count,
            "failed": failed_count,
            "aborted_early": len(execution_records) < len(run_groups),
            "dry_run": bool(args.dry_run),
            "post_validate": {
                "requested": bool(args.post_validate_coverage),
                "status": post_validate_status,
                "returncode": post_validate_returncode,
                "world_kind": args.post_validate_world_kind if args.post_validate_coverage else None,
                "check_meta": bool(args.post_validate_check_meta) if args.post_validate_coverage else None,
                "group_count": post_validate_group_count,
                "skipped_groups": post_validate_skipped_groups,
            },
            "records": execution_records,
        }
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=True)
            f.write("\n")
        log(f"summary saved: {summary_path}")

    log("done")
    if failed_count > 0 or post_validate_status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

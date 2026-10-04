#!/usr/bin/env python3
"""Replay the frame filter on a screenshots directory (or an agent record dir)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.frame_filter import (  # noqa: E402
    ExactFrameDeduper,
    FrameFilterConfig,
    summarize_frame_filter_telemetry_records,
)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _looks_like_record_dir(path: Path) -> bool:
    return path.is_dir() and (path / "screenshots").is_dir()


def _resolve_input_paths(path_str: str) -> tuple[Path, Path, Optional[Path]]:
    path = Path(path_str).expanduser()
    if _looks_like_record_dir(path):
        telemetry = path / "frame_filter_telemetry.jsonl"
        return path, path / "screenshots", (telemetry if telemetry.is_file() else None)
    if path.is_dir():
        return path, path, None
    raise FileNotFoundError(f"Directory not found: {path}")


def _extract_screenshot_index(path: Path) -> Optional[int]:
    m = re.search(r"(\d+)", path.stem)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _iter_screenshot_files(screenshots_dir: Path) -> list[Path]:
    files = [p for p in screenshots_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
    files.sort(
        key=lambda p: (
            _extract_screenshot_index(p) is None,
            _extract_screenshot_index(p) if _extract_screenshot_index(p) is not None else 0,
            p.name,
        )
    )
    return files


def _load_trigger_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("type") != "screenshot_force_keep_trigger":
                continue
            ts = raw.get("timestamp")
            try:
                raw["timestamp"] = float(ts)
            except Exception:
                continue
            records.append(raw)
    records.sort(key=lambda r: float(r.get("timestamp", 0.0)))
    return records


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _make_timestamp(
    file_path: Path,
    idx: int,
    *,
    mode: str,
    start_ts: float,
    interval_sec: float,
) -> float:
    if mode == "mtime":
        try:
            return float(file_path.stat().st_mtime)
        except Exception:
            return start_ts + idx * interval_sec
    return start_ts + idx * interval_sec


def _format_pct(value: float) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "n/a"


def _print_text_summary(payload: dict) -> None:
    print(f"input_path: {payload.get('input_path')}")
    print(f"screenshots_dir: {payload.get('screenshots_dir')}")
    print(f"screenshot_files_found: {payload.get('screenshot_files_found', 0)}")
    print(f"timestamp_source: {payload.get('timestamp_source')}")
    print(f"trigger_telemetry: {payload.get('trigger_telemetry') or '<none>'}")
    print(f"trigger_replay_enabled: {payload.get('trigger_replay_enabled')}")

    summary = payload.get("summary") or {}
    print(f"screenshot_frames_total: {summary.get('screenshot_frames_total', 0)}")
    print(f"screenshot_frames_kept: {summary.get('screenshot_frames_kept', 0)}")
    print(f"screenshot_frames_dropped: {summary.get('screenshot_frames_dropped', 0)}")
    print(f"drop_ratio: {_format_pct(summary.get('drop_ratio', 0.0))}")

    reasons = summary.get("reasons") or {}
    if reasons:
        print("reasons:")
        for key, value in sorted(reasons.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    budget_status = summary.get("budget_status") or {}
    if budget_status:
        print("budget_status:")
        for key, value in sorted(budget_status.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    approx_status = summary.get("approx_status") or {}
    if approx_status:
        print("approx_status:")
        for key, value in sorted(approx_status.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")


def _build_config(args: argparse.Namespace) -> FrameFilterConfig:
    return FrameFilterConfig(
        enabled=not args.disable_dedup,
        force_keep_after_event_count=max(int(args.force_keep_after_event), 0),
        enable_approx_dedup=bool(args.enable_approx_dedup),
        approx_signature_width=max(int(args.approx_sig_width), 1),
        approx_signature_height=max(int(args.approx_sig_height), 1),
        approx_diff_threshold=float(args.approx_diff_threshold),
        approx_peak_tile_guard_threshold=float(args.approx_peak_tile_guard_threshold),
        approx_tile_cols=max(int(args.approx_tile_cols), 1),
        approx_tile_rows=max(int(args.approx_tile_rows), 1),
        enable_approx_center_roi_weighting=bool(args.enable_approx_center_roi_weighting),
        approx_center_roi_width_ratio=float(args.approx_center_roi_width_ratio),
        approx_center_roi_height_ratio=float(args.approx_center_roi_height_ratio),
        approx_center_roi_weight=float(args.approx_center_roi_weight),
        enable_adaptive_budget=bool(args.enable_adaptive_budget),
        adaptive_idle_min_keep_interval_sec=float(args.idle_min_keep_interval_sec),
        adaptive_active_min_keep_interval_sec=float(args.active_min_keep_interval_sec),
        adaptive_event_boost_window_sec=float(args.event_boost_window_sec),
        adaptive_bypass_local_peak_change=not args.disable_bypass_local_peak,
        adaptive_boost_on_visual_change=not args.disable_visual_boost,
    )


def _replay(
    screenshot_files: list[Path],
    *,
    cfg: FrameFilterConfig,
    timestamp_source: str,
    start_ts: float,
    index_interval_sec: float,
    trigger_records: list[dict],
    use_trigger_replay: bool,
) -> list[dict]:
    deduper = ExactFrameDeduper(cfg)
    replay_records: list[dict] = []

    trigger_idx = 0
    for idx, file_path in enumerate(screenshot_files):
        ts = _make_timestamp(
            file_path,
            idx,
            mode=timestamp_source,
            start_ts=start_ts,
            interval_sec=index_interval_sec,
        )

        if use_trigger_replay:
            while trigger_idx < len(trigger_records) and float(trigger_records[trigger_idx]["timestamp"]) <= ts:
                trig = dict(trigger_records[trigger_idx])
                source = str(trig.get("source") or "event")
                deduper.notify_event(source, float(trig["timestamp"]))
                replay_records.append(trig)
                trigger_idx += 1

        frame_bytes = file_path.read_bytes()
        decision = deduper.decide(frame_bytes, ts)
        replay_records.append(
            {
                "type": "screenshot_frame",
                "timestamp": ts,
                "screenshot_index": _extract_screenshot_index(file_path) or (idx + 1),
                "kept": decision.kept,
                "reason": decision.reason,
                "force_event": decision.force_event,
                "sha1": decision.sha1,
                "size_bytes": len(frame_bytes),
                "path": str(file_path),
                "metrics": decision.metrics or {},
                "stats": decision.stats or {},
            }
        )

    return replay_records


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay frame filter logic on screenshots to tune thresholds offline.",
    )
    ap.add_argument("path", help="Agent record dir (contains screenshots/) or screenshots dir.")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of text summary.")
    ap.add_argument(
        "--timestamp-source",
        choices=["mtime", "index"],
        default="mtime",
        help="Use file mtime or synthetic index-based timestamps.",
    )
    ap.add_argument("--start-ts", type=float, default=0.0, help="Synthetic timestamp start (for --timestamp-source index).")
    ap.add_argument("--index-interval-sec", type=float, default=1.0, help="Synthetic timestamp interval (index mode).")
    ap.add_argument("--disable-dedup", action="store_true", help="Disable dedup (for baseline replay).")
    ap.add_argument("--force-keep-after-event", type=int, default=1)
    ap.add_argument("--enable-approx-dedup", action="store_true")
    ap.add_argument("--approx-sig-width", type=int, default=32)
    ap.add_argument("--approx-sig-height", type=int, default=18)
    ap.add_argument("--approx-tile-cols", type=int, default=4)
    ap.add_argument("--approx-tile-rows", type=int, default=3)
    ap.add_argument("--enable-approx-center-roi-weighting", action="store_true")
    ap.add_argument("--approx-center-roi-width-ratio", type=float, default=0.5)
    ap.add_argument("--approx-center-roi-height-ratio", type=float, default=0.5)
    ap.add_argument("--approx-center-roi-weight", type=float, default=2.0)
    ap.add_argument("--approx-diff-threshold", type=float, default=1.5)
    ap.add_argument("--approx-peak-tile-guard-threshold", type=float, default=6.0)
    ap.add_argument("--enable-adaptive-budget", action="store_true")
    ap.add_argument("--idle-min-keep-interval-sec", type=float, default=4.0)
    ap.add_argument("--active-min-keep-interval-sec", type=float, default=1.0)
    ap.add_argument("--event-boost-window-sec", type=float, default=4.0)
    ap.add_argument("--disable-bypass-local-peak", action="store_true")
    ap.add_argument("--disable-visual-boost", action="store_true")
    ap.add_argument(
        "--trigger-telemetry",
        default="auto",
        help="Path to telemetry JSONL containing screenshot_force_keep_trigger records; 'auto' uses <record>/frame_filter_telemetry.jsonl if present; 'none' disables.",
    )
    args = ap.parse_args()

    input_dir, screenshots_dir, auto_telemetry = _resolve_input_paths(args.path)
    if not screenshots_dir.is_dir():
        print(f"screenshots dir not found: {screenshots_dir}", file=sys.stderr)
        return 1

    screenshot_files = _iter_screenshot_files(screenshots_dir)
    if not screenshot_files:
        print(f"no screenshots found in: {screenshots_dir}", file=sys.stderr)
        return 1

    trigger_path: Optional[Path]
    if args.trigger_telemetry == "none":
        trigger_path = None
    elif args.trigger_telemetry == "auto":
        trigger_path = auto_telemetry
    else:
        trigger_path = Path(args.trigger_telemetry).expanduser()

    trigger_records: list[dict] = []
    use_trigger_replay = False
    if trigger_path and trigger_path.is_file():
        trigger_records = _load_trigger_records(trigger_path)
        use_trigger_replay = True

    cfg = _build_config(args)
    replay_records = _replay(
        screenshot_files,
        cfg=cfg,
        timestamp_source=args.timestamp_source,
        start_ts=_coerce_float(args.start_ts, 0.0),
        index_interval_sec=max(_coerce_float(args.index_interval_sec, 1.0), 0.0),
        trigger_records=trigger_records,
        use_trigger_replay=use_trigger_replay,
    )
    summary = summarize_frame_filter_telemetry_records(replay_records)

    result = {
        "input_path": str(input_dir),
        "screenshots_dir": str(screenshots_dir),
        "screenshot_files_found": len(screenshot_files),
        "timestamp_source": args.timestamp_source,
        "trigger_telemetry": str(trigger_path) if trigger_path else None,
        "trigger_replay_enabled": use_trigger_replay,
        "frame_filter_config": asdict(cfg),
        "summary": summary,
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_text_summary(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

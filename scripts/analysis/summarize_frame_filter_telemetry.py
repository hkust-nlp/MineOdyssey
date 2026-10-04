#!/usr/bin/env python3
"""Summarize frame filter telemetry JSONL produced by agent/agent.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.frame_filter import summarize_frame_filter_telemetry_file  # noqa: E402


def _resolve_telemetry_path(input_path: str) -> Path:
    path = Path(input_path).expanduser()
    if path.is_dir():
        candidate = path / "frame_filter_telemetry.jsonl"
        return candidate
    return path


def _format_pct(value: float) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "n/a"


def _print_text_summary(result: dict) -> None:
    print(f"telemetry_file: {result.get('telemetry_file')}")
    if not result.get("exists"):
        print(f"exists: false ({result.get('error', 'unknown_error')})")
        return

    if "parse_errors" in result:
        print(f"parse_errors: {result.get('parse_errors')}")

    summary = result.get("summary")
    if not isinstance(summary, dict):
        print("summary: <missing>")
        return

    print(f"screenshot_frames_total: {summary.get('screenshot_frames_total', 0)}")
    print(f"screenshot_frames_kept: {summary.get('screenshot_frames_kept', 0)}")
    print(f"screenshot_frames_dropped: {summary.get('screenshot_frames_dropped', 0)}")
    print(f"drop_ratio: {_format_pct(summary.get('drop_ratio', 0.0))}")
    print(f"bytes_total: {summary.get('bytes_total', 0)}")
    print(f"bytes_dropped: {summary.get('bytes_dropped', 0)}")
    print(f"bytes_kept: {summary.get('bytes_kept', 0)}")

    reasons = summary.get("reasons") or {}
    if reasons:
        print("reasons:")
        for key, value in sorted(reasons.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    dropped_reasons = summary.get("dropped_reasons") or {}
    if dropped_reasons:
        print("dropped_reasons:")
        for key, value in sorted(dropped_reasons.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    trigger_sources = summary.get("trigger_sources") or {}
    if trigger_sources:
        print("trigger_sources:")
        for key, value in sorted(trigger_sources.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    approx_status = summary.get("approx_status") or {}
    if approx_status:
        print("approx_status:")
        for key, value in sorted(approx_status.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    budget_stage = summary.get("budget_stage") or {}
    if budget_stage:
        print("budget_stage:")
        for key, value in sorted(budget_stage.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    budget_status = summary.get("budget_status") or {}
    if budget_status:
        print("budget_status:")
        for key, value in sorted(budget_status.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")

    budget_active_source = summary.get("budget_active_source") or {}
    if budget_active_source:
        print("budget_active_source:")
        for key, value in sorted(budget_active_source.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize frame filter telemetry JSONL or a record directory containing it.",
    )
    parser.add_argument(
        "path",
        help="Path to frame_filter_telemetry.jsonl or the agent record directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON summary instead of text view.",
    )
    args = parser.parse_args()

    telemetry_path = _resolve_telemetry_path(args.path)
    result = summarize_frame_filter_telemetry_file(telemetry_path)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_text_summary(result)

    return 0 if result.get("exists") else 1


if __name__ == "__main__":
    raise SystemExit(main())

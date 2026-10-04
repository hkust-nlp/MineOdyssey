"""Lightweight screenshot dedup for the agent observe loop."""

from __future__ import annotations

import json
import hashlib
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None


@dataclass
class FrameFilterConfig:
    """Config for screenshot dedup (exact + optional approximate stage)."""

    enabled: bool = True
    force_keep_after_event_count: int = 1
    enable_approx_dedup: bool = True
    approx_signature_width: int = 24
    approx_signature_height: int = 14
    approx_diff_threshold: float = 3.0
    approx_peak_tile_guard_threshold: float = 10.0
    approx_tile_cols: int = 4
    approx_tile_rows: int = 3
    enable_approx_center_roi_weighting: bool = True
    approx_center_roi_width_ratio: float = 0.60
    approx_center_roi_height_ratio: float = 0.60
    approx_center_roi_weight: float = 2.8
    enable_adaptive_budget: bool = False
    adaptive_idle_min_keep_interval_sec: float = 4.0
    adaptive_active_min_keep_interval_sec: float = 1.0
    adaptive_event_boost_window_sec: float = 4.0
    adaptive_bypass_local_peak_change: bool = True
    adaptive_boost_on_visual_change: bool = True
    keepalive_max_interval_sec: float = 8.0


@dataclass
class FrameDecision:
    """Decision returned by the frame filter for one screenshot."""

    kept: bool
    reason: str
    sha1: str
    force_event: Optional[str] = None
    force_keep_budget_remaining: int = 0
    metrics: Optional[Dict[str, float | str | bool]] = None
    stats: Optional[Dict[str, int]] = None


class ExactFrameDeduper:
    """Screenshot deduper with exact match + optional CPU-friendly approximate diff."""

    def __init__(self, config: Optional[FrameFilterConfig] = None):
        self.config = config or FrameFilterConfig()
        self._last_kept_sha1: Optional[str] = None
        self._last_kept_signature: Optional[tuple[int, ...]] = None
        self._last_kept_timestamp: Optional[float] = None
        self._force_keep_budget = 0
        self._last_force_event: Optional[str] = None
        self._last_event_timestamp: Optional[float] = None
        self._last_visual_activity_timestamp: Optional[float] = None
        self._approx_backend_warned = False
        self._stats: Dict[str, int] = {
            "total": 0,
            "kept": 0,
            "dropped": 0,
            "dropped_exact_duplicate": 0,
            "dropped_approx_duplicate": 0,
            "dropped_adaptive_budget": 0,
            "kept_forced_event": 0,
            "kept_local_peak_change": 0,
            "kept_adaptive_budget_bypass_local_peak": 0,
            "kept_keepalive": 0,
            "approx_backend_unavailable": 0,
            "approx_decode_error": 0,
        }

    def notify_event(self, event_type: str, timestamp: Optional[float] = None) -> None:
        """Guarantee at least the next N screenshots are kept after important events."""
        budget = max(int(self.config.force_keep_after_event_count), 0)
        event_ts = self._coerce_timestamp(timestamp)
        if event_ts is not None:
            self._last_event_timestamp = event_ts
        if budget <= 0:
            return
        # Coalesce bursts of events instead of building an unbounded queue.
        self._force_keep_budget = max(self._force_keep_budget, budget)
        self._last_force_event = event_type or "event"

    def force_keep_next(self, count: int, event_type: str = "manual", timestamp: Optional[float] = None) -> None:
        """Force-keep next `count` frames regardless of dedup/adaptive logic."""
        budget = max(int(count), 0)
        event_ts = self._coerce_timestamp(timestamp)
        if event_ts is not None:
            self._last_event_timestamp = event_ts
        if budget <= 0:
            return
        self._force_keep_budget = max(self._force_keep_budget, budget)
        self._last_force_event = event_type or "manual"

    def decide(self, frame_bytes: bytes, timestamp: Optional[float] = None) -> FrameDecision:
        """Return keep/drop decision for a screenshot frame."""
        payload = frame_bytes or b""
        sha1 = hashlib.sha1(payload).hexdigest()
        self._stats["total"] += 1
        metrics: Dict[str, float | str | bool] = {}
        signature: Optional[tuple[int, ...]] = None
        frame_ts = self._coerce_timestamp(timestamp)

        force_event = None
        if not self.config.enabled:
            kept = True
            reason = "filter_disabled"
        elif self._force_keep_budget > 0:
            kept = True
            reason = "forced_event"
            force_event = self._last_force_event
            self._force_keep_budget -= 1
            self._stats["kept_forced_event"] += 1
        elif self._last_kept_sha1 == sha1:
            kept = False
            reason = "exact_duplicate"
            self._stats["dropped_exact_duplicate"] += 1
        else:
            kept, reason, signature_metrics, signature = self._decide_approx_stage(payload)
            metrics.update(signature_metrics)

        if self.config.enabled:
            kept, reason = self._apply_adaptive_budget(
                kept=kept,
                reason=reason,
                force_event=force_event,
                timestamp=frame_ts,
                metrics=metrics,
            )

        if not kept:
            kept, reason = self._apply_keepalive(
                kept=kept,
                reason=reason,
                timestamp=frame_ts,
                metrics=metrics,
            )

        if kept:
            self._mark_visual_activity(
                kept=kept,
                reason=reason,
                timestamp=frame_ts,
                metrics=metrics,
            )

        if kept:
            self._last_kept_sha1 = sha1
            self._last_kept_signature = self._resolve_kept_signature_baseline(
                payload=payload,
                decision_reason=reason,
                signature=signature,
            )
            if frame_ts is not None:
                self._last_kept_timestamp = frame_ts
            self._stats["kept"] += 1
        else:
            self._stats["dropped"] += 1

        return FrameDecision(
            kept=kept,
            reason=reason,
            sha1=sha1,
            force_event=force_event,
            force_keep_budget_remaining=self._force_keep_budget,
            metrics=metrics or None,
            stats=self.stats(),
        )

    def _resolve_kept_signature_baseline(
        self,
        *,
        payload: bytes,
        decision_reason: str,
        signature: Optional[tuple[int, ...]],
    ) -> Optional[tuple[int, ...]]:
        """Keep approximate baseline stable across non-approx keep paths (e.g. forced_event)."""
        if signature is not None:
            return signature

        # When approximate dedup is off, preserve whatever baseline we already had.
        if not self.config.enable_approx_dedup:
            return self._last_kept_signature

        # Forced-event keeps bypass the approx stage; refresh baseline proactively.
        if decision_reason == "forced_event":
            refreshed = self._build_signature(payload)
            if refreshed is not None:
                return refreshed

        # Decode failures / backend-missing should not wipe an existing baseline.
        return self._last_kept_signature

    def _coerce_timestamp(self, timestamp: Optional[float]) -> Optional[float]:
        if timestamp is None:
            return None
        try:
            return float(timestamp)
        except Exception:
            return None

    def _apply_adaptive_budget(
        self,
        *,
        kept: bool,
        reason: str,
        force_event: Optional[str],
        timestamp: Optional[float],
        metrics: Dict[str, float | str | bool],
    ) -> tuple[bool, str]:
        """Adaptive rate limiter after dedup decisions (optional, default off)."""
        if not kept:
            return kept, reason

        if not self.config.enable_adaptive_budget:
            metrics["budget_enabled"] = False
            return kept, reason

        metrics["budget_enabled"] = True
        if force_event:
            metrics["budget_status"] = "forced_event_bypass"
            metrics["budget_stage"] = "active"
            metrics["budget_active_source"] = "forced_event"
            return kept, reason

        if timestamp is None:
            metrics["budget_status"] = "missing_timestamp"
            return kept, reason

        stage = "idle"
        stage_source = "none"
        event_delta: Optional[float] = None
        visual_delta: Optional[float] = None
        if self._last_event_timestamp is not None:
            event_delta = timestamp - self._last_event_timestamp
            metrics["budget_time_since_event_sec"] = round(event_delta, 4)
        if self._last_visual_activity_timestamp is not None:
            visual_delta = timestamp - self._last_visual_activity_timestamp
            metrics["budget_time_since_visual_activity_sec"] = round(visual_delta, 4)

        in_event_window = (
            event_delta is not None
            and event_delta >= 0
            and event_delta <= float(self.config.adaptive_event_boost_window_sec)
        )
        in_visual_window = (
            visual_delta is not None
            and visual_delta >= 0
            and visual_delta <= float(self.config.adaptive_event_boost_window_sec)
        )
        if in_event_window and in_visual_window:
            stage = "active"
            stage_source = "event+visual"
        elif in_event_window:
            stage = "active"
            stage_source = "event"
        elif in_visual_window:
            stage = "active"
            stage_source = "visual"

        if stage == "active":
            min_interval = max(float(self.config.adaptive_active_min_keep_interval_sec), 0.0)
        else:
            min_interval = max(float(self.config.adaptive_idle_min_keep_interval_sec), 0.0)
        metrics["budget_stage"] = stage
        metrics["budget_active_source"] = stage_source
        metrics["budget_min_keep_interval_sec"] = round(min_interval, 4)

        if self._last_kept_timestamp is None:
            metrics["budget_status"] = "no_kept_baseline"
            return kept, reason

        delta = timestamp - self._last_kept_timestamp
        metrics["budget_time_since_last_kept_sec"] = round(delta, 4)
        if delta < 0:
            metrics["budget_status"] = "clock_reversed"
            return kept, reason

        if delta >= min_interval:
            metrics["budget_status"] = "pass"
            return kept, reason

        if (
            reason == "local_peak_change"
            and bool(self.config.adaptive_bypass_local_peak_change)
        ):
            metrics["budget_status"] = "bypass_local_peak_change"
            self._stats["kept_adaptive_budget_bypass_local_peak"] += 1
            return kept, reason

        metrics["budget_status"] = "cooldown_drop"
        self._stats["dropped_adaptive_budget"] += 1
        return False, "adaptive_budget_cooldown"

    def _apply_keepalive(
        self,
        *,
        kept: bool,
        reason: str,
        timestamp: Optional[float],
        metrics: Dict[str, float | str | bool],
    ) -> tuple[bool, str]:
        """Force-keep a frame if too long has passed since the last kept frame."""
        max_interval = float(self.config.keepalive_max_interval_sec)
        if max_interval <= 0:
            return kept, reason
        if timestamp is None:
            return kept, reason
        if self._last_kept_timestamp is None:
            return kept, reason
        delta = timestamp - self._last_kept_timestamp
        metrics["keepalive_time_since_last_kept_sec"] = round(delta, 4)
        if delta >= max_interval:
            self._stats["kept_keepalive"] += 1
            metrics["keepalive_triggered"] = True
            return True, "keepalive"
        return kept, reason

    def _mark_visual_activity(
        self,
        *,
        kept: bool,
        reason: str,
        timestamp: Optional[float],
        metrics: Dict[str, float | str | bool],
    ) -> None:
        """Use visual changes as an activity hint for future adaptive budget windows."""
        if not kept:
            return
        if not self.config.enable_adaptive_budget:
            return
        if not bool(self.config.adaptive_boost_on_visual_change):
            return
        if timestamp is None:
            return
        approx_status = str(metrics.get("approx_status") or "")
        if approx_status not in {"kept_changed", "kept_local_peak_change"}:
            return
        self._last_visual_activity_timestamp = timestamp
        metrics["budget_visual_activity_boost_armed"] = True

    def _decide_approx_stage(
        self,
        payload: bytes,
    ) -> tuple[bool, str, Dict[str, float | str | bool], Optional[tuple[int, ...]]]:
        """Optional approximate dedup stage after exact-hash check."""
        if not self.config.enable_approx_dedup:
            return True, "new_frame", {"approx_enabled": False}, None

        metrics: Dict[str, float | str | bool] = {
            "approx_enabled": True,
            "approx_backend": "pillow" if Image is not None else "missing_pillow",
        }
        signature = self._build_signature(payload)
        if signature is None:
            # Keep frame when approximation is unavailable or decode failed.
            metrics["approx_status"] = "backend_unavailable_or_decode_error"
            return True, "new_frame", metrics, None

        if self._last_kept_signature is None:
            metrics["approx_status"] = "no_baseline"
            return True, "new_frame", metrics, signature

        mean_abs = self.signature_mean_abs_diff(signature, self._last_kept_signature)
        effective_mean_abs = mean_abs
        center_roi_enabled = bool(self.config.enable_approx_center_roi_weighting)
        metrics["approx_center_roi_enabled"] = center_roi_enabled
        if center_roi_enabled:
            roi_mean_abs = self.signature_center_roi_weighted_mean_abs_diff(
                signature,
                self._last_kept_signature,
                width=max(int(self.config.approx_signature_width), 1),
                height=max(int(self.config.approx_signature_height), 1),
                center_width_ratio=float(self.config.approx_center_roi_width_ratio),
                center_height_ratio=float(self.config.approx_center_roi_height_ratio),
                center_weight=float(self.config.approx_center_roi_weight),
            )
            effective_mean_abs = roi_mean_abs
            metrics["approx_center_roi_mean_abs_diff"] = round(roi_mean_abs, 4)
            metrics["approx_center_roi_width_ratio"] = round(float(self.config.approx_center_roi_width_ratio), 4)
            metrics["approx_center_roi_height_ratio"] = round(float(self.config.approx_center_roi_height_ratio), 4)
            metrics["approx_center_roi_weight"] = round(float(self.config.approx_center_roi_weight), 4)
        peak_tile = self.signature_peak_tile_mean_abs_diff(
            signature,
            self._last_kept_signature,
            width=max(int(self.config.approx_signature_width), 1),
            height=max(int(self.config.approx_signature_height), 1),
            tile_cols=max(int(self.config.approx_tile_cols), 1),
            tile_rows=max(int(self.config.approx_tile_rows), 1),
        )
        metrics["approx_mean_abs_diff"] = round(mean_abs, 4)
        metrics["approx_effective_mean_abs_diff"] = round(effective_mean_abs, 4)
        metrics["approx_peak_tile_abs_diff"] = round(peak_tile, 4)

        if (
            effective_mean_abs <= float(self.config.approx_diff_threshold)
            and peak_tile < float(self.config.approx_peak_tile_guard_threshold)
        ):
            metrics["approx_status"] = "dropped_approx_duplicate"
            self._stats["dropped_approx_duplicate"] += 1
            return False, "approx_duplicate", metrics, signature

        if (
            effective_mean_abs <= float(self.config.approx_diff_threshold)
            and peak_tile >= float(self.config.approx_peak_tile_guard_threshold)
        ):
            metrics["approx_status"] = "kept_local_peak_change"
            self._stats["kept_local_peak_change"] += 1
            return True, "local_peak_change", metrics, signature

        metrics["approx_status"] = "kept_changed"
        return True, "new_frame", metrics, signature

    def _build_signature(self, payload: bytes) -> Optional[tuple[int, ...]]:
        """Decode JPEG bytes into a small grayscale signature for CPU-friendly diff."""
        if Image is None:
            self._stats["approx_backend_unavailable"] += 1
            if not self._approx_backend_warned:
                print("⚠️  Pillow is not installed; approximate frame dedup disabled (exact dedup still active)")
                self._approx_backend_warned = True
            return None

        width = max(int(self.config.approx_signature_width), 1)
        height = max(int(self.config.approx_signature_height), 1)
        try:
            with Image.open(BytesIO(payload)) as img:
                gray = img.convert("L")
                resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
                small = gray.resize((width, height), resampling)
                return tuple(int(v) for v in small.getdata())
        except Exception:
            self._stats["approx_decode_error"] += 1
            return None

    @staticmethod
    def signature_mean_abs_diff(a: Sequence[int], b: Sequence[int]) -> float:
        """Mean absolute difference between two equal-length grayscale signatures."""
        if len(a) != len(b):
            raise ValueError("Signatures must have equal length")
        if not a:
            return 0.0
        total = 0
        for av, bv in zip(a, b):
            total += abs(int(av) - int(bv))
        return total / len(a)

    @staticmethod
    def signature_peak_tile_mean_abs_diff(
        a: Sequence[int],
        b: Sequence[int],
        *,
        width: int,
        height: int,
        tile_cols: int,
        tile_rows: int,
    ) -> float:
        """Peak tile mean-abs-diff to preserve localized small object changes."""
        if len(a) != len(b):
            raise ValueError("Signatures must have equal length")
        if width <= 0 or height <= 0 or len(a) != width * height:
            raise ValueError("Signature size does not match width*height")
        tile_cols = max(int(tile_cols), 1)
        tile_rows = max(int(tile_rows), 1)

        peak = 0.0
        for row in range(tile_rows):
            y0 = (row * height) // tile_rows
            y1 = ((row + 1) * height) // tile_rows
            for col in range(tile_cols):
                x0 = (col * width) // tile_cols
                x1 = ((col + 1) * width) // tile_cols
                if x1 <= x0 or y1 <= y0:
                    continue
                total = 0
                count = 0
                for y in range(y0, y1):
                    base = y * width
                    for x in range(x0, x1):
                        idx = base + x
                        total += abs(int(a[idx]) - int(b[idx]))
                        count += 1
                if count <= 0:
                    continue
                tile_mean = total / count
                if tile_mean > peak:
                    peak = tile_mean
        return peak

    @staticmethod
    def signature_center_roi_weighted_mean_abs_diff(
        a: Sequence[int],
        b: Sequence[int],
        *,
        width: int,
        height: int,
        center_width_ratio: float,
        center_height_ratio: float,
        center_weight: float,
    ) -> float:
        """Mean absolute diff with center-ROI weighting (normalized by total weights)."""
        if len(a) != len(b):
            raise ValueError("Signatures must have equal length")
        if width <= 0 or height <= 0 or len(a) != width * height:
            raise ValueError("Signature size does not match width*height")
        if not a:
            return 0.0

        # Clamp ratios to sensible bounds and derive a centered rectangle.
        width_ratio = min(max(float(center_width_ratio), 0.0), 1.0)
        height_ratio = min(max(float(center_height_ratio), 0.0), 1.0)
        roi_w = min(width, max(int(round(width * width_ratio)), 1))
        roi_h = min(height, max(int(round(height * height_ratio)), 1))
        x0 = max((width - roi_w) // 2, 0)
        y0 = max((height - roi_h) // 2, 0)
        x1 = min(x0 + roi_w, width)
        y1 = min(y0 + roi_h, height)

        center_w = max(float(center_weight), 0.0)
        edge_w = 1.0
        weighted_sum = 0.0
        weight_total = 0.0
        for y in range(height):
            base = y * width
            in_y = y0 <= y < y1
            for x in range(width):
                idx = base + x
                in_center = in_y and (x0 <= x < x1)
                w = center_w if in_center else edge_w
                weighted_sum += w * abs(int(a[idx]) - int(b[idx]))
                weight_total += w

        if weight_total <= 0:
            return 0.0
        return weighted_sum / weight_total

    def stats(self) -> Dict[str, int]:
        """Return a copy of internal counters for logging/telemetry."""
        return dict(self._stats)

    def pending_force_keep_budget(self) -> int:
        """Remaining forced-keep budget for upcoming screenshots."""
        return int(self._force_keep_budget)


def _increment_count(counter: Dict[str, int], key: Optional[str]) -> None:
    if not key:
        return
    key_text = str(key)
    counter[key_text] = counter.get(key_text, 0) + 1


def summarize_frame_filter_telemetry_records(records: Iterable[dict]) -> dict:
    """Build a compact summary from frame filter telemetry records."""
    summary: Dict[str, Any] = {
        "entries_total": 0,
        "records_ignored": 0,
        "screenshot_frames_total": 0,
        "screenshot_frames_kept": 0,
        "screenshot_frames_dropped": 0,
        "bytes_total": 0,
        "bytes_kept": 0,
        "bytes_dropped": 0,
        "reasons": {},
        "kept_reasons": {},
        "dropped_reasons": {},
        "force_events": {},
        "trigger_sources": {},
        "approx_status": {},
        "budget_stage": {},
        "budget_status": {},
        "budget_active_source": {},
        "timestamps": {},
        "last_stats": {},
        "latest_screenshot_index": None,
    }

    approx_mean_sum = 0.0
    approx_mean_count = 0
    approx_peak_sum = 0.0
    approx_peak_count = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None

    for raw in records:
        if not isinstance(raw, dict):
            summary["records_ignored"] += 1
            continue

        summary["entries_total"] += 1
        record_type = str(raw.get("type") or "")
        ts_raw = raw.get("timestamp")
        try:
            ts = float(ts_raw)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        except Exception:
            ts = None

        if record_type == "screenshot_force_keep_trigger":
            _increment_count(summary["trigger_sources"], raw.get("source"))
            continue

        if record_type != "screenshot_frame":
            summary["records_ignored"] += 1
            continue

        summary["screenshot_frames_total"] += 1
        kept = bool(raw.get("kept"))
        reason = str(raw.get("reason") or "unknown")
        _increment_count(summary["reasons"], reason)
        _increment_count(summary["kept_reasons"] if kept else summary["dropped_reasons"], reason)

        force_event = raw.get("force_event")
        _increment_count(summary["force_events"], force_event)

        size_bytes = raw.get("size_bytes", 0)
        try:
            size_int = max(int(size_bytes), 0)
        except Exception:
            size_int = 0
        summary["bytes_total"] += size_int
        if kept:
            summary["screenshot_frames_kept"] += 1
            summary["bytes_kept"] += size_int
        else:
            summary["screenshot_frames_dropped"] += 1
            summary["bytes_dropped"] += size_int

        shot_index = raw.get("screenshot_index")
        try:
            shot_index_int = int(shot_index)
            latest = summary["latest_screenshot_index"]
            if latest is None or shot_index_int > latest:
                summary["latest_screenshot_index"] = shot_index_int
        except Exception:
            pass

        metrics = raw.get("metrics")
        if isinstance(metrics, dict):
            _increment_count(summary["approx_status"], metrics.get("approx_status"))
            _increment_count(summary["budget_stage"], metrics.get("budget_stage"))
            _increment_count(summary["budget_status"], metrics.get("budget_status"))
            _increment_count(summary["budget_active_source"], metrics.get("budget_active_source"))
            mean_diff = metrics.get("approx_mean_abs_diff")
            peak_diff = metrics.get("approx_peak_tile_abs_diff")
            try:
                approx_mean_sum += float(mean_diff)
                approx_mean_count += 1
            except Exception:
                pass
            try:
                approx_peak_sum += float(peak_diff)
                approx_peak_count += 1
            except Exception:
                pass

        stats = raw.get("stats")
        if isinstance(stats, dict):
            summary["last_stats"] = dict(stats)

    frame_total = int(summary["screenshot_frames_total"])
    kept_total = int(summary["screenshot_frames_kept"])
    dropped_total = int(summary["screenshot_frames_dropped"])
    summary["drop_ratio"] = (dropped_total / frame_total) if frame_total > 0 else 0.0
    summary["keep_ratio"] = (kept_total / frame_total) if frame_total > 0 else 0.0

    if first_ts is not None or last_ts is not None:
        timestamps: Dict[str, float] = {}
        if first_ts is not None:
            timestamps["first"] = first_ts
        if last_ts is not None:
            timestamps["last"] = last_ts
        if first_ts is not None and last_ts is not None:
            timestamps["duration_sec"] = max(last_ts - first_ts, 0.0)
        summary["timestamps"] = timestamps

    if approx_mean_count > 0:
        summary["approx_mean_abs_diff_avg"] = approx_mean_sum / approx_mean_count
    if approx_peak_count > 0:
        summary["approx_peak_tile_abs_diff_avg"] = approx_peak_sum / approx_peak_count

    return summary


def summarize_frame_filter_telemetry_file(path: str | Path) -> dict:
    """Load a JSONL telemetry file and summarize it."""
    telemetry_path = Path(path)
    result: Dict[str, Any] = {
        "telemetry_file": str(telemetry_path),
        "exists": telemetry_path.is_file(),
    }
    if not telemetry_path.is_file():
        result["error"] = "telemetry_file_not_found"
        return result

    parse_errors = 0

    def _iter_records() -> Iterable[dict]:
        nonlocal parse_errors
        with telemetry_path.open("r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except Exception:
                    parse_errors += 1
                    continue
                if isinstance(parsed, dict):
                    yield parsed
                else:
                    parse_errors += 1

    summary = summarize_frame_filter_telemetry_records(_iter_records())
    result["summary"] = summary
    if parse_errors:
        result["parse_errors"] = parse_errors
    return result

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.frame_filter import (
    ExactFrameDeduper,
    FrameFilterConfig,
    summarize_frame_filter_telemetry_records,
)


class ExactFrameDeduperTests(unittest.TestCase):
    def test_drops_consecutive_exact_duplicates(self):
        deduper = ExactFrameDeduper()

        first = deduper.decide(b"frame-a")
        second = deduper.decide(b"frame-a")
        third = deduper.decide(b"frame-b")

        self.assertTrue(first.kept)
        self.assertEqual(first.reason, "new_frame")
        self.assertFalse(second.kept)
        self.assertEqual(second.reason, "exact_duplicate")
        self.assertTrue(third.kept)
        self.assertEqual(third.reason, "new_frame")

    def test_event_forces_next_duplicate_frame_to_be_kept(self):
        deduper = ExactFrameDeduper(FrameFilterConfig(enabled=True, force_keep_after_event_count=1))

        deduper.decide(b"same")
        deduper.notify_event("chat_message")
        forced = deduper.decide(b"same")
        dropped_after = deduper.decide(b"same")

        self.assertTrue(forced.kept)
        self.assertEqual(forced.reason, "forced_event")
        self.assertEqual(forced.force_event, "chat_message")
        self.assertFalse(dropped_after.kept)
        self.assertEqual(dropped_after.reason, "exact_duplicate")

    def test_disabled_filter_keeps_all_frames(self):
        deduper = ExactFrameDeduper(FrameFilterConfig(enabled=False))

        first = deduper.decide(b"x")
        second = deduper.decide(b"x")

        self.assertTrue(first.kept)
        self.assertTrue(second.kept)
        self.assertEqual(second.reason, "filter_disabled")

    def test_event_burst_is_coalesced(self):
        deduper = ExactFrameDeduper(FrameFilterConfig(enabled=True, force_keep_after_event_count=1))

        deduper.notify_event("chat_message")
        deduper.notify_event("command_result")
        forced = deduper.decide(b"f1")
        normal = deduper.decide(b"f2")

        self.assertEqual(forced.reason, "forced_event")
        self.assertEqual(forced.force_event, "command_result")
        self.assertEqual(normal.reason, "new_frame")

    def test_signature_peak_tile_detects_localized_change(self):
        width, height = 4, 4
        base = [0] * (width * height)
        local_change = base.copy()
        local_change[0] = 100  # Only one pixel changed in top-left tile

        mean_abs = ExactFrameDeduper.signature_mean_abs_diff(base, local_change)
        peak_tile = ExactFrameDeduper.signature_peak_tile_mean_abs_diff(
            base,
            local_change,
            width=width,
            height=height,
            tile_cols=2,
            tile_rows=2,
        )

        self.assertAlmostEqual(mean_abs, 100 / 16)
        self.assertAlmostEqual(peak_tile, 100 / 4)
        self.assertGreater(peak_tile, mean_abs)

    def test_center_roi_weighted_diff_emphasizes_center(self):
        width, height = 4, 4
        base = [0] * (width * height)
        edge_change = base.copy()
        center_change = base.copy()
        edge_change[0] = 20         # (0,0) edge
        center_change[5] = 20       # (1,1) center for 4x4 with 0.5x0.5 ROI

        edge_weighted = ExactFrameDeduper.signature_center_roi_weighted_mean_abs_diff(
            base,
            edge_change,
            width=width,
            height=height,
            center_width_ratio=0.5,
            center_height_ratio=0.5,
            center_weight=4.0,
        )
        center_weighted = ExactFrameDeduper.signature_center_roi_weighted_mean_abs_diff(
            base,
            center_change,
            width=width,
            height=height,
            center_width_ratio=0.5,
            center_height_ratio=0.5,
            center_weight=4.0,
        )
        global_mean = ExactFrameDeduper.signature_mean_abs_diff(base, edge_change)

        self.assertAlmostEqual(global_mean, 20 / 16)
        self.assertLess(edge_weighted, global_mean)
        self.assertGreater(center_weighted, global_mean)
        self.assertGreater(center_weighted, edge_weighted)

    def test_center_roi_weighting_can_drop_edge_change_but_keep_center_change(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                force_keep_after_event_count=0,
                enable_approx_dedup=True,
                approx_signature_width=4,
                approx_signature_height=4,
                approx_tile_cols=1,
                approx_tile_rows=1,
                enable_approx_center_roi_weighting=True,
                approx_center_roi_width_ratio=0.5,
                approx_center_roi_height_ratio=0.5,
                approx_center_roi_weight=4.0,
                approx_diff_threshold=1.5,
                approx_peak_tile_guard_threshold=999.0,
            )
        )

        base = [0] * 16
        edge_change = base.copy()
        center_change = base.copy()
        edge_change[0] = 20
        center_change[5] = 20
        signatures = {
            b"base": tuple(base),
            b"edge": tuple(edge_change),
            b"center": tuple(center_change),
        }
        deduper._build_signature = lambda payload: signatures[payload]  # type: ignore[method-assign]

        first = deduper.decide(b"base")
        edge = deduper.decide(b"edge")
        center = deduper.decide(b"center")

        self.assertTrue(first.kept)
        self.assertFalse(edge.kept)
        self.assertEqual(edge.reason, "approx_duplicate")
        self.assertTrue((edge.metrics or {}).get("approx_center_roi_enabled"))
        self.assertTrue(center.kept)
        self.assertEqual(center.reason, "new_frame")
        self.assertGreater(
            float((center.metrics or {}).get("approx_effective_mean_abs_diff", 0.0)),
            float((edge.metrics or {}).get("approx_effective_mean_abs_diff", 0.0)),
        )

    def test_approx_stage_falls_back_when_pillow_missing(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                enable_approx_dedup=True,
                force_keep_after_event_count=0,
            )
        )

        first = deduper.decide(b"not-a-real-image")
        second = deduper.decide(b"another-non-image-payload")

        self.assertTrue(first.kept)
        self.assertTrue(second.kept)
        self.assertEqual(first.reason, "new_frame")
        self.assertEqual(second.reason, "new_frame")
        self.assertIn("approx_enabled", first.metrics or {})

    def test_forced_event_kept_frame_refreshes_approx_baseline(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                force_keep_after_event_count=1,
                enable_approx_dedup=True,
                approx_signature_width=2,
                approx_signature_height=1,
                approx_tile_cols=1,
                approx_tile_rows=1,
                approx_diff_threshold=0.1,
                approx_peak_tile_guard_threshold=999.0,
            )
        )

        signatures = {
            b"a": (0, 0),
            b"b": (100, 100),
            b"b2": (100, 100),
        }
        deduper._build_signature = lambda payload: signatures[payload]  # type: ignore[method-assign]

        first = deduper.decide(b"a")
        deduper.notify_event("chat_message")
        forced = deduper.decide(b"b")
        after_forced = deduper.decide(b"b2")

        self.assertTrue(first.kept)
        self.assertEqual(first.reason, "new_frame")
        self.assertTrue(forced.kept)
        self.assertEqual(forced.reason, "forced_event")
        self.assertFalse(after_forced.kept)
        self.assertEqual(after_forced.reason, "approx_duplicate")

    def test_adaptive_budget_drops_frames_during_idle_cooldown(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                force_keep_after_event_count=0,
                enable_adaptive_budget=True,
                adaptive_idle_min_keep_interval_sec=5.0,
                adaptive_active_min_keep_interval_sec=1.0,
                adaptive_event_boost_window_sec=2.0,
            )
        )

        first = deduper.decide(b"a", timestamp=0.0)
        second = deduper.decide(b"b", timestamp=1.0)
        third = deduper.decide(b"c", timestamp=6.0)

        self.assertTrue(first.kept)
        self.assertEqual(first.reason, "new_frame")
        self.assertEqual((first.metrics or {}).get("budget_status"), "no_kept_baseline")

        self.assertFalse(second.kept)
        self.assertEqual(second.reason, "adaptive_budget_cooldown")
        self.assertEqual((second.metrics or {}).get("budget_stage"), "idle")
        self.assertEqual((second.metrics or {}).get("budget_status"), "cooldown_drop")

        self.assertTrue(third.kept)
        self.assertEqual((third.metrics or {}).get("budget_status"), "pass")

    def test_adaptive_budget_uses_active_window_after_event(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                force_keep_after_event_count=0,
                enable_adaptive_budget=True,
                adaptive_idle_min_keep_interval_sec=5.0,
                adaptive_active_min_keep_interval_sec=1.0,
                adaptive_event_boost_window_sec=3.0,
            )
        )

        deduper.decide(b"a", timestamp=0.0)
        deduper.notify_event("chat_message", timestamp=0.5)
        kept_in_active_window = deduper.decide(b"b", timestamp=1.4)

        self.assertTrue(kept_in_active_window.kept)
        self.assertEqual(kept_in_active_window.reason, "new_frame")
        self.assertEqual((kept_in_active_window.metrics or {}).get("budget_stage"), "active")
        self.assertEqual((kept_in_active_window.metrics or {}).get("budget_status"), "pass")
        self.assertEqual((kept_in_active_window.metrics or {}).get("budget_active_source"), "event")

    def test_adaptive_budget_can_be_activated_by_visual_change(self):
        deduper = ExactFrameDeduper(
            FrameFilterConfig(
                enabled=True,
                force_keep_after_event_count=0,
                enable_approx_dedup=True,
                approx_signature_width=2,
                approx_signature_height=1,
                approx_tile_cols=1,
                approx_tile_rows=1,
                approx_diff_threshold=1.0,
                approx_peak_tile_guard_threshold=999.0,
                enable_adaptive_budget=True,
                adaptive_idle_min_keep_interval_sec=5.0,
                adaptive_active_min_keep_interval_sec=0.4,
                adaptive_event_boost_window_sec=2.0,
                adaptive_boost_on_visual_change=True,
            )
        )

        signatures = {
            b"a": (0, 0),
            b"b": (40, 40),
            b"c": (90, 90),
        }
        deduper._build_signature = lambda payload: signatures[payload]  # type: ignore[method-assign]

        first = deduper.decide(b"a", timestamp=0.0)
        visual_change = deduper.decide(b"b", timestamp=10.0)
        after_visual_boost = deduper.decide(b"c", timestamp=10.5)

        self.assertTrue(first.kept)
        self.assertTrue(visual_change.kept)
        self.assertEqual((visual_change.metrics or {}).get("approx_status"), "kept_changed")
        self.assertTrue((visual_change.metrics or {}).get("budget_visual_activity_boost_armed"))

        self.assertTrue(after_visual_boost.kept)
        self.assertEqual((after_visual_boost.metrics or {}).get("budget_stage"), "active")
        self.assertEqual((after_visual_boost.metrics or {}).get("budget_active_source"), "visual")
        self.assertEqual((after_visual_boost.metrics or {}).get("budget_status"), "pass")

    def test_telemetry_summary_counts_frames_and_triggers(self):
        summary = summarize_frame_filter_telemetry_records(
            [
                {
                    "type": "screenshot_force_keep_trigger",
                    "timestamp": 10.0,
                    "source": "chat_message:player",
                    "force_budget": 1,
                },
                {
                    "type": "screenshot_frame",
                    "timestamp": 11.0,
                    "screenshot_index": 1,
                    "kept": True,
                    "reason": "forced_event",
                    "force_event": "chat_message:player",
                    "size_bytes": 100,
                    "metrics": {
                        "approx_status": "no_baseline",
                        "budget_status": "forced_event_bypass",
                        "budget_active_source": "forced_event",
                    },
                    "stats": {"total": 1, "kept": 1},
                },
                {
                    "type": "screenshot_frame",
                    "timestamp": 12.0,
                    "screenshot_index": 2,
                    "kept": False,
                    "reason": "exact_duplicate",
                    "force_event": None,
                    "size_bytes": 120,
                    "metrics": {},
                    "stats": {"total": 2, "kept": 1, "dropped": 1},
                },
            ]
        )

        self.assertEqual(summary["entries_total"], 3)
        self.assertEqual(summary["screenshot_frames_total"], 2)
        self.assertEqual(summary["screenshot_frames_kept"], 1)
        self.assertEqual(summary["screenshot_frames_dropped"], 1)
        self.assertEqual(summary["bytes_total"], 220)
        self.assertEqual(summary["trigger_sources"]["chat_message:player"], 1)
        self.assertEqual(summary["reasons"]["forced_event"], 1)
        self.assertEqual(summary["dropped_reasons"]["exact_duplicate"], 1)
        self.assertEqual(summary["budget_status"]["forced_event_bypass"], 1)
        self.assertEqual(summary["budget_active_source"]["forced_event"], 1)
        self.assertAlmostEqual(summary["drop_ratio"], 0.5)

    def test_telemetry_summary_ignores_unknown_records(self):
        summary = summarize_frame_filter_telemetry_records(
            [
                {"type": "foo"},
                "bad-record",
                {"type": "screenshot_frame", "kept": True, "reason": "new_frame", "size_bytes": 1},
            ]
        )

        self.assertEqual(summary["entries_total"], 2)
        self.assertEqual(summary["records_ignored"], 2)
        self.assertEqual(summary["screenshot_frames_total"], 1)
        self.assertEqual(summary["screenshot_frames_kept"], 1)
        self.assertEqual(summary["bytes_total"], 1)


if __name__ == "__main__":
    unittest.main()

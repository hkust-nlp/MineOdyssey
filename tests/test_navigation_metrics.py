import unittest

from eval.navigation.metrics import (
    CheckpointTracker,
    NavigationMetricAccumulator,
    distance_3d,
    distance_xz,
)


class NavigationMetricsTest(unittest.TestCase):
    def test_distance_helpers(self):
        left = {"x": 0, "y": 0, "z": 0}
        right = {"x": 3, "y": 12, "z": 4}
        self.assertEqual(distance_xz(left, right), 5)
        self.assertEqual(distance_3d(left, right), 13)

    def test_checkpoints_are_ordered(self):
        tracker = CheckpointTracker(
            [
                {
                    "waypoint_id": "first",
                    "position": {"x": 1, "y": 0, "z": 0},
                    "radius_3d": 0.5,
                    "radius_y": 0.5,
                },
                {
                    "waypoint_id": "second",
                    "position": {"x": 2, "y": 0, "z": 0},
                    "radius_3d": 0.5,
                    "radius_y": 0.5,
                },
            ]
        )
        tracker.observe({"x": 2, "y": 0, "z": 0}, 1)
        self.assertEqual(tracker.summary()["ordered_reached"], 0)
        tracker.observe({"x": 1, "y": 0, "z": 0}, 2)
        tracker.observe({"x": 2, "y": 0, "z": 0}, 3)
        self.assertEqual(tracker.summary()["coverage"], 1)

    def test_checkpoint_default_radius_is_strictly_less_than_three_point_five(self):
        checkpoint = {
            "waypoint_id": "target",
            "position": {"x": 0, "y": 0, "z": 0},
        }
        outside = CheckpointTracker([checkpoint])
        self.assertIsNone(outside.observe({"x": 3.5, "y": 0, "z": 0}, 1))
        inside = CheckpointTracker([checkpoint])
        self.assertIsNotNone(inside.observe({"x": 3.49, "y": 0, "z": 0}, 1))

    def test_path_progress_and_spl(self):
        metrics = NavigationMetricAccumulator(
            start={"x": 0, "y": 0, "z": 0},
            target={"x": 10, "y": 0, "z": 0},
            reference_length_blocks=8,
        )
        metrics.observe({"x": 0, "y": 0, "z": 0}, 0)
        metrics.observe({"x": 4, "y": 0, "z": 3}, 1)
        metrics.observe({"x": 10, "y": 0, "z": 0}, 2)
        result = metrics.finalize(completed=True, duration_sec=2)
        self.assertAlmostEqual(result["path_length_3d"], 11.708204)
        self.assertAlmostEqual(result["static_spl"], 8 / 11.7082039325, places=6)
        self.assertEqual(result["progress_ratio_xz"], 1)
        self.assertEqual(result["average_speed_xz"], result["path_length_xz"] / 2)
        failed = metrics.finalize(completed=False, duration_sec=2)
        self.assertEqual(failed["static_spl"], 0)


if __name__ == "__main__":
    unittest.main()

import unittest

from eval.navigation.startup import StartReadinessPolicy, StartReadinessTracker


def state(*, x=524.5, y=-8.0, z=659.5, on_ground=True, health=20.0):
    return {
        "success": True,
        "data": {
            "position": {"x": x, "y": y, "z": z},
            "on_ground": on_ground,
            "health": health,
        },
    }


class StartReadinessTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = StartReadinessTracker(
            StartReadinessPolicy(expected_x=524.5, expected_y=-7.0, expected_z=659.5)
        )

    def test_requires_three_stable_grounded_samples(self) -> None:
        self.assertFalse(self.tracker.observe(state())["ready"])
        self.assertFalse(self.tracker.observe(state())["ready"])
        self.assertFalse(self.tracker.observe(state())["ready"])
        self.assertTrue(self.tracker.observe(state())["ready"])

    def test_airborne_sample_resets_progress(self) -> None:
        self.tracker.observe(state())
        self.tracker.observe(state())
        sample = self.tracker.observe(state(on_ground=False))
        self.assertEqual(sample["consecutive_ready_samples"], 0)
        self.assertFalse(sample["ready"])

    def test_rejects_stable_position_far_below_start(self) -> None:
        self.tracker.observe(state(y=-20.0))
        sample = self.tracker.observe(state(y=-20.0))
        self.assertFalse(sample["near_start"])
        self.assertFalse(sample["ready"])

    def test_rejects_dead_player(self) -> None:
        self.tracker.observe(state(health=0.0))
        sample = self.tracker.observe(state(health=0.0))
        self.assertFalse(sample["eligible"])


if __name__ == "__main__":
    unittest.main()

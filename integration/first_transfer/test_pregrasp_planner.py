import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pregrasp_planner import orientation_waypoints, pregrasp_pose


class PregraspPlannerTests(unittest.TestCase):
    def test_pregrasp_is_above_target_and_keeps_small_offset_bounded(self):
        self.assertEqual(pregrasp_pose([.427, -.013, .28], .48), (.427, -.013, .48))
        self.assertEqual(pregrasp_pose([.427, -.013, .50], .48, [.02, -.01]),
                         (.447, -.023, .50))

    def test_pregrasp_rejects_offsets_or_heights_outside_envelope(self):
        with self.assertRaises(ValueError):
            pregrasp_pose([.4, 0, .2], .48, [.05, 0])
        with self.assertRaises(ValueError):
            pregrasp_pose([.4, 0, .2], .60)

    def test_orientation_steps_follow_shortest_arc_and_respect_limit(self):
        start = [1, 0, 0, 0]
        target = [math.cos(math.pi / 4), 0, 0, math.sin(math.pi / 4)]
        steps = orientation_waypoints(start, target, 30)
        self.assertEqual(len(steps), 3)
        previous = start
        for step in steps:
            dot = abs(sum(a * b for a, b in zip(previous, step)))
            self.assertLessEqual(2 * math.degrees(math.acos(min(1.0, dot))), 30.0001)
            previous = step
        self.assertAlmostEqual(abs(sum(a * b for a, b in zip(steps[-1], target))), 1.0)

    def test_orientation_handles_antipodal_quaternion_without_long_rotation(self):
        steps = orientation_waypoints([1, 0, 0, 0], [-1, 0, 0, 0], 30)
        self.assertEqual(len(steps), 1)
        self.assertAlmostEqual(abs(steps[0][0]), 1.0)


if __name__ == "__main__":
    unittest.main()

"""The high-probe fallback cannot turn a low or vertical grasp into success."""
import unittest

from high_probe_gate import high_probe_physically_reached


class HighProbeGateTests(unittest.TestCase):
    def test_measured_high_side_pose_is_accepted_for_preflight_only(self):
        self.assertTrue(high_probe_physically_reached(
            phase="pick_electrode/align/orientation_probe",
            target_m=[.427464, -.012800, .48],
            actual_m=[.438859, -.013049, .472640],
            closing_axis_world=[.981212, .057818, -.184066],
            tolerance_m=.0143942))

    def test_rejects_low_waypoint_even_with_small_error(self):
        self.assertFalse(high_probe_physically_reached(
            phase="pick_electrode/align", target_m=[.288, 0, .145],
            actual_m=[.288, 0, .145], closing_axis_world=[1, 0, 0],
            tolerance_m=.014))

    def test_rejects_vertical_grasp_or_large_deviation(self):
        base = dict(phase="pick_electrode/align/orientation_probe",
                    target_m=[.427, -.013, .48], tolerance_m=.014)
        self.assertFalse(high_probe_physically_reached(
            **base, actual_m=[.438, -.013, .472], closing_axis_world=[.7, 0, .71]))
        self.assertFalse(high_probe_physically_reached(
            **base, actual_m=[.45, -.013, .472], closing_axis_world=[1, 0, 0]))


if __name__ == "__main__":
    unittest.main()

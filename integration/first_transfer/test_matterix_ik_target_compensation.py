"""Tests for the measured, runtime-pinned simulator pose correction."""
import unittest

from matterix_ik_target_compensation import compensated_target, residual_for_runtime


class MatterixTargetCompensationTests(unittest.TestCase):
    def test_corrects_only_the_measured_simulator_y_residual(self):
        target = compensated_target((0.56, -0.16, 0.2999))
        self.assertAlmostEqual(target[0], 0.56)
        self.assertAlmostEqual(target[1], -0.1636)
        self.assertAlmostEqual(target[2], 0.2999)

    def test_rejects_non_xyz_pose(self):
        with self.assertRaises(ValueError):
            compensated_target((0.1, 0.2))

    def test_original_lab3_runtime_does_not_inherit_lab2_residual(self):
        pose = (0.56, -0.16, 0.2999)
        self.assertEqual(compensated_target(pose, isaaclab_version="3.0.0b2.post1"), pose)
        self.assertEqual(residual_for_runtime("3.0.0b2.post1"), (0.0, 0.0, 0.0))

    def test_unknown_runtime_requires_explicit_verification(self):
        with self.assertRaises(ValueError):
            compensated_target((0.56, -0.16, 0.2999), isaaclab_version="4.0.0")


if __name__ == '__main__':
    unittest.main()

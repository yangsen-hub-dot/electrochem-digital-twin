"""CPU visual-readability checks, not device physics or efficiency validation."""
import unittest
from mixer_presentation import stirring_angle_degrees, mixing_camera_spec, update_stirring_visuals
from microcell_labware import CELLS


class MixerPresentationTests(unittest.TestCase):
    def test_sampled_bar_motion_is_visible_without_half_turn_aliasing(self):
        angles = [stirring_angle_degrees(i/12) for i in range(121)]
        deltas = [b-a for a,b in zip(angles, angles[1:])]
        self.assertTrue(all(d > 0 for d in deltas))
        self.assertGreater(deltas[24], 15)
        self.assertLess(max(deltas), 30)
        self.assertLess(deltas[0], deltas[24])
        self.assertLess(deltas[-1], deltas[24])
        self.assertEqual(stirring_angle_degrees(11), angles[-1])

    def test_camera_above_both_rims_and_avoids_front_forks(self):
        geometries = {name: cell.at(None, 2000 if name.startswith('left') else 1000)
                      for name,cell in CELLS.items()}
        focus, distance, vertical, _ = mixing_camera_spec(geometries)
        self.assertGreater(focus[2]+vertical, max(g['rim_z'] for g in geometries.values())+.02)
        self.assertLess(abs(distance)/vertical, .3)
        self.assertLess(distance, 0)  # Rear approach, not through the front forks.
        self.assertAlmostEqual(focus[0], .57)

    def test_inactive_bars_hold_the_last_orientation(self):
        class Op:
            value = None
            def Set(self, value): self.value = value
        op = Op()
        visual = {'bar_rotate': op}
        update_stirring_visuals(visual, 10, True)
        stopped = op.value
        update_stirring_visuals(visual, 11, False)
        self.assertEqual(op.value, stopped)


if __name__ == '__main__':
    unittest.main()

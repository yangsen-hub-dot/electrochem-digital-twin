"""Geometry/receipt tests, not real magnetic, transport or contact validation."""
import unittest
from microcell_labware import CELLS, BRIDGE_TIP_CLEARANCE_M
from salt_bridge_visuals import bridge_geometry, bridge_target, BRIDGE_RADIUS_M
from action_camera import focus_key


class SaltBridgeGeometryTests(unittest.TestCase):
    def test_both_ends_immerse_clear_of_electrodes_and_rest_on_holder(self):
        result = bridge_geometry(bridge_target(CELLS), CELLS,
                                 {'left_half_cell': 1996., 'right_half_cell': 999.})
        self.assertTrue(all(result['checks'].values()))
        self.assertGreater(result['immersed_depths_m']['right'], .001)
        self.assertAlmostEqual(result['grip_bottom_z_m'], result['holder_contact_z_m'])

    def test_offset_or_shallow_liquid_does_not_pass(self):
        target = list(bridge_target(CELLS))
        target[0] += .006
        result = bridge_geometry(target, CELLS, {'left_half_cell': 1996., 'right_half_cell': 999.})
        self.assertFalse(result['checks']['left_end_inside_cell'])
        result = bridge_geometry(bridge_target(CELLS), CELLS, {'left_half_cell': 100., 'right_half_cell': 999.})
        self.assertFalse(result['checks']['left_end_immersed'])

    def test_clearance_retains_margin_under_small_position_errors(self):
        target = bridge_target(CELLS)
        observed = (target[0] - .000134, target[1] + .000146, target[2] - .000128)
        volumes = {'left_half_cell': 1996., 'right_half_cell': 999.}
        self.assertTrue(all(bridge_geometry(observed, CELLS, volumes)['checks'].values()))
        too_low = (target[0], target[1], target[2] - .0015)
        self.assertFalse(bridge_geometry(too_low, CELLS, volumes)['checks']['stir_bars_clear'])

    def test_solid_displacement_changes_depth_not_inventory(self):
        cell = CELLS['left_half_cell']
        liquid = cell.at(None, 1000., electrode_inserted=True, bridge_inserted=True)
        depth = liquid['liquid_depth']
        area = 3.141592653589793 * cell.inner_radius**2
        recovered_volume = (area * depth - .0004 * .003 * (depth - .003)
                            - 3.141592653589793 * BRIDGE_RADIUS_M**2 * (depth - BRIDGE_TIP_CLEARANCE_M))
        self.assertAlmostEqual(recovered_volume, 1000e-9)

    def test_camera_follows_bridge_pick_and_connection(self):
        self.assertEqual(focus_key('pick_salt_bridge/align'), 'salt_bridge_rack')
        self.assertEqual(focus_key('connect_salt_bridge/align_and_insert'), 'salt_bridge_installation')


if __name__ == '__main__':
    unittest.main()

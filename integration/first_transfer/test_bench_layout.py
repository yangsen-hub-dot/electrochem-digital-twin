"""Layout contact and target consistency; not contact-force or reachability tests."""
import unittest
from bench_layout import (TOOL_BANK_CENTER_M, TOOL_BANK_SIZE_M, PIPETTE_STAND_XY,
                          PIPETTE_STAND_XY_BY_ID, COPPER_RACK_XY, BRIDGE_RACK_CENTER_M, PIPETTE_DOCK_EE_Z_M)
from electrode_visuals import PIPETTE_STAND_XY as MOTION_PIPETTE_XY
from salt_bridge_visuals import BRIDGE_RACK_CENTER_M as MOTION_BRIDGE_CENTER
from microcell_labware import CELLS
from tip_disposal_visuals import WASTE_XY


class BenchLayoutTests(unittest.TestCase):
    def test_shared_base_touches_table_and_contains_all_stations(self):
        self.assertAlmostEqual(TOOL_BANK_CENTER_M[2] - TOOL_BANK_SIZE_M[2] / 2, 0.)
        # Pipette fork outer X edge is +/-12 mm plus half its 3 mm width;
        # unlike the old single-tool fixture it does not have a 25 mm half-width.
        stations = [(xy, .0135) for xy in PIPETTE_STAND_XY_BY_ID.values()]
        stations += [(COPPER_RACK_XY, .025), (BRIDGE_RACK_CENTER_M[:2], .025)]
        for xy, half_width in stations:
            for axis in range(2):
                self.assertLess(abs(xy[axis] - TOOL_BANK_CENTER_M[axis]) + (half_width if axis == 0 else .025),
                                TOOL_BANK_SIZE_M[axis] / 2)

    def test_motion_and_visuals_share_station_constants(self):
        self.assertEqual(MOTION_PIPETTE_XY, PIPETTE_STAND_XY)
        self.assertEqual(MOTION_BRIDGE_CENTER, BRIDGE_RACK_CENTER_M)
        self.assertGreater(PIPETTE_DOCK_EE_Z_M - .088, .008)

    def test_reactor_and_edge_waste_stay_in_accepted_positions(self):
        self.assertEqual(CELLS['left_half_cell'].xy, (.56, -.16))
        self.assertEqual(CELLS['right_half_cell'].xy, (.58, -.16))
        self.assertEqual(WASTE_XY, (.40, -.32))
        self.assertLess(TOOL_BANK_CENTER_M[0] + TOOL_BANK_SIZE_M[0] / 2, .40 - .05)


if __name__ == '__main__':
    unittest.main()

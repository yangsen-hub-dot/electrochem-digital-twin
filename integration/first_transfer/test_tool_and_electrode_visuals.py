"""Deterministic geometry checks for the pipette grip and electrode seating."""

import unittest

from electrode_visuals import (
    ELECTRODE_CAP_M,
    ELECTRODE_INSERTION_M,
    ELECTRODE_PLATE_M,
    PIPETTE_DOCK_EE_Z_M,
    TIP_CLEARANCE_M,
    electrode_stack_geometry,
)
from microcell_labware import CELLS
from pipette_visuals import (
    PIPETTE_GRIP_DIAMETER_M,
    PIPETTE_GRIP_RADIUS_M,
    PIPETTE_LOWER_COLLAR_CENTER_Z_M,
    PIPETTE_LOWER_COLLAR_HEIGHT_M,
    pipette_grip_aligned,
    pipette_stand_contact_z,
)


class ToolAndElectrodeGeometryTests(unittest.TestCase):
    def test_pipette_grip_waist_matches_closed_franka_gap(self):
        self.assertAlmostEqual(PIPETTE_GRIP_RADIUS_M * 2,
                               PIPETTE_GRIP_DIAMETER_M)
        self.assertTrue(pipette_grip_aligned(.010))
        self.assertFalse(pipette_grip_aligned(.040))

    def test_pipette_parking_forks_touch_lower_collar_bottom(self):
        contact_z = pipette_stand_contact_z(PIPETTE_DOCK_EE_Z_M)
        collar_bottom = (PIPETTE_DOCK_EE_Z_M + PIPETTE_LOWER_COLLAR_CENTER_Z_M
                         - PIPETTE_LOWER_COLLAR_HEIGHT_M / 2)
        self.assertAlmostEqual(contact_z, collar_bottom, places=9)

    def test_working_plate_joins_cap_and_starts_above_pool_floor(self):
        for cell in CELLS.values():
            floor_z = cell.support_z + cell.floor_thickness
            cap_center_z = (floor_z + TIP_CLEARANCE_M + ELECTRODE_INSERTION_M
                            + ELECTRODE_CAP_M[2] / 2)
            stack = electrode_stack_geometry(cap_center_z)
            holder_top_z = stack['cap_bottom_z_m']
            self.assertAlmostEqual(stack['plate_top_z_m'], holder_top_z, places=9)
            self.assertAlmostEqual(stack['plate_bottom_z_m'] - floor_z,
                                   TIP_CLEARANCE_M, places=9)

    def test_working_plate_enters_one_and_two_ml_solutions(self):
        for cell in CELLS.values():
            floor_z = cell.support_z + cell.floor_thickness
            cap_center_z = (floor_z + TIP_CLEARANCE_M + ELECTRODE_INSERTION_M
                            + ELECTRODE_CAP_M[2] / 2)
            stack = electrode_stack_geometry(cap_center_z)
            for volume_ul in (1000.0, 2000.0):
                liquid = cell.at(None, volume_ul)
                immersed_depth = max(
                    0.0,
                    min(stack['plate_top_z_m'], liquid['surface_z'])
                    - stack['plate_bottom_z_m'])
                self.assertGreater(immersed_depth, .001)
                self.assertLessEqual(immersed_depth, ELECTRODE_PLATE_M[2])


if __name__ == '__main__':
    unittest.main()

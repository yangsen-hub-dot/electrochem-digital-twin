"""Geometry and controller-clearance checks for the project microcells."""
import unittest

from microcell_labware import CELLS


class MicrocellClearanceTests(unittest.TestCase):
    def test_controller_tolerance_is_separate_from_stricter_aperture_guard(self):
        for cell in CELLS.values():
            geometry = cell.at(None, 1000.0)
            self.assertGreater(geometry['position_tolerance_m'],
                               geometry['horizontal_tolerance_m'])
            self.assertGreater(geometry['surface_z'], geometry['floor_z'])
            self.assertLess(geometry['surface_z'], geometry['rim_z'])


if __name__ == '__main__':
    unittest.main()

"""Geometry/unit tests only; not calibration or collision validation."""
import unittest
from labware_presentation import VesselGeometry, copper_solution_blue


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.g = VesselGeometry((-.0264, -.03684, -.02494), (.02865,.02285,.02494))

    def test_liquid_stays_inside_and_height_uses_volume(self):
        a = self.g.at((.55,.14,.035), 5000)
        b = self.g.at((.55,.14,.035), 2500)
        self.assertLess(a['surface_z'], a['rim_z'])
        self.assertAlmostEqual(a['liquid_depth'], 2*b['liquid_depth'])
        self.assertLess(a['radius'], (.02865+.0264)*1.4/2)

    def test_empty_vessel_has_no_fake_fill(self):
        a = self.g.at((.55,-.16,.035), 0)
        self.assertEqual(a['liquid_depth'], 0)

    def test_operating_height_follows_asset_position(self):
        a = self.g.at((0,0,.035), 5000)
        b = self.g.at((0,0,.045), 5000)
        self.assertAlmostEqual(b['aspirate_ee_z']-a['aspirate_ee_z'], .01)
        self.assertLess(a['dispense_ee_z']-self.g.nozzle_offset_m, a['rim_z'])

    def test_impossible_volume_rejected(self):
        with self.assertRaises(ValueError):
            self.g.at((0,0,0), 1e9)

    def test_dilute_copper_solution_stays_visibly_blue(self):
        dilute = copper_solution_blue(.005)
        concentrated = copper_solution_blue(.1)
        self.assertGreater(dilute[2], dilute[0] * 2)
        self.assertGreater(dilute[1], dilute[0])
        self.assertGreater(concentrated[2], concentrated[0] * 2)
        self.assertGreater(dilute[0], concentrated[0])

    def test_invalid_concentration_rejected_for_display(self):
        with self.assertRaises(ValueError):
            copper_solution_blue(-.1)

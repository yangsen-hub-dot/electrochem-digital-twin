"""Pure geometry contract tests for the commercial-style tip-rack adapter."""
import unittest

from tip_rack_visuals import (
    RACK_COLUMNS, RACK_DEPTH_M, RACK_PITCH_M, RACK_ROWS, RACK_WIDTH_M,
    RACK_DOCK_EE_Z_M, RACK_TOP_Z_M, RACK_XY_M, tip_position,
    RACK_XY_BY_PIPETTE,
)
from pipette_visuals import (PIPETTE_NOZZLE_BOTTOM_OFFSET_M,
                              pipette_visual_profile)


class TipRackGeometryTests(unittest.TestCase):
    def test_96_slots_are_in_row_major_nine_mm_grid_inside_estimated_footprint(self):
        positions = [tip_position(i) for i in range(RACK_ROWS * RACK_COLUMNS)]
        self.assertEqual(len(set(positions)), 96)
        self.assertAlmostEqual(positions[1][0] - positions[0][0], RACK_PITCH_M)
        self.assertAlmostEqual(positions[RACK_COLUMNS][1] - positions[0][1], RACK_PITCH_M)
        self.assertAlmostEqual(sum(p[0] for p in positions) / len(positions), RACK_XY_M[0])
        self.assertAlmostEqual(sum(p[1] for p in positions) / len(positions), RACK_XY_M[1])
        self.assertLess(max(p[0] for p in positions) - min(p[0] for p in positions), RACK_WIDTH_M)
        self.assertLess(max(p[1] for p in positions) - min(p[1] for p in positions), RACK_DEPTH_M)

    def test_slot_bounds(self):
        with self.assertRaises(IndexError):
            tip_position(-1)
        with self.assertRaises(IndexError):
            tip_position(RACK_ROWS * RACK_COLUMNS)

    def test_nozzle_underside_meets_tip_top_at_positive_reachable_ee_height(self):
        self.assertAlmostEqual(
            RACK_DOCK_EE_Z_M + PIPETTE_NOZZLE_BOTTOM_OFFSET_M,
            RACK_TOP_Z_M)
        self.assertGreater(RACK_DOCK_EE_Z_M, RACK_TOP_Z_M)

    def test_p200_and_p1000_have_model_specific_tip_supply_positions(self):
        p200 = RACK_XY_BY_PIPETTE["gilson_pipetman_p200"]
        p1000 = RACK_XY_BY_PIPETTE["gilson_pipetman_p1000"]
        self.assertAlmostEqual(abs(p1000[0] - p200[0]), RACK_WIDTH_M + .022)
        self.assertEqual(pipette_visual_profile("gilson_pipetman_p200")["tip_id"],
                         "PIPETMAN_DIAMOND_D200")
        self.assertEqual(pipette_visual_profile("gilson_pipetman_p1000")["tip_id"],
                         "PIPETMAN_DIAMOND_D1200")

    def test_tip_length_tracks_the_recommended_tip_reference(self):
        self.assertAlmostEqual(
            pipette_visual_profile("gilson_pipetman_p200")["tip_length_m"], .051)
        self.assertAlmostEqual(
            pipette_visual_profile("gilson_pipetman_p1000")["tip_length_m"], .084)

    def test_unknown_pipette_cannot_fall_back_to_generic_tip_supply(self):
        with self.assertRaises(ValueError):
            pipette_visual_profile("unknown")


if __name__ == "__main__":
    unittest.main()

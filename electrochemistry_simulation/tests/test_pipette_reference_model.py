"""Continuous pipette range selection and reference interpolation tests."""

import json
import unittest
from pathlib import Path

from electrochem_sim.pipette_reference_model import (
    aliquots_for_volume,
    interpolate_anchor,
    reference_at_volume,
    select_pipette,
)


PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "error_profiles" / "gilson_pipetman_p200_p1000_continuous_v1.json"


class PipetteReferenceModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        cls.low = catalog["models"]["pipetting"]["continuous_commercial_pipette_reference"]["low"]

    def test_arbitrary_setpoints_interpolate_between_manufacturer_anchors(self):
        p200 = self.low["pipette_profiles"][0]["manufacturer_specification_anchors"]
        estimate = interpolate_anchor(p200, 50.0)
        self.assertEqual(estimate["evidence_status"], "linear_interpolation_between_manufacturer_anchors")
        self.assertAlmostEqual(estimate["systematic_error_limit_ul"], 0.6125)
        self.assertAlmostEqual(estimate["repeatability_sd_limit_ul"], 0.21875)

        estimate = reference_at_volume(self.low, 333.3)
        self.assertEqual(estimate["pipette_id"], "gilson_pipetman_p1000")
        self.assertAlmostEqual(estimate["systematic_error_limit_ul"], 3.58325)
        self.assertEqual(estimate["parameter_source_status"], "manufacturer_specification_reference_not_project_calibration")

    def test_every_anchor_and_interval_midpoint_follow_the_piecewise_linear_reference(self):
        for profile in self.low["pipette_profiles"]:
            anchors = profile["manufacturer_specification_anchors"]
            for anchor in anchors:
                with self.subTest(pipette=profile["pipette_id"], anchor=anchor["volume_ul"]):
                    result = interpolate_anchor(anchors, anchor["volume_ul"])
                    self.assertEqual(result["evidence_status"], "manufacturer_specification_anchor")
                    self.assertAlmostEqual(result["systematic_error_limit_ul"], anchor["systematic_error_limit_ul"])
                    self.assertAlmostEqual(result["repeatability_sd_limit_ul"], anchor["repeatability_sd_limit_ul"])
            for left, right in zip(anchors, anchors[1:]):
                midpoint = (left["volume_ul"] + right["volume_ul"]) / 2
                with self.subTest(pipette=profile["pipette_id"], midpoint=midpoint):
                    result = interpolate_anchor(anchors, midpoint)
                    self.assertEqual(
                        result["evidence_status"],
                        "linear_interpolation_between_manufacturer_anchors",
                    )
                    self.assertAlmostEqual(
                        result["systematic_error_limit_ul"],
                        (left["systematic_error_limit_ul"] + right["systematic_error_limit_ul"]) / 2,
                    )
                    self.assertAlmostEqual(
                        result["repeatability_sd_limit_ul"],
                        (left["repeatability_sd_limit_ul"] + right["repeatability_sd_limit_ul"]) / 2,
                    )

    def test_boundaries_and_overlap_choose_the_smaller_range_tool(self):
        profiles = self.low["pipette_profiles"]
        for volume, expected in ((20, "gilson_pipetman_p200"), (100, "gilson_pipetman_p200"),
                                 (200, "gilson_pipetman_p200"), (200.001, "gilson_pipetman_p1000"),
                                 (1000, "gilson_pipetman_p1000")):
            with self.subTest(volume=volume):
                self.assertEqual(select_pipette(profiles, volume)["pipette_id"], expected)

    def test_rejects_out_of_range_and_non_finite_values_without_extrapolation(self):
        profiles = self.low["pipette_profiles"]
        for volume in (19.999, 1000.001, float("nan"), float("inf")):
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                select_pipette(profiles, volume)
        with self.assertRaisesRegex(ValueError, "厂家参考锚点范围"):
            interpolate_anchor(profiles[0]["manufacturer_specification_anchors"], 19)

    def test_large_total_volume_splits_into_minimum_equal_in_range_aliquots(self):
        aliquots = aliquots_for_volume(2501.7)
        self.assertEqual(len(aliquots), 3)
        self.assertTrue(all(row["pipette_id"] == "gilson_pipetman_p1000" for row in aliquots))
        self.assertTrue(all(100 <= row["volume_ul"] <= 1000 for row in aliquots))
        self.assertAlmostEqual(sum(row["volume_ul"] for row in aliquots), 2501.7)

    def test_rejects_single_doses_below_combined_range(self):
        with self.assertRaisesRegex(ValueError, "小于 20 μL"):
            aliquots_for_volume(19.9)

    def test_dense_volume_sweep_keeps_every_aliquot_in_range_and_conserves_total(self):
        # A deterministic grid catches discontinuities around interpolation
        # anchors and the P200/P1000 handoff without implying calibration.
        totals = [20.0 + index * 0.25 for index in range(3921)]
        totals.extend((200.001, 1000.001, 1999.9, 2501.7, 10000.0))
        for total in totals:
            with self.subTest(total=total):
                aliquots = aliquots_for_volume(total)
                self.assertAlmostEqual(sum(row["volume_ul"] for row in aliquots), total, places=8)
                for row in aliquots:
                    reference = reference_at_volume(self.low, row["volume_ul"])
                    self.assertEqual(row["pipette_id"], reference["pipette_id"])
                    self.assertTrue(20.0 <= row["volume_ul"] <= 1000.0)
                    self.assertGreaterEqual(reference["systematic_error_limit_ul"], 0.0)
                    self.assertGreaterEqual(reference["repeatability_sd_limit_ul"], 0.0)


if __name__ == "__main__":
    unittest.main()

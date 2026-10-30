"""公开离子电导温度参考参数的范围与换算测试。"""

import unittest

from electrochem_sim.ion_transport_temperature import (
    ion_mobilities_from_temperature_reference,
    load_ion_conductivity_temperature_library,
)


class IonTransportTemperatureReferenceTests(unittest.TestCase):
    def test_reference_library_covers_all_copper_cell_and_bridge_ions(self):
        library = load_ion_conductivity_temperature_library()
        self.assertEqual(set(library["ions"]), {"Cu", "SO4", "K", "NO3"})
        self.assertEqual(library["temperature_model"]["valid_temperature_range_c"], [0.0, 95.0])
        self.assertFalse(library["data_policy"]["automatic_use"])

    def test_25_c_returns_25_c_anchor_and_explicit_reference_estimate(self):
        selection = ion_mobilities_from_temperature_reference(
            298.15,
            {"Cu", "SO4", "K", "NO3"},
        )
        self.assertEqual(selection["status"], "supported_reference_estimate")
        self.assertFalse(selection["condition_matched"])
        self.assertAlmostEqual(selection["temperature_c"], 25.0)
        self.assertEqual(selection["valid_temperature_range_c"], [0.0, 95.0])
        self.assertAlmostEqual(selection["temperature_scale_factor"], 1.0)
        self.assertAlmostEqual(selection["water_viscosity_pa_s"], 8.89996773678783e-4, places=10)
        for ion in selection["ions"].values():
            self.assertGreater(ion["mobility_magnitude_m2_v_s"], 0)
            self.assertTrue(ion["reference_ids"])
        self.assertTrue(any("iapws" in url.lower() for url in selection["source_urls"]))
        self.assertTrue(any("doi.org/10.1016/j.cplett.2018.03.063" in url.lower() for url in selection["source_urls"]))

    def test_temperature_reference_covers_wide_range_and_refuses_outside(self):
        for temperature_c in (0.0, 10.0, 25.0, 50.0, 80.0, 95.0):
            selection = ion_mobilities_from_temperature_reference(
                temperature_c + 273.15, {"Cu", "SO4", "K", "NO3"}
            )
            self.assertEqual(selection["status"], "supported_reference_estimate")
            self.assertGreater(selection["water_viscosity_pa_s"], 0)
            self.assertGreater(selection["temperature_scale_factor"], 0)
        for temperature_k in (273.149, 368.151):
            with self.subTest(temperature_k=temperature_k), self.assertRaisesRegex(
                ValueError, "不进行范围外外推"
            ):
                ion_mobilities_from_temperature_reference(temperature_k, {"Cu", "SO4", "K", "NO3"})

    def test_temperature_reference_refuses_unknown_ion(self):
        with self.assertRaisesRegex(ValueError, "缺少离子 X"):
            ion_mobilities_from_temperature_reference(298.25, {"X"})


if __name__ == "__main__":
    unittest.main()

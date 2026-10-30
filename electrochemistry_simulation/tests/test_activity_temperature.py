import unittest

from electrochem_sim.activity_temperature import davies_A_at_temperature


class DaviesTemperatureParameterTests(unittest.TestCase):
    def test_published_values_are_available_at_source_temperatures(self):
        at_25c = davies_A_at_temperature(298.15)
        at_30c = davies_A_at_temperature(303.15)
        self.assertEqual(at_25c["status"], "supported_exact")
        self.assertEqual(at_25c["A"], 0.509)
        self.assertEqual(at_30c["status"], "supported_exact")
        self.assertEqual(at_30c["A"], 0.513)
        self.assertEqual(at_30c["source_id"], "oecd_nea_tdb_2020")

    def test_temperature_between_source_points_is_bounded_linear_interpolation(self):
        selection = davies_A_at_temperature(300.65)
        self.assertEqual(selection["status"], "supported_interpolated")
        self.assertEqual(selection["method"], "linear_interpolation_between_published_values")
        self.assertAlmostEqual(selection["A"], 0.511)
        self.assertEqual(selection["interpolation_interval_c"], [25.0, 30.0])

    def test_outside_source_range_is_not_extrapolated(self):
        for temperature_k in (272.15, 323.151):
            with self.subTest(temperature_k=temperature_k):
                with self.assertRaisesRegex(ValueError, "不进行范围外外推"):
                    davies_A_at_temperature(temperature_k)


if __name__ == "__main__":
    unittest.main()

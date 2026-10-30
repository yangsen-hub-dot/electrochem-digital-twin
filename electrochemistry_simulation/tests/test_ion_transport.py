"""离子迁移参数参考库和 Einstein/Nernst–Einstein 换算核验。"""

import unittest
from pathlib import Path

from electrochem_sim.ion_transport import (
    load_electrolyte_transport_database,
    load_ion_transport_library,
    mobility_magnitude_from_diffusion,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_PATH = (
    PROJECT_ROOT
    / "config"
    / "ion_transport"
    / "aqueous_ion_diffusion_infinite_dilution_25c_v1.json"
)
FINITE_CONCENTRATION_DATABASE_PATH = (
    PROJECT_ROOT
    / "config"
    / "ion_transport"
    / "aqueous_electrolyte_transport_finite_concentration_v1.json"
)


class IonTransportLibraryTests(unittest.TestCase):
    def test_reference_library_has_current_cell_and_bridge_ions(self):
        library = load_ion_transport_library(LIBRARY_PATH)
        self.assertEqual(library["reference_concentration_state"], "infinite_dilution_limit")
        self.assertEqual(set(library["ions"]), {"Cu", "SO4", "K", "NO3"})
        for entry in library["ions"].values():
            self.assertEqual(entry["reference_temperature_k"], 298.15)
            self.assertTrue(entry["reference_ids"])

    def test_mobility_uses_charge_magnitude_and_reference_temperature(self):
        copper = mobility_magnitude_from_diffusion(7.14e-10, 2, 298.15)
        sulfate = mobility_magnitude_from_diffusion(1.065e-9, -2, 298.15)
        self.assertAlmostEqual(copper, 5.558025e-8, delta=1e-13)
        self.assertAlmostEqual(sulfate, 8.290332e-8, delta=1e-13)

    def test_invalid_mobility_inputs_are_rejected(self):
        invalid_cases = [
            (0, 1, 298.15),
            (1e-9, 0, 298.15),
            (1e-9, 1, 0),
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                mobility_magnitude_from_diffusion(*values)

    def test_finite_concentration_database_keeps_parameter_types_separate(self):
        database = load_electrolyte_transport_database(FINITE_CONCENTRATION_DATABASE_PATH)
        datasets = {entry["dataset_id"]: entry for entry in database["datasets"]}
        self.assertIn(
            "binary_electrolyte_mutual_diffusion_coefficient",
            {entry["property_type"] for entry in database["datasets"]},
        )
        self.assertEqual(
            datasets["kno3_cation_transference_number_25c_v1"]["points"][2]["value"],
            0.5103,
        )
        copper_transference = datasets["cuso4_apparent_cation_transference_number_v1"]
        copper_01m_25c = next(
            point for point in copper_transference["points"]
            if point["concentration_mol_kg"] == 0.1 and point["temperature_c"] == 25
        )
        self.assertEqual(copper_01m_25c["value"], 0.3255)
        diffusion = datasets["binary_aqueous_electrolyte_mutual_diffusion_table_2_5_v1"]
        kno3 = next(series for series in diffusion["series"] if series["electrolyte_formula"] == "KNO3")
        self.assertEqual(kno3["values_1e-9_m2_s"][-1], 1.846)
        self.assertFalse(database["data_policy"]["automatic_liquid_junction_use"])

    def test_config_references_library_without_enabling_junction_correction(self):
        import json

        config_path = PROJECT_ROOT / "config" / "copper_concentration_cell.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        junction = config["cell"]["liquid_junction_potential"]
        self.assertFalse(junction["enabled"])
        self.assertTrue((PROJECT_ROOT / "config" / junction["transport_parameter_library"]).is_file())
        self.assertNotIn("required_ion_ids", junction)
        self.assertEqual(set(junction["solution_ions_by_side"]["left"]), {"Cu", "SO4"})
        self.assertEqual(
            set(junction["bridge_electrolyte"]["electrolytes"][0]["ion_stoichiometry"]),
            {"K", "NO3"},
        )


if __name__ == "__main__":
    unittest.main()

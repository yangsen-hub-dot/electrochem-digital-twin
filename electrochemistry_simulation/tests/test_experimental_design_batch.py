"""Tests for user-selectable condition-factor batch matrices."""

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from electrochem_sim.experimental_design_batch import run_experimental_design_batch
from electrochem_sim.simulator import PROJECT_ROOT, load_config


BASE_CONFIG_PATH = PROJECT_ROOT / "config" / "copper_concentration_cell.json"


class ExperimentalDesignBatchTests(unittest.TestCase):
    def setUp(self):
        self.base_config = load_config(BASE_CONFIG_PATH)
        self.settings = {
            "batch_name_short_zh": "条件矩阵测试",
            "temperatures_k": [298.15],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
            "salt_bridge_types": ["KNO3"],
            "salt_bridge_concentrations_m": [0.1],
            "activity_correction_enabled": [True],
            "liquid_junction_enabled": [False],
            "liquid_junction_models": ["henderson_approximation"],
            "errors_enabled": [False],
            "operation_error_levels": {
                "pipetting": ["low"],
                "tip_carryover": ["low"],
                "mixing": ["low"],
            },
            "replicates": 1,
            "max_scenarios": 500,
        }

    def test_default_template_runs_temperature_and_concentration_matrix(self):
        config_path = PROJECT_ROOT / "config" / "copper_experimental_design_sweep.json"
        with config_path.open(encoding="utf-8") as stream:
            settings = json.load(stream)
        with TemporaryDirectory() as directory:
            summary_path, rows = run_experimental_design_batch(
                self.base_config, settings, Path(directory), seed=31
            )
            with summary_path.open(encoding="utf-8-sig", newline="") as stream:
                saved = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 15)
        self.assertEqual(len(saved), 15)
        self.assertTrue(all(row["scenario_status"] == "completed" for row in rows))
        self.assertEqual({row["salt_bridge_type"] for row in rows}, {"KNO3"})
        self.assertEqual({row["activity_correction_enabled"] for row in rows}, {True})
        self.assertEqual({row["errors_enabled"] for row in rows}, {False})

    def test_selected_bridge_corrections_error_tier_and_replicates_are_applied(self):
        settings = {
            **self.settings,
            "salt_bridge_types": ["KNO3", "NaNO3"],
            "activity_correction_enabled": [False, True],
            "errors_enabled": [False, True],
            "operation_error_levels": {
                "pipetting": ["low", "high"],
                "tip_carryover": ["medium"],
                "mixing": ["low"],
            },
            "replicates": 2,
        }
        # 2 bridges × 2 activity settings × 2 error settings × 2 pipetting levels × 2 repeats.
        with TemporaryDirectory() as directory:
            summary_path, rows = run_experimental_design_batch(
                self.base_config, settings, Path(directory), seed=50
            )
            with summary_path.open(encoding="utf-8-sig", newline="") as stream:
                saved = list(csv.DictReader(stream))
            details = [json.loads((Path(directory) / row["detail_file"]).read_text(encoding="utf-8")) for row in rows]

        self.assertEqual(len(rows), 32)
        self.assertEqual(len(saved), 32)
        self.assertEqual({row["salt_bridge_type"] for row in rows}, {"KNO3", "NaNO3"})
        self.assertEqual({row["activity_correction_enabled"] for row in rows}, {False, True})
        self.assertEqual({row["errors_enabled"] for row in rows}, {False, True})
        self.assertEqual({row["pipetting_level"] for row in rows}, {"low", "high"})
        self.assertEqual({row["tip_carryover_level"] for row in rows}, {"medium"})
        self.assertEqual({row["replicate"] for row in rows}, {1, 2})
        self.assertEqual(len({row["run_id"] for row in rows}), len(rows))
        self.assertTrue(all(detail["config"]["cell_setup"]["salt_bridge"]["type"] in {"KNO3", "NaNO3"} for detail in details))
        for row, detail in zip(rows, details):
            condition_inputs = detail["config"]["condition_inputs"]
            self.assertAlmostEqual(condition_inputs["left_concentration_m"], row["target_c_left_m"])
            expected_right_concentration = (
                row["target_c_right_m"] if row["target_c_right_m"] is not None else 0.1
            )
            self.assertAlmostEqual(condition_inputs["right_concentration_m"], expected_right_concentration)
            self.assertAlmostEqual(condition_inputs["left_volume_ml"], 1.0)
            self.assertAlmostEqual(condition_inputs["right_volume_ml"], 1.0)
        self.assertTrue(all(row["conservation_passed"] for row in rows))

    def test_matrix_size_guard_prevents_accidental_full_factorial_run(self):
        settings = {
            **self.settings,
            "temperatures_k": [293.15, 298.15],
            "left_concentrations_m": [0.01, 0.02],
            "salt_bridge_types": ["KNO3", "KCl", "NaNO3"],
            "errors_enabled": [False, True],
            "operation_error_levels": {
                "pipetting": ["low", "medium", "high"],
                "tip_carryover": ["low", "medium", "high"],
                "mixing": ["low", "medium", "high"],
            },
            "max_scenarios": 100,
        }
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "超过 max_scenarios"):
                run_experimental_design_batch(self.base_config, settings, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

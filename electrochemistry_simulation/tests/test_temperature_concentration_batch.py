import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from electrochem_sim.simulator import PROJECT_ROOT, load_config, run_experiment
from electrochem_sim.temperature_concentration_batch import (
    make_scenario_config,
    run_batch,
)


BASE_CONFIG_PATH = PROJECT_ROOT / "config" / "copper_concentration_cell.json"


class TemperatureConcentrationBatchTests(unittest.TestCase):
    def setUp(self):
        self.base_config = load_config(BASE_CONFIG_PATH)

    def make_case(self, *, temperature_k=298.15, left_m=0.01, right_m=None):
        return make_scenario_config(
            self.base_config,
            temperature_k=temperature_k,
            left_concentration_m=left_m,
            left_total_volume_ml=1.0,
            stock_step_id="02_aspirate_low_stock",
            water_step_id="06_aspirate_water",
            right_concentration_m=right_m,
            right_total_volume_ml=1.0 if right_m is not None else None,
        )

    def test_right_concentration_defaults_to_base_protocol_value(self):
        case = self.make_case()
        result = run_experiment(case)
        self.assertTrue(result["prediction"]["valid"])
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.1)
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(case["temperature_k"], 298.15)
        self.assertEqual(case["condition_inputs"]["left_concentration_m"], 0.01)
        self.assertEqual(case["condition_inputs"]["right_concentration_m"], 0.1)
        self.assertEqual(case["condition_inputs"]["left_volume_ml"], 1.0)
        self.assertEqual(case["condition_inputs"]["right_volume_ml"], 1.0)

    def test_optional_right_concentration_rebuilds_right_solution(self):
        case = self.make_case(left_m=0.01, right_m=0.04)
        result = run_experiment(case)
        self.assertEqual(result["status"], "completed")
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.04)
        self.assertAlmostEqual(
            result["final_state"]["containers"]["right_half_cell"]["volume_l"], 0.001
        )
        self.assertTrue(result["conservation"]["passed"])

    def test_right_grid_keeps_base_protocol_when_target_matches_base(self):
        case = self.make_case(left_m=0.01, right_m=0.1)
        base_protocol = self.base_config["protocol"]
        left_index = next(i for i, step in enumerate(base_protocol) if step.get("step_id") == "09_mix_left")
        right_index = next(i for i, step in enumerate(base_protocol) if step.get("step_id") == "16_mix_right")
        case_left_index = next(i for i, step in enumerate(case["protocol"]) if step.get("step_id") == "09_mix_left")
        case_right_index = next(i for i, step in enumerate(case["protocol"]) if step.get("step_id") == "16_mix_right")
        self.assertEqual(
            case["protocol"][case_left_index + 1:case_right_index],
            base_protocol[left_index + 1:right_index],
        )

    def test_each_case_uses_an_independent_configuration_copy(self):
        original_temperature = self.base_config["temperature_k"]
        original_protocol = json.dumps(self.base_config["protocol"], sort_keys=True)
        first = self.make_case(temperature_k=298.15, left_m=0.01, right_m=0.04)
        second = self.make_case(temperature_k=303.15, left_m=0.02, right_m=0.08)
        self.assertIsNot(first, second)
        self.assertIsNot(first["protocol"], second["protocol"])
        self.assertEqual(self.base_config["temperature_k"], original_temperature)
        self.assertEqual(json.dumps(self.base_config["protocol"], sort_keys=True), original_protocol)
        self.assertEqual(first["temperature_k"], 298.15)
        self.assertEqual(second["temperature_k"], 303.15)
        self.assertAlmostEqual(self.base_config["cell"]["activity_correction"]["A"], 0.509)
        self.assertAlmostEqual(first["cell"]["activity_correction"]["A"], 0.509)
        self.assertAlmostEqual(second["cell"]["activity_correction"]["A"], 0.513)
        first_result = run_experiment(first)
        second_result = run_experiment(second)
        self.assertAlmostEqual(first_result["prediction"]["c_right_m"], 0.04)
        self.assertAlmostEqual(second_result["prediction"]["c_right_m"], 0.08)
        self.assertAlmostEqual(first_result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(second_result["prediction"]["c_left_m"], 0.02)

    def test_default_temperature_grid_has_source_supported_davies_parameters(self):
        batch_path = PROJECT_ROOT / "config" / "copper_temperature_concentration_sweep.json"
        with batch_path.open(encoding="utf-8") as stream:
            settings = json.load(stream)
        self.assertEqual(settings["temperatures_k"], [293.15, 298.15, 303.15, 308.15, 313.15])
        self.assertTrue(all(273.15 <= temperature <= 323.15 for temperature in settings["temperatures_k"]))

    def test_batch_uses_temperature_dependent_davies_A(self):
        settings = {
            "batch_name_short_zh": "铜电池_批量测试",
            "temperatures_k": [298.15, 303.15],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            summary_path, rows = run_batch(self.base_config, settings, output_dir)
            self.assertTrue(summary_path.name.startswith("批_铜电池_批量测试_"))
            self.assertRegex(summary_path.parent.name, r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(len(rows), 2)
            by_temp = {row["temperature_k"]: row for row in rows}
            matched = by_temp[298.15]
            warmed = by_temp[303.15]
            self.assertEqual(matched["scenario_status"], "completed")
            self.assertIsNotNone(matched["final_theoretical_voltage_mv"])
            self.assertEqual(warmed["scenario_status"], "completed")
            self.assertEqual(warmed["activity_status"], "estimated")
            self.assertAlmostEqual(warmed["activity_parameter_a"], 0.513)
            self.assertEqual(warmed["activity_parameter_status"], "supported_exact")
            self.assertEqual(warmed["activity_parameter_method"], "published_tabulated_value")
            self.assertIn("来源表值", warmed["temperature_parameter_status"])
            self.assertIsNotNone(warmed["final_theoretical_voltage_mv"])
            self.assertTrue(all(row["conservation_passed"] for row in rows))
            with summary_path.open(encoding="utf-8-sig", newline="") as stream:
                saved_rows = list(csv.DictReader(stream))
            self.assertEqual(len(saved_rows), 2)
            for row in rows:
                detail_path = output_dir / row["detail_file"]
                self.assertTrue(detail_path.is_file())
                with detail_path.open(encoding="utf-8") as stream:
                    detail = json.load(stream)
                self.assertEqual(detail["run_id"], row["run_id"])
                self.assertIn("temperature_parameter_selection", detail)
                condition_inputs = detail["config"]["condition_inputs"]
                self.assertAlmostEqual(condition_inputs["left_concentration_m"], row["target_c_left_m"])
                self.assertAlmostEqual(condition_inputs["right_concentration_m"], row["target_c_right_m"])
                self.assertAlmostEqual(condition_inputs["left_volume_ml"], 1.0)
                self.assertAlmostEqual(condition_inputs["right_volume_ml"], 1.0)

    def test_temperature_outside_davies_table_is_rejected_without_extrapolation(self):
        settings = {
            "batch_name_short_zh": "超范围温度测试",
            "temperatures_k": [330.15],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            _, rows = run_batch(self.base_config, settings, Path(temp_dir))
        row = rows[0]
        self.assertEqual(row["activity_parameter_status"], "unsupported_temperature")
        self.assertEqual(row["activity_status"], "invalid_parameters")
        self.assertIn("不进行范围外外推", row["temperature_parameter_status"])
        self.assertEqual(row["scenario_status"], "protocol_completed_prediction_rejected")
        self.assertIsNotNone(row["ideal_nernst_mv"])
        self.assertIsNone(row["final_theoretical_voltage_mv"])
        self.assertTrue(row["conservation_passed"])

    def test_optional_right_grid_creates_cartesian_product(self):
        settings = {
            "batch_name_short_zh": "铜电池_左右扫描测试",
            "temperatures_k": [298.15],
            "left_concentrations_m": [0.01, 0.02],
            "right_concentrations_m": [0.04, 0.08],
            "left_total_volume_ml": 1.0,
            "right_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            summary_path, rows = run_batch(self.base_config, settings, Path(temp_dir))
            self.assertEqual(len(rows), 4)
            self.assertEqual({row["target_c_left_m"] for row in rows}, {0.01, 0.02})
            self.assertEqual({row["target_c_right_m"] for row in rows}, {0.04, 0.08})
            self.assertTrue(all(row["scenario_status"] == "completed" for row in rows))
            self.assertTrue(summary_path.is_file())

    def test_liquid_junction_uses_marked_temperature_reference_estimate(self):
        base_config = load_config(BASE_CONFIG_PATH)
        base_config["cell"]["liquid_junction_potential"].update(
            enabled=True,
            parameter_use_policy="allow_reference_estimate",
        )
        settings = {
            "batch_name_short_zh": "液接温度测试",
            "temperatures_k": [303.15],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            _, rows = run_batch(base_config, settings, Path(temp_dir))
        row = rows[0]
        self.assertEqual(row["liquid_junction_status"], "estimated_from_reference_parameters")
        self.assertEqual(row["liquid_junction_temperature_parameter_method"], "walden_water_viscosity_ratio_reference_v1")
        self.assertEqual(row["liquid_junction_temperature_parameter_range_c"], "0.0;95.0")
        self.assertIn("水黏度比例近似范围 0–95 °C", row["temperature_parameter_status"])
        self.assertEqual(row["activity_status"], "estimated")
        self.assertAlmostEqual(row["activity_parameter_a"], 0.513)
        self.assertIsNotNone(row["liquid_junction_correction_mv"])
        self.assertTrue(row["conservation_passed"])

    def test_25_1_c_is_simulated_with_activity_and_explicit_ljp_reference_estimate(self):
        base_config = load_config(BASE_CONFIG_PATH)
        base_config["cell"]["liquid_junction_potential"].update(
            enabled=True,
            parameter_use_policy="allow_reference_estimate",
        )
        settings = {
            "batch_name_short_zh": "25点1度批量测试",
            "temperatures_k": [298.25],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            summary_path, rows = run_batch(base_config, settings, Path(temp_dir))
            with summary_path.open(encoding="utf-8-sig", newline="") as stream:
                saved_row = next(csv.DictReader(stream))
        row = rows[0]
        self.assertEqual(row["scenario_status"], "completed")
        self.assertEqual(row["temperature_k"], 298.25)
        self.assertEqual(row["activity_parameter_status"], "supported_interpolated")
        self.assertEqual(row["liquid_junction_status"], "estimated_from_reference_parameters")
        self.assertEqual(row["liquid_junction_model"], "henderson_approximation")
        self.assertTrue(row["liquid_junction_enabled"])
        self.assertIsNotNone(row["liquid_junction_correction_mv"])
        self.assertAlmostEqual(row["liquid_junction_henderson_mv"], row["liquid_junction_correction_mv"])
        self.assertAlmostEqual(row["liquid_junction_nernst_planck_mv"], row["liquid_junction_correction_mv"], places=6)
        self.assertLess(abs(row["liquid_junction_model_difference_mv"]), 1e-6)
        self.assertIn("连续/自由扩散液接", row["liquid_junction_henderson_scope_zh"])
        self.assertIn("局部电中性", row["liquid_junction_nernst_planck_scope_zh"])
        self.assertIn("只核对数值积分", row["liquid_junction_model_comparison_note"])
        self.assertIsNotNone(row["final_theoretical_voltage_mv"])
        self.assertTrue(row["conservation_passed"])
        self.assertEqual(saved_row["liquid_junction_model_name_zh"], "Henderson 近似")
        self.assertAlmostEqual(float(saved_row["liquid_junction_henderson_mv"]), row["liquid_junction_henderson_mv"])
        self.assertAlmostEqual(float(saved_row["liquid_junction_nernst_planck_mv"]), row["liquid_junction_nernst_planck_mv"])
        self.assertAlmostEqual(float(saved_row["liquid_junction_model_difference_mv"]), row["liquid_junction_model_difference_mv"])

    def test_ljp_temperature_estimate_requires_explicit_reference_policy(self):
        base_config = load_config(BASE_CONFIG_PATH)
        base_config["cell"]["liquid_junction_potential"]["enabled"] = True
        base_config["cell"]["liquid_junction_potential"]["parameter_use_policy"] = "condition_matched_only"
        settings = {
            "batch_name_short_zh": "未允许液接参考估算测试",
            "temperatures_k": [298.25],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            _, rows = run_batch(base_config, settings, Path(temp_dir))
        row = rows[0]
        self.assertEqual(row["liquid_junction_status"], "awaiting_condition_matched_parameters")
        self.assertIsNone(row["liquid_junction_correction_mv"])
        self.assertTrue(row["conservation_passed"])

    def test_liquid_junction_temperature_estimate_supports_35_c_with_reference_model(self):
        base_config = load_config(BASE_CONFIG_PATH)
        base_config["cell"]["liquid_junction_potential"].update(
            enabled=True,
            parameter_use_policy="allow_reference_estimate",
        )
        settings = {
            "batch_name_short_zh": "超出液接参考温度测试",
            "temperatures_k": [308.15],
            "left_concentrations_m": [0.01],
            "left_total_volume_ml": 1.0,
            "stock_aspirate_step_id": "02_aspirate_low_stock",
            "water_aspirate_step_id": "06_aspirate_water",
        }
        with TemporaryDirectory() as temp_dir:
            _, rows = run_batch(base_config, settings, Path(temp_dir))
        row = rows[0]
        self.assertEqual(row["liquid_junction_status"], "estimated_from_reference_parameters")
        self.assertEqual(
            row["liquid_junction_temperature_parameter_method"],
            "walden_water_viscosity_ratio_reference_v1",
        )
        self.assertEqual(row["liquid_junction_temperature_parameter_range_c"], "0.0;95.0")
        self.assertIsNotNone(row["liquid_junction_correction_mv"])
        self.assertEqual(row["activity_status"], "estimated")
        self.assertTrue(row["conservation_passed"])


if __name__ == "__main__":
    unittest.main()

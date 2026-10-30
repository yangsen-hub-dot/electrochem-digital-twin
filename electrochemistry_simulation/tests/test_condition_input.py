"""Tests for UI-supplied, one-run experiment conditions."""

import json
import unittest
from pathlib import Path

from electrochem_sim.condition_input import build_condition_config
from electrochem_sim.simulator import load_config, run_experiment


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "copper_concentration_cell.json"


def conditions(**changes):
    payload = {
        "left_concentration_m": 0.01,
        "right_concentration_m": 0.1,
        "left_volume_ml": 1.0,
        "right_volume_ml": 1.0,
        "temperature_c": 25.0,
        "salt_bridge_type": "KNO3",
        "salt_bridge_concentration_m": 0.1,
        "activity_correction_enabled": True,
        "liquid_junction_enabled": False,
        "errors_enabled": False,
    }
    payload.update(changes)
    return payload


class ConditionInputTests(unittest.TestCase):
    def test_reference_consent_is_persisted_but_strict_override_is_respected(self):
        base=load_config(CONFIG_PATH)
        allowed=build_condition_config(base,conditions(liquid_junction_enabled=True))
        self.assertEqual(allowed['condition_inputs']['liquid_junction_parameter_use_policy'],'allow_reference_estimate')
        strict=build_condition_config(base,conditions(liquid_junction_enabled=True,
                     liquid_junction_parameter_use_policy='condition_matched_only'))
        result=run_experiment(strict,seed=0)['prediction']
        self.assertFalse(result['valid'])
        self.assertIn('allow_reference_estimate',result['reason'])

    def setUp(self):
        self.config = load_config(CONFIG_PATH)
        self.original = json.loads(json.dumps(self.config))

    def test_builds_a_fresh_protocol_and_preserves_the_loaded_config(self):
        candidate = build_condition_config(self.config, conditions(left_concentration_m=0.005))
        result = run_experiment(candidate, seed=3)
        self.assertEqual(result["status"], "completed")
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.005)
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.1)
        self.assertTrue(result["conservation"]["passed"])
        self.assertEqual(result["final_state"]["completed_steps"], len(candidate["protocol"]))
        self.assertEqual(self.config, self.original)
        self.assertNotIn("condition_inputs", self.config)

    def test_accepts_common_bridge_choices_and_a_temperature_without_inapplicable_corrections(self):
        for bridge_type in ("KNO3", "KCl", "NaNO3"):
            with self.subTest(bridge_type=bridge_type):
                candidate = build_condition_config(self.config, conditions(
                    temperature_c=30.0,
                    salt_bridge_type=bridge_type,
                    activity_correction_enabled=False,
                ))
                result = run_experiment(candidate, seed=2)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["config"]["cell_setup"]["salt_bridge"]["type"], bridge_type)
                self.assertAlmostEqual(result["final_state"]["temperature_k"], 303.15)

    def test_activity_enabled_ui_input_uses_temperature_matched_davies_parameter(self):
        candidate = build_condition_config(self.config, conditions(temperature_c=30.0))
        activity = candidate["cell"]["activity_correction"]
        self.assertAlmostEqual(activity["A"], 0.513)
        self.assertAlmostEqual(activity["A_reference_temperature_k"], 303.15)
        self.assertEqual(
            activity["condition_temperature_parameter_selection"]["status"],
            "supported_exact",
        )
        result = run_experiment(candidate, seed=3)
        self.assertEqual(result["status"], "completed")

    def test_activity_enabled_ui_input_rejects_temperature_outside_davies_table(self):
        with self.assertRaisesRegex(ValueError, "0–50 °C"):
            build_condition_config(self.config, conditions(temperature_c=55.0))

    def test_rejects_zero_volume_and_concentration_above_stock(self):
        for payload, message in (
            (conditions(left_volume_ml=0), "体积"),
            (conditions(right_concentration_m=0.2), "母液浓度"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    build_condition_config(self.config, payload)

    def test_liquid_junction_support_is_reported_by_model_not_guessed(self):
        candidate = build_condition_config(self.config, conditions(
            salt_bridge_type="NaNO3",
            liquid_junction_enabled=True,
        ))
        result = run_experiment(candidate, seed=2)
        junction = result["prediction"]["liquid_junction_potential"]
        self.assertNotEqual(junction["status"], "calculated")
        self.assertIsNone(junction["correction_mv"])

    def test_liquid_junction_model_can_be_selected_and_is_recorded_in_the_run_config(self):
        model_id = "nernst_planck_zero_current_linear_profile"
        candidate = build_condition_config(self.config, conditions(
            liquid_junction_enabled=True,
            liquid_junction_model=model_id,
        ))
        self.assertEqual(candidate["cell"]["liquid_junction_potential"]["model"], model_id)
        self.assertEqual(candidate["condition_inputs"]["liquid_junction_model"], model_id)

        with self.assertRaisesRegex(ValueError, "液接电势模型"):
            build_condition_config(self.config, conditions(liquid_junction_model="unsupported"))

    def test_operation_error_levels_select_the_matching_profile_parameters(self):
        candidate = build_condition_config(self.config, conditions(
            errors_enabled=True,
            operation_error_levels={
                "pipetting": "medium",
                "tip_carryover": "high",
                "mixing": "medium",
            },
        ))
        factors = candidate["error_model"]["factors"]
        self.assertEqual(factors["pipetting"]["level"], "medium")
        self.assertEqual(factors["tip_carryover"]["level"], "high")
        self.assertEqual(factors["mixing"]["level"], "medium")
        self.assertEqual(factors["pipetting"]["parameters"]["scenario_multiplier"], 1.5)
        self.assertEqual(len(factors["pipetting"]["parameters"]["pipette_profiles"]), 2)
        self.assertEqual(factors["tip_carryover"]["parameters"]["residual_volume_ul_range"], [0.6, 1.0])
        self.assertEqual(factors["mixing"]["parameters"]["rate_range_s_inv"], [0.230259, 0.391202])
        result = run_experiment(candidate, seed=7)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_model_realized"]["pipetting"]["level"], "medium")

    def test_arbitrary_transfer_volumes_use_p200_and_p1000_with_explicit_pipette_ids(self):
        candidate = build_condition_config(self.config, conditions(
            left_concentration_m=0.03333,
            left_volume_ml=1.0,
        ))
        aspirates = [step for step in candidate["protocol"] if step["action"] == "aspirate"]
        by_step = {step["step_id"]: step for step in aspirates}
        self.assertAlmostEqual(by_step["ui_left_stock_aspirate_01"]["volume_ul"], 333.3)
        self.assertEqual(by_step["ui_left_stock_aspirate_01"]["pipette_id"], "gilson_pipetman_p1000")
        self.assertAlmostEqual(by_step["ui_left_water_aspirate_01"]["volume_ul"], 666.7)
        self.assertEqual(by_step["ui_left_water_aspirate_01"]["pipette_id"], "gilson_pipetman_p1000")
        dispenses = {step["step_id"]: step for step in candidate["protocol"] if step["action"] == "dispense"}
        self.assertEqual(dispenses["ui_left_stock_dispense_01"]["pipette_id"], "gilson_pipetman_p1000")

    def test_small_stock_dose_uses_p200_and_values_below_range_are_rejected(self):
        candidate = build_condition_config(self.config, conditions(left_concentration_m=0.0025))
        stock = next(step for step in candidate["protocol"] if step["step_id"] == "ui_left_stock_aspirate_01")
        self.assertAlmostEqual(stock["volume_ul"], 25.0)
        self.assertEqual(stock["pipette_id"], "gilson_pipetman_p200")
        with self.assertRaisesRegex(ValueError, "小于 20 μL"):
            build_condition_config(self.config, conditions(left_concentration_m=0.001))

    def test_large_dose_is_split_and_continuous_model_is_applied_per_aspiration(self):
        candidate = build_condition_config(self.config, conditions(
            left_concentration_m=0.099,
            left_volume_ml=2.0,
            errors_enabled=True,
        ))
        aspirates = [step for step in candidate["protocol"] if step["action"] == "aspirate" and step["step_id"].startswith("ui_left_stock")]
        self.assertEqual(len(aspirates), 2)
        self.assertTrue(all(abs(step["volume_ul"] - 990.0) < 1e-9 for step in aspirates))
        self.assertTrue(all(step["pipette_id"] == "gilson_pipetman_p1000" for step in aspirates))
        # Total aliquots may exceed the single-draw maximum; each aspiration may not.
        water = [step for step in candidate["protocol"] if step["action"] == "aspirate" and step["step_id"].startswith("ui_left_water")]
        self.assertEqual(len(water), 1)
        self.assertTrue(all(20 <= step["volume_ul"] <= 1000 for step in water))
        result = run_experiment(candidate, seed=41)
        self.assertEqual(result["status"], "completed")
        sampled = result["error_model_realized"]["pipetting"]["sampled_continuous_actions"]
        self.assertIn("ui_left_stock_aspirate_01", sampled)
        self.assertEqual(sampled["ui_left_stock_aspirate_01"]["pipette_id"], "gilson_pipetman_p1000")

    def test_rejects_unknown_operation_error_level(self):
        with self.assertRaisesRegex(ValueError, "低、中或高"):
            build_condition_config(self.config, conditions(
                operation_error_levels={"pipetting": "extreme"},
            ))


if __name__ == "__main__":
    unittest.main()

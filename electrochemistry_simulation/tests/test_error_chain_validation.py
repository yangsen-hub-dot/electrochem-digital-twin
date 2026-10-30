"""Independent checks for isolated operation errors and their downstream effects."""

import math
import random
import statistics
import unittest
from copy import deepcopy
from pathlib import Path

from electrochem_sim.error_chain_validation import MODES, run_audit, scenario_config, validate_result
from electrochem_sim.error_model import ErrorModel
from electrochem_sim.liquid_transfer import apply_action
from electrochem_sim.error_model import RealizedAction
from electrochem_sim.protocol import Action, build_protocol
from electrochem_sim.simulator import load_config, run_experiment
from electrochem_sim.state import ExperimentState, LiquidState


class AlternatingNoise(random.Random):
    """Prescribed +/- one-standard-deviation draws, not a probability model."""
    def __init__(self):
        super().__init__(0)
        self.sign = -1

    def gauss(self, mu, sigma):
        value = mu + self.sign * sigma
        self.sign *= -1
        return value


class ErrorChainValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = load_config(Path(__file__).resolve().parents[1] / "config" / "copper_concentration_cell.json")

    def run_case(self, mode):
        result = run_experiment(scenario_config(self.base, mode), 20000)
        validate_result(result)
        return result

    def test_all_diagnostic_cases_preserve_plan_and_original_parameters(self):
        original = deepcopy(self.base)
        configs = [scenario_config(self.base, mode) for mode in MODES]
        self.assertEqual(self.base, original)
        self.assertTrue(all(config["protocol"] == configs[0]["protocol"] for config in configs))
        doses = [step["volume_ul"] for step in configs[0]["protocol"] if step["action"] == "aspirate"]
        for actual, expected in zip(doses, (150, 675, 675, 750, 750)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(doses), 5)
        self.assertEqual(sum(step["action"] == "load_tip" for step in configs[0]["protocol"]), 3)

    def test_ideal_baseline_matches_hand_calculated_concentration_and_voltage(self):
        result = self.run_case("ideal")
        left = result["final_state"]["containers"]["left_half_cell"]
        self.assertAlmostEqual(left["volume_l"] * 1e6, 1500)
        self.assertAlmostEqual(left["concentrations_m"]["Cu"], .01)
        expected = 8.314462618 * 298.15 / (2 * 96485.33212) * math.log(10) * 1000
        self.assertAlmostEqual(result["prediction"]["voltage_mv"], expected)
        self.assertAlmostEqual(result["final_state"]["waste"]["volume_l"], 0)

    def test_residual_only_loses_once_per_tip_batch_and_keeps_solute_accounted(self):
        config = scenario_config(self.base, "residual_only")
        config["error_model"]["factors"]["tip_carryover"] = {
            "model": "bounded_residual_volume", "parameters": {"residual_volume_ul": 1.0}}
        result = run_experiment(config, 3)
        validate_result(result)
        left = result["final_state"]["containers"]["left_half_cell"]
        right = result["final_state"]["containers"]["right_half_cell"]
        self.assertAlmostEqual(left["volume_l"] * 1e6, 1498)
        self.assertAlmostEqual(left["concentrations_m"]["Cu"], .1 * 149 / 1498)
        self.assertAlmostEqual(right["volume_l"] * 1e6, 1499)
        self.assertAlmostEqual(right["concentrations_m"]["Cu"], .1)
        self.assertAlmostEqual(result["final_state"]["waste"]["volume_l"] * 1e6, 3)
        water = [step["event"]["simulated_volume_ul"] for step in result["steps"]
                 if step["event"]["action"] == "dispense" and "water" in step["event"]["step_id"]]
        self.assertAlmostEqual(water[0], 674)
        self.assertAlmostEqual(water[1], 675)

    def test_new_tips_each_time_discard_two_residuals_instead_of_one(self):
        state = ExperimentState(containers={"stock": LiquidState("stock", .01, {"Cu": .001}),
                                           "cell": LiquidState("cell")}, temperature_k=298.15)
        for index in range(2):
            for command, volume, residual in (
                (Action(f"load{index}", "load_tip"), None, 0),
                (Action(f"asp{index}", "aspirate", source="stock", volume_l=950e-6), 950e-6, 0),
                (Action(f"disp{index}", "dispense", target="cell"), None, 10e-6),
                (Action(f"discard{index}", "discard_tip"), None, 0),
            ):
                state, _ = apply_action(state, RealizedAction(command, volume, tip_residual_l=residual))
        self.assertAlmostEqual(state.containers["cell"].volume_l * 1e6, 1880)
        self.assertAlmostEqual(state.waste.volume_l * 1e6, 20)
        self.assertAlmostEqual(state.inventory()["volume_l"], .01)
        self.assertAlmostEqual(state.inventory()["amounts_mol"]["Cu"], .001)

    def test_systematic_bias_is_shared_by_tool_and_accumulates_without_averaging(self):
        result = self.run_case("systematic_only")
        groups = {}
        for step in result["steps"]:
            event = step["event"]
            if event["action"] == "aspirate":
                self.assertEqual(event["random_offset_ul"], 0)
                fraction = event["systematic_offset_ul"] / event["pipette_systematic_limit_ul"]
                groups.setdefault(event["pipette_id"], []).append(fraction)
        self.assertEqual(len(groups), 2)
        for values in groups.values():
            self.assertTrue(all(math.isclose(value, values[0]) for value in values))
        water = [step["event"] for step in result["steps"] if step["event"]["action"] == "aspirate"
                 and step["event"]["source"] == "water"]
        total_offset = sum(event["simulated_volume_ul"] - event["requested_volume_ul"] for event in water)
        self.assertNotEqual(water[0]["systematic_offset_ul"], 0)
        self.assertAlmostEqual(total_offset, 2 * water[0]["systematic_offset_ul"])

    def test_repeatability_is_drawn_again_for_each_aspiration(self):
        config = scenario_config(self.base, "repeatability_only")
        rng = AlternatingNoise()
        model = ErrorModel(config["error_model"], rng)
        water = [command for command in build_protocol(config) if command.kind == "aspirate" and command.source == "water"]
        actions = [model.realize(command, rng) for command in water]
        self.assertLess(actions[0].random_offset_l, 0)
        self.assertGreater(actions[1].random_offset_l, 0)
        self.assertAlmostEqual(sum(action.random_offset_l for action in actions), 0)
        self.assertTrue(all(action.systematic_offset_l == 0 for action in actions))

    def test_mixing_changes_local_ranges_without_changing_mean_voltage(self):
        ideal = self.run_case("ideal")
        mixed = self.run_case("mixing_only")
        self.assertAlmostEqual(mixed["prediction"]["voltage_mv"], ideal["prediction"]["voltage_mv"])
        local = mixed["prediction"]["local_concentration_ranges_m"]["left"]
        self.assertLess(local["minimum"], .01)
        self.assertGreater(local["maximum"], .01)
        bounds = mixed["prediction"]["local_voltage_range_mv"]
        self.assertLess(bounds["minimum_mv"], mixed["prediction"]["voltage_mv"])
        self.assertGreater(bounds["maximum_mv"], mixed["prediction"]["voltage_mv"])
        right = mixed["prediction"]["local_concentration_ranges_m"]["right"]
        self.assertAlmostEqual(right["minimum"], right["maximum"])

    def test_repeated_transfer_statistics_distinguish_shared_bias_from_independent_noise(self):
        # Two equal water doses: shared bias adds coherently (variance * 4),
        # independent zero-mean noise adds in quadrature (variance * 2).
        for mode, expected_variance_ratio in (("systematic_only", 4.0), ("repeatability_only", 2.0)):
            config = scenario_config(self.base, mode)
            commands = [command for command in build_protocol(config)
                        if command.kind == "aspirate" and command.source == "water"]
            first, total = [], []
            for seed in range(1500):
                rng = random.Random(seed)
                errors = ErrorModel(config["error_model"], rng)
                actions = [errors.realize(command, rng) for command in commands]
                deviations = [action.systematic_offset_l + action.random_offset_l for action in actions]
                first.append(deviations[0])
                total.append(sum(deviations))
            ratio = statistics.variance(total) / statistics.variance(first)
            self.assertAlmostEqual(ratio, expected_variance_ratio, delta=.2)
            self.assertLess(abs(statistics.mean(total)), .15 * statistics.stdev(total))

    def test_validator_detects_wrong_voltage_and_wrong_event_totals(self):
        result = self.run_case("ideal")
        result["prediction"]["voltage_components_mv"]["ideal_nernst"] += 1
        with self.assertRaisesRegex(AssertionError, "Nernst"):
            validate_result(result)
        result = self.run_case("ideal")
        event = next(step["event"] for step in result["steps"] if step["event"]["action"] == "dispense")
        event["simulated_volume_ul"] -= 1
        with self.assertRaisesRegex(AssertionError, "delivered volume"):
            validate_result(result)

    def test_report_separates_model_intervals_from_local_scenarios_and_corrections(self):
        report = run_audit(self.base, repeats=2, first_seed=20)
        self.assertTrue(report["validation_passed"])
        self.assertFalse(report["automatic_compensation"])
        self.assertFalse(report["cloud_actions_submitted"])
        self.assertEqual(set(report["ensembles"]), set(MODES))
        for mode in MODES:
            self.assertEqual(report["ensembles"][mode]["count"], 2)
        self.assertGreater(report["ensembles"]["mixing_only"]["statistics"]["local_voltage_width_mv"]["mean"], 0)
        self.assertAlmostEqual(report["ensembles"]["ideal"]["statistics"]["local_voltage_width_mv"]["mean"], 0)
        corrections = report["electrochemical_corrections_same_actions"]
        self.assertNotEqual(corrections["activity"]["voltage_mv"], corrections["ideal"]["voltage_mv"])
        self.assertNotEqual(corrections["activity_and_junction"]["voltage_mv"], corrections["activity"]["voltage_mv"])


if __name__ == "__main__":
    unittest.main()

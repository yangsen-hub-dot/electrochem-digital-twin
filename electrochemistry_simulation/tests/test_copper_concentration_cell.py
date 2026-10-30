"""模板核验：基线、守恒、因果关系、误差与失败边界。"""

import math
import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from electrochem_sim.electrochemistry import nernst_voltage_v, predict_voltage
from electrochem_sim.activity import davies_activity_coefficient, ionic_strength_molal
from electrochem_sim.batch import make_case
from electrochem_sim.liquid_junction import (
    evaluate_liquid_junction_potential,
    henderson_junction_potential_v,
    nernst_planck_junction_potential_v,
)
from electrochem_sim.nernst import activity_cell_voltage_v
from electrochem_sim.error_model import ErrorModel, RealizedAction
from electrochem_sim.pipetting import apply_action
from electrochem_sim.protocol import Action, build_protocol
from electrochem_sim.result_recorder import build_result_filename
from electrochem_sim.simulator import load_config, run_experiment, save_result
from electrochem_sim.state import ExperimentState, LiquidState, build_initial_state

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "copper_concentration_cell.json"


class CopperCellTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG_PATH)

    def test_baseline_and_inventory(self):
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_state"]["completed_steps"], 18)
        self.assertAlmostEqual(result["prediction"]["voltage_mv"], 22.78883899493854)
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.1)
        for side in ("left_half_cell", "right_half_cell"):
            self.assertAlmostEqual(result["final_state"]["containers"][side]["volume_l"], 0.001)
        self.assertTrue(result["conservation"]["passed"])
        self.assertTrue(all(step["conservation"]["passed"] for step in result["steps"]))
        self.assertEqual(result["config"], self.config)

    def test_equal_concentrations_and_polarity(self):
        self.assertEqual(nernst_voltage_v(0.01, 0.01, 298.15), 0.0)
        self.assertAlmostEqual(nernst_voltage_v(0.01, 0.1, 298.15), -nernst_voltage_v(0.1, 0.01, 298.15))

    def test_activity_nernst_and_davies_helpers(self):
        self.assertAlmostEqual(activity_cell_voltage_v(0.01, 0.1, 298.15), 0.029579674842860754)
        ionic_strength = ionic_strength_molal(
            {"Cu2+": 0.1, "SO4_2-": 0.1}, {"Cu2+": 2, "SO4_2-": -2}
        )
        self.assertAlmostEqual(ionic_strength, 0.4)
        gamma = davies_activity_coefficient(0.1, 2)
        expected = 10 ** (-0.509 * 4 * (math.sqrt(0.1) / (1 + math.sqrt(0.1)) - 0.03))
        self.assertAlmostEqual(gamma, expected)
        with self.assertRaises(ValueError):
            davies_activity_coefficient(0.6, 2)

    def test_optional_activity_correction_preserves_ideal_default_and_records_inputs(self):
        ideal_config = json.loads(json.dumps(self.config))
        ideal_config["cell"]["activity_correction"]["enabled"] = False
        baseline = run_experiment(ideal_config)["prediction"]
        self.assertAlmostEqual(baseline["voltage_mv"], 29.579674842860754)
        self.assertEqual(baseline["activity_correction"]["status"], "disabled")
        self.assertIsNone(baseline["liquid_junction_potential"]["correction_mv"])

        corrected = run_experiment(self.config)["prediction"]
        self.assertTrue(corrected["valid"])
        self.assertEqual(corrected["activity_correction"]["status"], "estimated")
        self.assertAlmostEqual(corrected["activity_correction"]["ionic_strength_molal_by_side"]["left"], 0.04)
        self.assertAlmostEqual(corrected["activity_correction"]["ionic_strength_molal_by_side"]["right"], 0.4)
        self.assertNotAlmostEqual(corrected["voltage_mv"], baseline["voltage_mv"])
        self.assertAlmostEqual(
            corrected["voltage_components_mv"]["ideal_nernst"]
            + corrected["voltage_components_mv"]["activity_correction"],
            corrected["voltage_mv"],
        )

    def test_activity_correction_requires_ionic_strength_and_matching_temperature(self):
        self.config["cell"]["activity_correction"]["enabled"] = True
        self.config["cell"]["activity_correction"]["ionic_strength_source"] = "configured_values"
        self.config["cell"]["activity_correction"]["ionic_strength_molal_by_side"] = {
            "left": None, "right": None,
        }
        missing = run_experiment(self.config)["prediction"]
        self.assertFalse(missing["valid"])
        self.assertIn("离子强度", missing["reason"])

        self.config["cell"]["activity_correction"]["ionic_strength_molal_by_side"] = {
            "left": 0.02, "right": 0.2,
        }
        self.config["temperature_k"] = 303.15
        mismatch = run_experiment(self.config)["prediction"]
        self.assertFalse(mismatch["valid"])
        self.assertIn("参考温度", mismatch["reason"])

    def test_activity_ionic_strength_tracks_batch_solution_composition(self):
        case = make_case(
            self.config,
            concentration_m=0.05,
            total_volume_ml=1.0,
            stock_step_id="02_aspirate_low_stock",
            water_step_id="06_aspirate_water",
        )
        prediction = run_experiment(case)["prediction"]
        self.assertTrue(prediction["valid"])
        self.assertAlmostEqual(
            prediction["activity_correction"]["ionic_strength_molal_by_side"]["left"], 0.2
        )
        self.assertAlmostEqual(
            prediction["activity_correction"]["ionic_strength_molal_by_side"]["right"], 0.4
        )

    def test_liquid_junction_module_is_explicitly_reserved_not_guessed(self):
        disabled = evaluate_liquid_junction_potential({"enabled": False})
        self.assertEqual(disabled["status"], "not_modeled")
        self.assertIsNone(disabled["correction_mv"])

        self.config["cell"]["liquid_junction_potential"]["enabled"] = True
        self.config["cell"]["liquid_junction_potential"]["parameter_use_policy"] = "condition_matched_only"
        pending = run_experiment(self.config)["prediction"]
        self.assertFalse(pending["valid"])
        self.assertIsNone(pending["voltage_mv"])
        self.assertEqual(
            pending["liquid_junction_potential"]["status"],
            "awaiting_condition_matched_parameters",
        )

    def test_henderson_junction_potential_reverses_with_boundary_order(self):
        charges = {"Na": 1, "Cl": -1}
        mobilities = {"Na": 5.19e-8, "Cl": 7.91e-8}
        side_a = {"Na": 0.01, "Cl": 0.01}
        side_b = {"Na": 0.1, "Cl": 0.1}
        forward = henderson_junction_potential_v(side_a, side_b, charges, mobilities, 298.15)
        reverse = henderson_junction_potential_v(side_b, side_a, charges, mobilities, 298.15)
        self.assertAlmostEqual(forward, -reverse)
        self.assertNotEqual(forward, 0.0)

    def test_nernst_planck_numerical_integral_matches_henderson_for_linear_profile(self):
        charges = {"Na": 1, "Cl": -1}
        mobilities = {"Na": 5.19e-8, "Cl": 7.91e-8}
        side_a = {"Na": 0.01, "Cl": 0.01}
        side_b = {"Na": 0.1, "Cl": 0.1}
        temperature_k = 298.15
        diffusion = {
            ion_id: mobility * 8.314462618 * temperature_k
            / (abs(charges[ion_id]) * 96485.33212)
            for ion_id, mobility in mobilities.items()
        }
        henderson = henderson_junction_potential_v(
            side_a, side_b, charges, mobilities, temperature_k
        )
        nernst_planck = nernst_planck_junction_potential_v(
            side_a, side_b, charges, diffusion, temperature_k
        )
        self.assertAlmostEqual(nernst_planck, henderson, places=9)

    def test_selected_nernst_planck_model_is_recorded_and_computes(self):
        henderson_config = json.loads(json.dumps(self.config))
        henderson_config["cell"]["liquid_junction_potential"].update(
            enabled=True,
            parameter_use_policy="allow_reference_estimate",
            model="henderson_approximation",
        )
        nernst_planck_config = json.loads(json.dumps(henderson_config))
        nernst_planck_config["cell"]["liquid_junction_potential"]["model"] = (
            "nernst_planck_zero_current_linear_profile"
        )

        henderson = run_experiment(henderson_config)["prediction"]["liquid_junction_potential"]
        nernst_planck = run_experiment(nernst_planck_config)["prediction"]["liquid_junction_potential"]
        self.assertEqual(nernst_planck["status"], "estimated_from_reference_parameters")
        self.assertEqual(nernst_planck["model"], "nernst_planck_zero_current_linear_profile")
        self.assertEqual(nernst_planck["numerical_grid_points"], 2001)
        self.assertAlmostEqual(nernst_planck["correction_mv"], henderson["correction_mv"], places=6)
        comparison = nernst_planck["model_comparison"]
        self.assertAlmostEqual(
            comparison["henderson_approximation"]["correction_mv"],
            comparison["nernst_planck_zero_current_linear_profile"]["correction_mv"],
            places=6,
        )
        self.assertLess(abs(comparison["difference_nernst_planck_minus_henderson_mv"]), 1e-6)

    def test_liquid_junction_uses_reference_parameters_only_when_explicitly_allowed(self):
        self.config["cell"]["liquid_junction_potential"].update(
            enabled=True,
            parameter_use_policy="allow_reference_estimate",
        )
        result = run_experiment(self.config)
        prediction = result["prediction"]
        junction = prediction["liquid_junction_potential"]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(prediction["valid"])
        self.assertEqual(junction["status"], "estimated_from_reference_parameters")
        self.assertFalse(junction["condition_matched"])
        self.assertIsNotNone(junction["correction_mv"])
        self.assertIn("Cu", junction["ion_mobilities_m2_v_s"])
        self.assertIn("K", junction["ion_mobilities_m2_v_s"])
        components = prediction["voltage_components_mv"]
        self.assertTrue(components["reported_includes_liquid_junction"])
        self.assertAlmostEqual(
            components["ideal_nernst"]
            + components["activity_correction"]
            + components["liquid_junction_potential"],
            prediction["voltage_mv"],
        )
        self.assertAlmostEqual(
            prediction["local_voltage_range_mv"]["minimum_mv"],
            prediction["voltage_mv"],
        )
        self.assertAlmostEqual(
            prediction["local_voltage_range_mv"]["maximum_mv"],
            prediction["voltage_mv"],
        )

    def test_concentration_cell_model_accepts_an_alternative_active_ion(self):
        alternate = json.loads(json.dumps(self.config))
        alternate["species"] = {
            "Zn": {"name": "Zn²⁺", "formula": "Zn", "charge": 2},
            "SO4": {"name": "SO₄²⁻", "formula": "SO4", "charge": -2},
        }
        for vessel in alternate["containers"].values():
            concentrations = vessel["concentrations_m"]
            if "Cu" in concentrations:
                concentrations["Zn"] = concentrations.pop("Cu")
        alternate["cell"]["active_species"] = "Zn"
        alternate["cell"]["redox_reaction"].update(
            name_zh="Zn²⁺/Zn",
            reduction_reaction_zh="Zn²⁺ + 2e⁻ → Zn(s)",
            display_species="Zn",
            reaction_quotient_terms={"Zn": -1},
        )
        alternate["cell"]["activity_correction"]["ionic_strength_species"] = ["Zn", "SO4"]
        alternate["cell"]["liquid_junction_potential"]["enabled"] = False
        result = run_experiment(alternate)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["prediction"]["active_species"], "Zn")
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.1)

    def test_reaction_quotient_supports_a_dissolved_redox_pair(self):
        state = ExperimentState(
            containers={
                "left": LiquidState("left", 0.001, {"Fe3": 0.00001, "Fe2": 0.00009}),
                "right": LiquidState("right", 0.001, {"Fe3": 0.0001, "Fe2": 0.00001}),
            },
            temperature_k=298.15,
            cell_setup={"electrodes_placed": True, "salt_bridge_connected": True},
        )
        cell = {
            "left_container": "left",
            "right_container": "right",
            "equilibrium_model": "reaction_quotient_concentration_cell_nernst",
            "redox_reaction": {
                "name_zh": "Fe³⁺/Fe²⁺",
                "reduction_reaction_zh": "Fe³⁺ + e⁻ → Fe²⁺",
                "electrons_transferred": 1,
                "reaction_quotient_terms": {"Fe3": -1, "Fe2": 1},
            },
            "activity_correction": {"enabled": False},
            "liquid_junction_potential": {"enabled": False},
        }
        prediction = predict_voltage(
            state,
            cell,
            {"Fe3": {"name": "Fe³⁺", "formula": "Fe", "charge": 3},
             "Fe2": {"name": "Fe²⁺", "formula": "Fe", "charge": 2}},
        )
        expected = 8.314462618 * 298.15 / 96485.33212 * math.log(90) * 1000
        self.assertTrue(prediction["valid"], prediction["reason"])
        self.assertAlmostEqual(prediction["voltage_mv"], expected)
        self.assertEqual(prediction["active_species"], "Fe3")
        self.assertEqual(prediction["reaction_quotient_terms"], {"Fe3": -1.0, "Fe2": 1.0})

    def test_zinc_example_runs_through_full_configured_protocol(self):
        zinc_config = load_config(CONFIG_PATH.parent / "zinc_concentration_cell.json")
        result = run_experiment(zinc_config, seed=42)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["prediction"]["active_species"], "Zn")
        self.assertEqual(result["prediction"]["redox_couple_name_zh"], "Zn²⁺/Zn")
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.01)
        self.assertAlmostEqual(result["prediction"]["c_right_m"], 0.1)
        self.assertAlmostEqual(result["prediction"]["voltage_mv"], 22.78883899493854)
        self.assertTrue(result["conservation"]["passed"])

    def test_invalid_nernst_inputs(self):
        for values in [(0, 0.1, 298.15), (0.01, 0.1, -1), (math.nan, 0.1, 298.15)]:
            with self.assertRaises(ValueError):
                nernst_voltage_v(*values)
    def test_empty_containers_have_no_prediction(self):
        state = build_initial_state(self.config)
        self.assertIsNone(state.containers["left_half_cell"].concentration_m("Cu"))
        self.assertIsNone(predict_voltage(state, self.config["cell"])["voltage_mv"])

    def test_aspiration_changes_new_state_not_old(self):
        state = build_initial_state(self.config)
        state.tip.loaded = True
        command = Action("asp", "aspirate", source="copper_stock", volume_l=0.0001)
        new_state, event = apply_action(state, RealizedAction(command, command.volume_l))
        self.assertAlmostEqual(state.containers["copper_stock"].volume_l, 0.005)
        self.assertAlmostEqual(new_state.containers["copper_stock"].volume_l, 0.0049)
        self.assertAlmostEqual(new_state.tip.liquid.volume_l, 0.0001)
        self.assertAlmostEqual(event["transferred_amounts_mol"]["Cu"], 0.00001)

    def test_stock_bias_changes_concentration(self):
        self.config["error_model"].update(enabled=True, source_proportional_bias={"copper_stock": -0.02})
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "completed")
        self.assertAlmostEqual(result["prediction"]["c_left_m"], 0.1 * 98 / 998)
        self.assertAlmostEqual(result["prediction"]["voltage_mv"], 22.959796079272067)
        self.assertTrue(result["conservation"]["passed"])

    def test_shared_proportional_bias_cancels(self):
        self.config["error_model"].update(enabled=True, proportional_bias=-0.02)
        result = run_experiment(self.config)
        self.assertAlmostEqual(result["prediction"]["voltage_mv"], 22.78883899493854)
        self.assertAlmostEqual(result["final_state"]["containers"]["left_half_cell"]["volume_l"], 0.00098)

    def test_seed_reproducibility_and_distinct_seeds(self):
        self.config["error_model"].update(enabled=True, repeatability_sd_ul=1)
        first = run_experiment(self.config, seed=42)
        second = run_experiment(self.config, seed=42)
        third = run_experiment(self.config, seed=43)
        self.assertEqual(first["steps"], second["steps"])
        self.assertEqual(first["prediction"], second["prediction"])
        self.assertNotEqual(first["prediction"], third["prediction"])

    def test_failure_does_not_partially_update_state(self):
        self.config["protocol"][1]["volume_ul"] = 6000
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "failed")
        self.assertIn("超出当前移液器组合量程", result["failure"]["reason"])
        self.assertEqual(result["steps"][-1]["before"], result["steps"][-1]["after"])
        self.assertTrue(result["conservation"]["passed"])

    def test_tip_required(self):
        self.config["protocol"] = self.config["protocol"][1:]
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "failed")
        self.assertIn("装枪头", result["failure"]["reason"])

    def test_overflow_rejected(self):
        self.config["containers"]["left_half_cell"]["capacity_ml"] = 0.05
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "failed")
        self.assertIn("容量", result["failure"]["reason"])
        self.assertTrue(result["conservation"]["passed"])

    def test_unmixed_source_rejected(self):
        self.config["containers"]["copper_stock"]["mixed"] = False
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "failed")
        self.assertIn("混匀", result["failure"]["reason"])

    def test_discarded_liquid_is_accounted_in_waste(self):
        state = build_initial_state(self.config)
        original = state.inventory()
        for command in [Action("load", "load_tip"), Action("asp", "aspirate", source="copper_stock", volume_l=0.0001), Action("discard", "discard_tip")]:
            state, _ = apply_action(state, RealizedAction(command, command.volume_l))
        self.assertAlmostEqual(state.waste.volume_l, 0.0001)
        self.assertAlmostEqual(state.waste.amounts_mol["Cu"], 0.00001)
        self.assertAlmostEqual(state.inventory()["amounts_mol"]["Cu"], original["amounts_mol"]["Cu"])
        self.assertFalse(state.tip.loaded)

    def test_unmixed_tip_can_be_fully_discarded_with_component_conservation(self):
        state = build_initial_state(self.config)
        tip = state.tip.liquid
        state.tip.loaded = True
        tip.volume_l = 10e-6
        tip.amounts_mol = {"Cu": 5e-6}
        tip.mixing_components = [
            {"source_id": "concentrated_parcel", "volume_l": 5e-6, "amounts_mol": {"Cu": 5e-6}},
            {"source_id": "dilute_parcel", "volume_l": 5e-6, "amounts_mol": {"Cu": 0.0}},
        ]
        tip.mixed = False
        tip.mixing_uniformity = 0.0
        original = state.inventory()

        discarded, event = apply_action(
            state, RealizedAction(Action("discard", "discard_tip"), None)
        )

        self.assertEqual(discarded.tip.liquid.volume_l, 0.0)
        self.assertFalse(discarded.tip.loaded)
        self.assertAlmostEqual(discarded.waste.volume_l, 10e-6)
        self.assertAlmostEqual(discarded.waste.amounts_mol["Cu"], 5e-6)
        self.assertEqual(event["transferred_amounts_mol"], {"Cu": 5e-6})
        self.assertAlmostEqual(discarded.inventory()["amounts_mol"]["Cu"], original["amounts_mol"]["Cu"])

    def test_invalid_protocol_and_error_parameters(self):
        self.config["protocol"][1]["volume_ul"] = -1
        with self.assertRaises(ValueError):
            build_protocol(self.config)
        with self.assertRaises(ValueError):
            ErrorModel({"repeatability_sd_ul": -1})
        with self.assertRaises(ValueError):
            ErrorModel({"proportional_bias": math.nan})

    def test_nonpositive_realization_rejected(self):
        model = ErrorModel({"enabled": True, "proportional_bias": -2})
        with self.assertRaises(ValueError):
            model.realize(Action("asp", "aspirate", source="copper_stock", volume_l=0.0001), random.Random(0))

    def test_preprepared_volume_does_not_change_concentration(self):
        self.config["containers"]["copper_stock"]["concentrations_m"]["Cu"] = 0.01
        self.config["protocol"] = [
            {"step_id": "load", "action": "load_tip"},
            {"step_id": "asp", "action": "aspirate", "source": "copper_stock", "volume_ul": 200},
            {"step_id": "disp", "action": "dispense", "target": "left_half_cell"},
            {"step_id": "mix", "action": "mix", "target": "left_half_cell"},
        ]
        self.config["error_model"].update(enabled=True, proportional_bias=-0.02)
        result = run_experiment(self.config)
        left = result["final_state"]["containers"]["left_half_cell"]
        self.assertAlmostEqual(left["volume_l"], 0.000196)
        self.assertAlmostEqual(left["concentrations_m"]["Cu"], 0.01)
        self.assertFalse(result["prediction"]["valid"])  # 右侧仍空，不能产生假电压。

    def test_one_aspiration_multiple_dispenses(self):
        state = build_initial_state(self.config)
        commands = [
            Action("load", "load_tip"),
            Action("asp", "aspirate", source="copper_stock", volume_l=0.001),
            Action("disp1", "dispense", target="left_half_cell", volume_l=0.0004),
            Action("disp2", "dispense", target="right_half_cell"),
        ]
        for command in commands:
            state, _ = apply_action(state, RealizedAction(command, command.volume_l))
        self.assertAlmostEqual(state.containers["left_half_cell"].volume_l, 0.0004)
        self.assertAlmostEqual(state.containers["right_half_cell"].volume_l, 0.0006)
        self.assertEqual(state.tip.liquid.volume_l, 0)

    def test_result_saved_without_overwrite(self):
        result = run_experiment(self.config)
        with TemporaryDirectory() as directory:
            path = save_result(result, directory)
            self.assertIn("单_铜电池_母液稀释_", path.name)
            self.assertRegex(path.name, r"_\d{6}_[0-9a-f]{8}\.json$")
            self.assertRegex(path.parent.name, r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(load_config(path)["prediction"], result["prediction"])
            with self.assertRaises(FileExistsError):
                save_result(result, directory)

    def test_named_result_filename_uses_china_time_and_safe_characters(self):
        filename = build_result_filename(
            "批", "铜电池/左侧浓度0.01M", "2026-10-12T03:33:31+00:00",
            "12345678abcdef", "json",
        )
        self.assertEqual(
            filename,
            "批_铜电池_左侧浓度0.01M_113331_12345678.json",
        )

    def test_conditions_and_protocol_are_separate_and_assembly_is_recorded(self):
        conditions_path = CONFIG_PATH
        raw = json.loads(conditions_path.read_text(encoding="utf-8"))
        self.assertNotIn("protocol", raw)
        result = run_experiment(self.config)
        self.assertTrue(result["final_state"]["cell_setup"]["electrodes_placed"])
        self.assertTrue(result["final_state"]["cell_setup"]["salt_bridge_connected"])
        self.assertEqual(result["final_state"]["cell_setup"]["salt_bridge_type"], "KNO3")
        self.assertTrue(result["prediction"]["valid"])

    def test_carryover_is_conserved_and_incomplete_mixing_remains_quantifiable(self):
        self.config["error_model"].update(
            enabled=True,
            tip_carryover={"residual_volume_ul": 10.0},
        )
        result = run_experiment(self.config)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["conservation"]["passed"])
        self.assertGreater(result["final_state"]["waste"]["volume_l"], 0)

        self.config["error_model"].update(
            tip_carryover={"residual_volume_ul": 0.0},
            mixing={"efficiency_per_action": 0.9},
        )
        incomplete = run_experiment(self.config)
        self.assertEqual(incomplete["status"], "completed")
        self.assertTrue(incomplete["prediction"]["valid"])
        self.assertGreater(
            incomplete["prediction"]["local_voltage_range_mv"]["maximum_mv"]
            - incomplete["prediction"]["local_voltage_range_mv"]["minimum_mv"],
            0,
        )
        self.assertTrue(incomplete["conservation"]["passed"])

    def test_mixing_efficiency_changes_local_scenario_not_bulk_voltage(self):
        results = []
        for efficiency in (1.0, 0.9, 0.5):
            self.config["error_model"] = {
                "enabled": True,
                "mixing": {"efficiency_per_action": efficiency},
            }
            results.append(run_experiment(self.config))

        bulk_voltages = [result["prediction"]["voltage_mv"] for result in results]
        widths = [
            result["prediction"]["local_voltage_range_mv"]["maximum_mv"]
            - result["prediction"]["local_voltage_range_mv"]["minimum_mv"]
            for result in results
        ]
        self.assertTrue(all(result["status"] == "completed" for result in results))
        self.assertTrue(all(result["conservation"]["passed"] for result in results))
        self.assertAlmostEqual(bulk_voltages[0], bulk_voltages[1])
        self.assertAlmostEqual(bulk_voltages[1], bulk_voltages[2])
        self.assertAlmostEqual(bulk_voltages[0], 22.78883899493854)
        self.assertLess(widths[0], widths[1])
        self.assertLess(widths[1], widths[2])

    def test_error_strength_and_error_mechanism_are_independent_config_dimensions(self):
        self.config["error_model"]["enabled"] = True
        self.config["error_model"]["factors"]["pipetting"]["level"] = "medium"
        catalog_path = CONFIG_PATH.parent / "error_profiles" / "copper_cell_aqueous_100_500_900ul_reference_v2.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.config["error_model"]["factors"]["pipetting"]["model"] = "shared_bounded_bias_gaussian_repeatability"
        self.config["error_model"]["factors"]["pipetting"]["parameters"] = catalog["models"]["pipetting"]["shared_bounded_bias_gaussian_repeatability"]["medium"]
        self.config["error_model"]["factors"]["pipetting"]["parameter_source"] = catalog["source"]
        result = run_experiment(self.config, seed=18)
        applied = result["error_model_realized"]
        self.assertEqual(applied["pipetting"]["model"], "shared_bounded_bias_gaussian_repeatability")
        self.assertEqual(applied["pipetting"]["level"], "medium")
        self.assertEqual(applied["tip_carryover"]["model"], "truncated_gaussian_residual_volume")
        self.assertEqual(applied["tip_carryover"]["sampling_distribution"], "truncated_gaussian")
        self.assertEqual(applied["tip_carryover"]["parameter_source"], "provisional_engineering_prior")
        self.assertEqual(applied["mixing"]["model"], "truncated_gaussian_rate_per_second")
        self.assertEqual(applied["mixing"]["sampling_distribution"], "truncated_gaussian_rate_per_second")
        self.assertEqual(applied["mixing"]["parameter_source"], "provisional_engineering_prior")
        self.assertEqual(applied["tip_carryover"]["status"], "parameterized")
        self.assertEqual(applied["mixing"]["status"], "parameterized")
        residual_low, residual_high = applied["tip_carryover"]["residual_volume_ul_range"]
        self.assertLessEqual(residual_low, applied["tip_carryover"]["sampled_residual_volume_ul"])
        self.assertLessEqual(applied["tip_carryover"]["sampled_residual_volume_ul"], residual_high)
        mix_low, mix_high = applied["mixing"]["rate_range_s_inv"]
        self.assertLessEqual(mix_low, applied["mixing"]["sampled_rate_s_inv"])
        self.assertLessEqual(applied["mixing"]["sampled_rate_s_inv"], mix_high)
        same_seed = run_experiment(self.config, seed=18)["error_model_realized"]
        self.assertEqual(applied["tip_carryover"]["sampled_residual_volume_ul"], same_seed["tip_carryover"]["sampled_residual_volume_ul"])
        self.assertEqual(applied["mixing"]["sampled_efficiency_per_action"], same_seed["mixing"]["sampled_efficiency_per_action"])
        self.assertEqual(applied["pipetting"]["parameter_source"], "published_measurements_and_manufacturer_specs")
        low, high = applied["pipetting"]["systematic_relative_bias_range"]
        self.assertLessEqual(low, applied["pipetting"]["sampled_systematic_relative_bias"])
        self.assertLessEqual(applied["pipetting"]["sampled_systematic_relative_bias"], high)
        aspirates = {step["event"]["step_id"]: step["event"]["error_application_status"] for step in result["steps"] if step.get("event", {}).get("action") == "aspirate"}
        self.assertEqual(aspirates["02_aspirate_low_stock"], "applied")
        self.assertEqual(aspirates["06_aspirate_water"], "not_parameterized")
        self.assertEqual(aspirates["11_aspirate_high_stock_first"], "applied")
        self.assertEqual(aspirates["13_aspirate_high_stock_second"], "applied")
        self.assertTrue(result["conservation"]["passed"])

    def test_truncated_gaussian_stays_in_bounds_and_uses_configured_shape(self):
        rng = random.Random(431)
        values = [
            ErrorModel._sample_truncated_gaussian([0.05, 0.20], 0.125, 0.03, rng, "残液测试")
            for _ in range(1000)
        ]
        self.assertTrue(all(0.05 <= value <= 0.20 for value in values))
        within_one_parent_sd = sum(abs(value - 0.125) <= 0.03 for value in values) / len(values)
        self.assertGreater(within_one_parent_sd, 0.62)
        self.assertLess(within_one_parent_sd, 0.76)

    def test_low_tier_uses_continuous_commercial_pipette_reference(self):
        self.config["error_model"]["enabled"] = True
        from electrochem_sim.condition_input import apply_operation_error_levels
        apply_operation_error_levels(self.config, {"pipetting": "low", "tip_carryover": "low", "mixing": "low"})
        result = run_experiment(self.config, seed=23)
        aspirates = {step["event"]["step_id"]: step["event"]["error_application_status"] for step in result["steps"] if step.get("event", {}).get("action") == "aspirate"}
        self.assertEqual(aspirates["02_aspirate_low_stock"], "applied")
        self.assertEqual(aspirates["06_aspirate_water"], "applied")
        self.assertEqual(aspirates["11_aspirate_high_stock_first"], "applied")
        self.assertEqual(aspirates["13_aspirate_high_stock_second"], "applied")
        events = {step["event"]["step_id"]: step["event"] for step in result["steps"] if step.get("event", {}).get("action") == "aspirate"}
        self.assertEqual(events["02_aspirate_low_stock"]["pipette_id"], "gilson_pipetman_p200")
        self.assertEqual(events["11_aspirate_high_stock_first"]["pipette_id"], "gilson_pipetman_p1000")
        sampled = result["error_model_realized"]["pipetting"]["sampled_continuous_actions"]
        self.assertIn("02_aspirate_low_stock", sampled)
        self.assertEqual(sampled["02_aspirate_low_stock"]["parameter_source_status"], "manufacturer_specification_reference_not_project_calibration")
        pipetting_summary = result["error_model_realized"]["pipetting"]
        self.assertIsNone(pipetting_summary["sampled_systematic_relative_bias"])
        bias_fractions = pipetting_summary["sampled_bias_fraction_by_pipette"]
        self.assertEqual(set(bias_fractions), {"gilson_pipetman_p200", "gilson_pipetman_p1000"})
        self.assertTrue(all(-1.0 <= value <= 1.0 for value in bias_fractions.values()))
        self.assertAlmostEqual(events["11_aspirate_high_stock_first"]["systematic_offset_ul"], events["13_aspirate_high_stock_second"]["systematic_offset_ul"])
        self.assertNotEqual(events["11_aspirate_high_stock_first"]["random_offset_ul"], events["13_aspirate_high_stock_second"]["random_offset_ul"])


if __name__ == "__main__":
    unittest.main()

"""确定性验证：枪头残液账本及移液量到浓度/电压的传播。"""

import json
import math
import unittest
from pathlib import Path

from electrochem_sim.error_model import RealizedAction
from electrochem_sim.electrochemistry import nernst_voltage_v
from electrochem_sim.liquid_transfer import apply_action
from electrochem_sim.protocol import Action
from electrochem_sim.simulator import load_config, run_experiment
from electrochem_sim.state import ExperimentState, LiquidState, TipState


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "copper_concentration_cell.json"


def _protocol(stock_left_ul=100.0, water_ul=(950.0, 950.0), stock_right_ul=(900.0, 900.0)):
    """构造不启用随机误差的同一转移链，用已知动作量隔离传播结果。"""
    steps = [
        {"step_id": "load_left", "action": "load_tip"},
        {"step_id": "aspirate_left_stock", "action": "aspirate", "source": "copper_stock", "volume_ul": stock_left_ul},
        {"step_id": "dispense_left_stock", "action": "dispense", "target": "left_half_cell"},
        {"step_id": "discard_left_stock_tip", "action": "discard_tip"},
        {"step_id": "load_water", "action": "load_tip"},
    ]
    for index, volume_ul in enumerate(water_ul, start=1):
        steps.extend((
            {"step_id": f"aspirate_water_{index}", "action": "aspirate", "source": "water", "volume_ul": volume_ul},
            {"step_id": f"dispense_water_{index}", "action": "dispense", "target": "left_half_cell"},
        ))
    steps.extend((
        {"step_id": "discard_water_tip", "action": "discard_tip"},
        {"step_id": "mix_left", "action": "mix", "target": "left_half_cell"},
        {"step_id": "load_right", "action": "load_tip"},
    ))
    for index, volume_ul in enumerate(stock_right_ul, start=1):
        steps.extend((
            {"step_id": f"aspirate_right_stock_{index}", "action": "aspirate", "source": "copper_stock", "volume_ul": volume_ul},
            {"step_id": f"dispense_right_stock_{index}", "action": "dispense", "target": "right_half_cell"},
        ))
    steps.extend((
        {"step_id": "discard_right_tip", "action": "discard_tip"},
        {"step_id": "mix_right", "action": "mix", "target": "right_half_cell"},
        {"step_id": "place_electrodes", "action": "place_electrodes"},
        {"step_id": "connect_salt_bridge", "action": "connect_salt_bridge", "component": "KNO3"},
    ))
    return steps


class PipetteTransferValidationTests(unittest.TestCase):
    def setUp(self):
        self.base_config = load_config(CONFIG_PATH)

    def _run(self, **kwargs):
        config = json.loads(json.dumps(self.base_config))
        config["protocol"] = _protocol(**kwargs)
        config["error_model"]["enabled"] = False
        result = run_experiment(config, seed=123)
        self.assertEqual(result["status"], "completed", result["failure"])
        self.assertTrue(result["conservation"]["passed"])
        return result

    def test_same_tip_residual_is_retained_not_recharged_as_a_new_loss(self):
        state = ExperimentState(
            containers={
                "stock": LiquidState("stock", volume_l=0.010, amounts_mol={"Cu": 0.001}),
                "cell": LiquidState("cell"),
            },
            temperature_k=298.15,
            tip=TipState(loaded=True),
        )
        initial = state.inventory()
        delivered = []
        for index in range(2):
            aspirate = Action(f"aspirate_{index}", "aspirate", source="stock", volume_l=950e-6)
            state, _ = apply_action(state, RealizedAction(aspirate, 950e-6))
            dispense = Action(f"dispense_{index}", "dispense", target="cell", volume_l=950e-6)
            state, event = apply_action(
                state,
                RealizedAction(dispense, 950e-6, tip_residual_l=10e-6),
            )
            delivered.append(event["simulated_volume_ul"])
            self.assertAlmostEqual(event["residual_volume_ul"], 10.0)

        self.assertEqual(delivered, [940.0, 950.0])
        self.assertAlmostEqual(sum(delivered), 1890.0)
        self.assertAlmostEqual(state.tip.liquid.volume_l * 1e6, 10.0)
        final = state.inventory()
        self.assertAlmostEqual(final["volume_l"], initial["volume_l"])
        self.assertAlmostEqual(final["amounts_mol"]["Cu"], initial["amounts_mol"]["Cu"])

    def test_left_stock_and_water_changes_update_state_concentration_and_voltage(self):
        baseline = self._run()
        less_stock = self._run(stock_left_ul=90.0)
        more_water = self._run(water_ul=(1000.0, 1000.0))

        base_left = baseline["final_state"]["containers"]["left_half_cell"]
        low_stock_left = less_stock["final_state"]["containers"]["left_half_cell"]
        more_water_left = more_water["final_state"]["containers"]["left_half_cell"]
        self.assertAlmostEqual(base_left["volume_l"] * 1e6, 2000.0)
        self.assertAlmostEqual(base_left["concentrations_m"]["Cu"], 0.005)
        self.assertAlmostEqual(low_stock_left["volume_l"] * 1e6, 1990.0)
        self.assertAlmostEqual(low_stock_left["concentrations_m"]["Cu"], 0.1 * 90 / 1990)
        self.assertAlmostEqual(more_water_left["volume_l"] * 1e6, 2100.0)
        self.assertAlmostEqual(more_water_left["concentrations_m"]["Cu"], 0.1 * 100 / 2100)

        base_voltage = baseline["prediction"]["voltage_mv"]
        self.assertGreater(less_stock["prediction"]["voltage_mv"], base_voltage)
        self.assertGreater(more_water["prediction"]["voltage_mv"], base_voltage)
        self.assertLess(more_water["prediction"]["voltage_mv"], less_stock["prediction"]["voltage_mv"])

    def test_adding_same_concentration_stock_to_right_changes_volume_not_concentration_or_voltage(self):
        baseline = self._run()
        more_right_stock = self._run(stock_right_ul=(950.0, 950.0))
        baseline_right = baseline["final_state"]["containers"]["right_half_cell"]
        changed_right = more_right_stock["final_state"]["containers"]["right_half_cell"]

        self.assertGreater(changed_right["volume_l"], baseline_right["volume_l"])
        self.assertAlmostEqual(baseline_right["concentrations_m"]["Cu"], 0.1)
        self.assertAlmostEqual(changed_right["concentrations_m"]["Cu"], 0.1)
        self.assertTrue(math.isclose(
            more_right_stock["prediction"]["voltage_mv"],
            baseline["prediction"]["voltage_mv"],
            rel_tol=0.0,
            abs_tol=1e-10,
        ))

    def test_enabled_continuous_error_realizations_reach_final_state_and_voltage_inputs(self):
        from electrochem_sim.condition_input import apply_operation_error_levels

        config = json.loads(json.dumps(self.base_config))
        config["protocol"] = _protocol()
        config["error_model"]["enabled"] = True
        apply_operation_error_levels(
            config,
            {"pipetting": "low", "tip_carryover": "low", "mixing": "low"},
        )
        result = run_experiment(config, seed=23)
        self.assertEqual(result["status"], "completed", result["failure"])
        self.assertTrue(result["conservation"]["passed"])

        events = [step["event"] for step in result["steps"] if step.get("event", {}).get("action") == "dispense"]
        for container_id in ("left_half_cell", "right_half_cell"):
            target_events = [event for event in events if event["target"] == container_id]
            final_container = result["final_state"]["containers"][container_id]
            delivered_ul = math.fsum(event["simulated_volume_ul"] for event in target_events)
            delivered_mol = math.fsum(
                event["transferred_amounts_mol"].get("Cu", 0.0)
                for event in target_events
            )
            self.assertAlmostEqual(final_container["volume_l"] * 1e6, delivered_ul, places=8)
            self.assertAlmostEqual(final_container["amounts_mol"]["Cu"], delivered_mol, places=14)
            self.assertAlmostEqual(
                final_container["concentrations_m"]["Cu"],
                delivered_mol / (delivered_ul / 1e6),
                places=12,
            )

        left = result["final_state"]["containers"]["left_half_cell"]
        right = result["final_state"]["containers"]["right_half_cell"]
        self.assertAlmostEqual(right["concentrations_m"]["Cu"], 0.1, places=12)
        expected_ideal_mv = nernst_voltage_v(
            left["concentrations_m"]["Cu"],
            right["concentrations_m"]["Cu"],
            config["temperature_k"],
        ) * 1000
        self.assertAlmostEqual(
            result["prediction"]["voltage_components_mv"]["ideal_nernst"],
            expected_ideal_mv,
            places=10,
        )
        ionic_strength = result["prediction"]["activity_correction"]["ionic_strength_molal_by_side"]
        self.assertAlmostEqual(ionic_strength["left"], 4 * left["concentrations_m"]["Cu"], places=12)
        self.assertAlmostEqual(ionic_strength["right"], 4 * right["concentrations_m"]["Cu"], places=12)
        voltage_components = result["prediction"]["voltage_components_mv"]
        self.assertAlmostEqual(
            voltage_components["ideal_nernst"] + voltage_components["activity_correction"],
            result["prediction"]["voltage_mv"],
            places=10,
        )
        self.assertTrue(all(step["conservation"]["passed"] for step in result["steps"]))


if __name__ == "__main__":
    unittest.main()

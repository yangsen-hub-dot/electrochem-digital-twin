import unittest
from copy import deepcopy

from electrochem_sim.chemical_models import get_chemical_model
from electrochem_sim.hmf_fdca_model import FARADAY_C_PER_MOL, MODEL_ID
from electrochem_sim.state import ExperimentState, LiquidState
from electrochem_sim.liquid_transfer import _move_liquid


class HmfEndpointTests(unittest.TestCase):
    def setUp(self):
        self.model = get_chemical_model(MODEL_ID)
        self.state = ExperimentState(containers={"reactor": LiquidState(
            "reactor", .005, {"HMF": .00015625, "TEMPO": .0000078125, "KOH": .0005})},
            temperature_k=298.15)
        self.cell = {"reaction_container": "reactor", "current_a": .01,
                     "duration_s": 6 * FARADAY_C_PER_MOL * .00015625 / .01,
                     "anode": "graphite", "cathode": "stainless_steel",
                     "electrolyte": "aqueous_koh", "mode": "constant_current",
                     "cell_configuration": "paper_single_vial"}

    def observe(self):
        return self.model.observe({}, self.state, self.cell, {})

    def test_reference_and_no_inventory_mutation(self):
        before = deepcopy(self.state.snapshot())
        out = self.observe()
        self.assertTrue(out["valid"], out)
        self.assertAlmostEqual(out["yield_fraction"], .96)
        self.assertAlmostEqual(out["fdca_amount_mol"], .00015)
        self.assertIsNone(out["hplc_reading"])
        self.assertEqual(before, self.state.snapshot())

    def test_interpolation(self):
        liquid = self.state.containers["reactor"]
        liquid.amounts_mol["HMF"] = .000171875
        liquid.amounts_mol["TEMPO"] = .000171875 * .05
        self.cell["duration_s"] = 6 * FARADAY_C_PER_MOL * .000171875 / .01
        out = self.observe()
        self.assertTrue(out["valid"], out)
        self.assertAlmostEqual(out["yield_fraction"], .90)
        self.assertAlmostEqual(out["fdca_amount_mol"], .0001546875)

    def test_unsupported_conditions_do_not_guess(self):
        for key, value in [("current_a", .02), ("duration_s", 2), ("anode", "platinum"),
                           ("cell_configuration", "divided")]:
            with self.subTest(key=key):
                original = self.cell[key]
                self.cell[key] = value
                out = self.observe()
                self.assertFalse(out["valid"])
                self.assertIsNone(out["fdca_amount_mol"])
                self.cell[key] = original

    def test_transfer_error_from_realized_state_is_not_hidden(self):
        # Simulated delivered amount is smaller; model sees the transferred inventory.
        source = LiquidState("source", .001, {"HMF": .0003125})
        reactor = LiquidState("reactor", .0045, {"TEMPO": .0000078125, "KOH": .0005})
        reactor.mixing_components = [{"source_id": "initial", "volume_l": .0045,
                                      "amounts_mol": dict(reactor.amounts_mol)}]
        _move_liquid(source, reactor, .00049)
        self.state.containers["reactor"] = reactor
        out = self.observe()
        self.assertFalse(out["valid"])
        self.assertAlmostEqual(out["input_hmf_mol"], .000153125)
        self.assertIn("volume_l", out["reason"])
        self.assertIn("tempo_mol_fraction", out["reason"])

    def test_concentration_outside_range_and_missing_data(self):
        self.state.containers["reactor"].amounts_mol["HMF"] = .001
        self.assertFalse(self.observe()["valid"])
        del self.state.containers["reactor"].amounts_mol["TEMPO"]
        self.assertFalse(self.observe()["valid"])

    def test_elapsed_validation(self):
        initial = self.model.initialize_state({}, self.state)
        after = self.model.advance(initial, self.state, step={}, event={}, elapsed_time_s=10, config={})
        self.assertTrue(self.model.validate_transition(self.state, self.state, initial, after, config={})["passed"])
        with self.assertRaises(ValueError):
            self.model.advance(initial, self.state, step={}, event={}, elapsed_time_s=-1, config={})


if __name__ == "__main__":
    unittest.main()

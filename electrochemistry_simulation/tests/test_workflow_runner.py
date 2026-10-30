import unittest
from copy import deepcopy
from pathlib import Path

from electrochem_sim.chemical_models import register_chemical_model, get_chemical_model
from electrochem_sim.simulator import load_config
from electrochem_sim.workflow_runner import run_workflow

CONFIG = Path(__file__).resolve().parents[1] / "config" / "hmf_fdca_single_cell_workflow.json"


class AlternativeModel:
    model_family = "test_alternative"
    state_schema_version = "1"
    def initialize_state(self, config, state):
        return {"seconds": 0.0}
    def advance(self, model_state, state, *, step, event, elapsed_time_s, config):
        return {"seconds": model_state["seconds"] + elapsed_time_s}
    def validate_transition(self, before_state, after_state, before_model_state, after_model_state, *, config):
        return {"passed": after_model_state["seconds"] >= before_model_state["seconds"]}
    def observe(self, model_state, state, cell, species_definitions):
        reactor = state.containers[cell["reaction_container"]]
        return {"valid": True, "model_id": "test_other_reaction", "reactor_volume_l": reactor.volume_l, "other_feed_mol": reactor.amounts_mol.get("other_feed"), "model_seconds": model_state["seconds"]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def test_full_workflow_reference_and_ledger(self):
        out = run_workflow(self.config)
        self.assertEqual(out["status"], "completed", out["failure"])
        self.assertEqual(len(out["steps"]), 26)
        pred = out["steps"][-1]["event"]["prediction"]
        self.assertTrue(pred["valid"])
        self.assertAlmostEqual(pred["fdca_amount_mol"], .00015)
        self.assertEqual(out["final_state"]["state_ledger"]["revision"], 26)
        self.assertAlmostEqual(out["final_state"]["state_ledger"]["provenance"][-1]["workflow_event"]["prediction"]["fdca_amount_mol"], .00015)
        self.assertEqual(out["final_state"]["state_ledger"]["certainty"]["chemical"]["status"], "uncertain")
        self.assertNotIn("FDCA", out["final_state"]["containers"]["reactor"]["amounts_mol"])
        self.assertEqual(out["locked_containers"], ["reactor"])
        self.assertTrue(out["conservation"]["passed"])

    def test_missing_connection_stops_before_electrolysis(self):
        self.config["workflow"] = [s for s in self.config["workflow"] if not (s["action"] == "connect_electrode" and s["electrode_id"] == "negative")]
        out = run_workflow(self.config)
        self.assertEqual(out["status"], "failed")
        self.assertIn("connected", out["failure"]["reason"])
        self.assertEqual(out["locked_containers"], [])

    def test_cannot_install_without_grasp(self):
        self.config["workflow"] = [s for s in self.config["workflow"] if not (s["action"] == "grasp_electrode" and s["electrode_id"] == "positive")]
        out = run_workflow(self.config)
        self.assertEqual(out["failure"]["action"], "install_electrode")
        self.assertEqual(out["steps"][-1]["before"]["containers"], out["steps"][-1]["after"]["containers"])

    def test_realized_pipetting_bias_reaches_model_and_is_rejected(self):
        self.config["error_model"] = {"enabled": True, "source_proportional_bias": {"hmf_stock": -.1}}
        out = run_workflow(self.config)
        self.assertEqual(out["status"], "completed_prediction_unavailable", out["failure"])
        pred = out["steps"][-1]["event"]["prediction"]
        self.assertAlmostEqual(pred["input_hmf_mol"], .000140625)
        self.assertIsNone(pred["fdca_amount_mol"])
        self.assertTrue(out["conservation"]["passed"])

    def test_no_sampling_after_unmodeled_reaction(self):
        self.config["workflow"].extend([{"step_id":"after_tip","action":"load_tip"},
                                        {"step_id":"after_sample","action":"aspirate","source":"reactor","volume_ul":100}])
        out = run_workflow(self.config)
        self.assertEqual(out["failure"]["step_id"], "after_sample")
        self.assertIn("unresolved", out["failure"]["reason"])

    def test_alternative_model_plugs_into_same_workflow(self):
        if get_chemical_model("test_other_reaction") is None:
            register_chemical_model("test_other_reaction", AlternativeModel())
        self.config["chemical_model"]["model_id"] = "test_other_reaction"
        self.config["containers"]["hmf_stock"]["concentrations_m"] = {"other_feed": .3125}
        out = run_workflow(self.config)
        self.assertEqual(out["status"], "completed", out["failure"])
        self.assertEqual(out["steps"][-1]["event"]["prediction"]["model_id"], "test_other_reaction")
        self.assertAlmostEqual(out["steps"][-1]["event"]["prediction"]["other_feed_mol"], .00015625)
        self.assertGreater(out["steps"][-1]["event"]["prediction"]["model_seconds"], 9000)

    def test_duplicate_and_unknown_steps_rejected(self):
        c = deepcopy(self.config)
        c["workflow"].append(deepcopy(c["workflow"][0]))
        with self.assertRaises(ValueError): run_workflow(c)
        c = deepcopy(self.config)
        c["workflow"][0]["action"] = "unknown_action"
        with self.assertRaises(ValueError): run_workflow(c)


if __name__ == "__main__": unittest.main()

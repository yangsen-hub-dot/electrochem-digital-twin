"""Execution receipts here are test doubles, never real Matterix evidence."""

import os
import unittest
from copy import deepcopy
from pathlib import Path

from transfer_session import TransferSession


ENGINE_ROOT = Path(os.environ.get("ELECTROCHEM_ENGINE_ROOT", str(
    Path(__file__).resolve().parents[2] / "electrochemistry_simulation")))


def conditions(**updates):
    payload = {
        "left_concentration_m": 0.01, "right_concentration_m": 0.1,
        "left_volume_ml": 1.0, "right_volume_ml": 1.0,
        "temperature_c": 25.0, "salt_bridge_type": "KNO3",
        "salt_bridge_concentration_m": 0.1,
        "activity_correction_enabled": True, "liquid_junction_enabled": False,
        "errors_enabled": True,
    }
    payload.update(updates)
    return payload


def successful_receipt(command, sim_time=1.0, **updates):
    receipt = {name: command[name] for name in ("run_id", "command_id", "step_id", "executor")}
    receipt.update({"status": "succeeded", "simulation_time_s": sim_time,
                    "preconditions_verified": True,
                    "evidence": {"test_stub": "unittest, no cloud execution"}})
    if command["action"] == "load_tip":
        receipt["tip_attached"] = True
    elif command["action"] == "discard_tip":
        receipt["tip_attached"] = False
        receipt["waste_position_verified"] = True
        receipt["actual_volume_ul"] = command["actual_volume_ul"]
        receipt["actual_source"] = "command_assumed"
    elif command["action"] in {"mix", "mix_pair"}:
        if command["action"] == "mix_pair":
            receipt["mixing_efficiencies"] = command["mixing_efficiencies"]
        else:
            receipt["mixing_efficiency"] = command["mixing_efficiency"]
        receipt["mixing_method"] = command["mixing_method"]
        receipt["actual_source"] = command["mixing_efficiency_source"]
        receipt["liquid_volume_change_ul"] = 0.0
    elif command["action"] == "park_pipette":
        receipt.update({"pipette_parked": True, "gripper_released": True,
                        "support_id": command['target'], 'pipette_id': command['pipette_id']})
    elif command['action'] == 'pick_pipette':
        receipt.update(component_attached='pipette', pipette_id=command['pipette_id'],
                       support_id=command['source'], gripper_gap_measured_m=.010)
    elif command["action"] == "pick_electrode":
        receipt.update({"component_attached": "copper_electrode",
                        "source": "electrode_rack", "target": command["target"],
                        "gripper_gap_measured_m": .010})
    elif command["action"] == "place_electrode":
        side = "right" if command["target"] == "right_half_cell" else "left"
        receipt.update({"component_installed": "copper_electrode",
                        "target": command["target"],
                        "insertion_depth_m": command["insertion_depth_m"],
                        "geometry_checks": {f"axis_inside_{side}_cell": True,
                                            "stir_bar_clearance": True,
                                            "working_plate_immersed_in_solution": True,
                                            "working_plate_meets_cap_bottom": True,
                                            "cap_bottom_resting_on_holder": True,
                                            "salt_bridge_reserved": True,
                                            "cap_supported": True}})
    elif command['action'] in {'pick_lead', 'connect_lead', 'measure_voltage'}:
        from measurement_session import REQUIRED_GEOMETRY, LEAD_MAP
        receipt.update(actual_source=command['actual_source'],liquid_volume_change_ul=0.,cell_output_enabled=False)
        if command['action']=='pick_lead':
            receipt.update(component_attached='measurement_lead',source='measurement_lead_rack',
                           target=command['target'],gripper_gap_measured_m=.010)
        elif command['action']=='connect_lead':
            receipt.update(target=command['target'],gripper_released=True,lead_roles=command['lead_roles'],
                           geometry_checks={key:True for key in REQUIRED_GEOMETRY})
        else:
            receipt.update(lead_map=LEAD_MAP,instrument_model='ideal_virtual_ocp',voltage=command['voltage'])
    elif command['action'] in {'pick_salt_bridge', 'connect_salt_bridge'}:
        receipt.update(actual_source=command['actual_source'], liquid_volume_change_ul=0., target='salt_bridge_holder')
        if command['action'] == 'pick_salt_bridge':
            receipt.update(component_attached='salt_bridge', source='salt_bridge_rack', gripper_gap_measured_m=.010)
        else:
            receipt.update(component_installed='salt_bridge', gripper_released=True,
                           geometry_checks={key: True for key in ('left_end_inside_cell','right_end_inside_cell',
                               'left_end_immersed','right_end_immersed','electrodes_clear','stir_bars_clear','holder_contact')})
    else:
        receipt["actual_volume_ul"] = command["actual_volume_ul"]
        receipt["actual_source"] = "simulated_error"
    receipt.update(updates)
    return receipt


class TransferTests(unittest.TestCase):
    def bridge_session(self):
        return TransferSession(ENGINE_ROOT, conditions(), seed=7, executor='test_stub',
                               include_discard_tip=True, include_paired_mix=True, include_salt_bridge=True)

    def test_bridge_commits_after_verified_receipt_without_changing_liquid(self):
        session = self.bridge_session()
        while session.actions[session.index].kind != 'pick_salt_bridge':
            self.complete_one(session)
        before = deepcopy(session.snapshot()['state'])
        self.complete_one(session)
        self.assertFalse(session.state.cell_setup['salt_bridge_connected'])
        command = session.prepare()
        self.assertFalse(session.state.cell_setup['salt_bridge_connected'])
        self.assertIsNone(command['error']['factor'])
        self.complete_one(session)
        result = session.snapshot()
        self.assertTrue(result['state']['cell_setup']['salt_bridge_connected'])
        self.assertIsNone(result['state']['cell_setup']['held_component'])
        self.assertEqual(result['state']['containers'], before['containers'])
        self.assertTrue(result['conservation']['passed'])
        self.assertFalse(result['voltage']['valid'])

    def test_bridge_rejects_unimmersed_or_unsupported_receipt(self):
        for key in ('left_end_immersed', 'right_end_immersed', 'holder_contact'):
            session = self.bridge_session()
            while session.actions[session.index].kind != 'connect_salt_bridge':
                self.complete_one(session)
            command = session.prepare()
            receipt = successful_receipt(command)
            receipt['geometry_checks'][key] = False
            with self.assertRaisesRegex(ValueError, '盐桥双端'):
                session.accept_receipt(receipt)
            self.assertFalse(session.state.cell_setup['salt_bridge_connected'])

    def session(self, **updates):
        return TransferSession(ENGINE_ROOT, conditions(**updates), seed=7, executor="test_stub")

    def complete_one(self, session, sim_time=1.0, **updates):
        command = session.prepare()
        session.accept_receipt(successful_receipt(command, sim_time, **updates))
        return command

    def advance_to(self, session, kind):
        while session.actions[session.index].kind != kind:
            self.complete_one(session, session.revision + 1)

    def fixed_errors(self, session):
        from electrochem_sim.error_model import ErrorModel
        session.errors = ErrorModel({
            "enabled": True,
            "pipetting": {"systematic_relative_bias": 0.01, "repeatability_sd_ul": 0.0},
            "tip_carryover": {"residual_volume_ul": 1.0},
            "mixing": {"efficiency_per_action": 1.0},
        }, rng=session.rng)

    def test_experiment_conditions_generate_the_plan(self):
        session = self.session(left_concentration_m=0.005)
        self.assertEqual([a.kind for a in session.actions], ["pick_pipette", "load_tip", "aspirate", "dispense"])
        self.assertAlmostEqual(next(a for a in session.actions if a.kind == 'aspirate').volume_l * 1_000_000, 50.0)
        self.assertEqual(session.snapshot()["conditions"]["left_concentration_m"], 0.005)
        self.assertIsNone(session.snapshot()["voltage"]["predicted_cell_mv"])

    def test_paired_mixing_accepts_undiluted_right_cell_and_commits_both(self):
        session = TransferSession(
            ENGINE_ROOT, conditions(left_concentration_m=0.005, left_volume_ml=2.0),
            seed=7, executor="test_stub", include_discard_tip=True,
            include_paired_mix=True)
        self.assertEqual(session.actions[-1].kind, "mix_pair")
        self.assertFalse(any(a.step_id.startswith("ui_right_water_") for a in session.actions))
        first_right = next(i for i, a in enumerate(session.actions) if a.step_id.startswith("ui_right_"))
        self.assertTrue(all(a.step_id.startswith("ui_left_") for a in session.actions[:first_right]
                            if a.kind in {'load_tip','aspirate','dispense','discard_tip'}))
        for index in range(len(session.actions) - 1):
            self.complete_one(session, index + 1)
        before = deepcopy(session.snapshot()["state"])
        command = session.prepare()
        self.assertEqual(session.prepare(), command)
        self.assertEqual(session.snapshot()["state"], before)
        efficiencies = command["mixing_efficiencies"]
        self.assertEqual(set(efficiencies), {"left_half_cell", "right_half_cell"})
        self.assertNotEqual(*efficiencies.values())
        result = session.accept_receipt(successful_receipt(command, len(session.actions)))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["conservation"]["passed"])
        for cell_id in efficiencies:
            self.assertEqual(result["state"]["containers"][cell_id]["volume_ul"],
                             before["containers"][cell_id]["volume_ul"])
            self.assertGreaterEqual(result["state"]["containers"][cell_id]["mixing_uniformity"],
                                    before["containers"][cell_id]["mixing_uniformity"])
        self.assertGreater(result["state"]["containers"]["left_half_cell"]["mixing_uniformity"],
                           before["containers"]["left_half_cell"]["mixing_uniformity"])

    def test_paired_electrodes_are_installed_left_then_right_after_mixing(self):
        session = TransferSession(
            ENGINE_ROOT, conditions(), seed=7, executor="test_stub",
            include_discard_tip=True, include_left_water=True,
            include_paired_mix=True, include_left_electrode=True,
            include_right_electrode=True)
        self.assertEqual([action.step_id for action in session.actions[-5:]], [
            "ui_park_pipette", "ui_pick_left_copper_electrode",
            "ui_place_left_copper_electrode", "ui_pick_right_copper_electrode",
            "ui_place_right_copper_electrode"])
        self.assertEqual(session.scope,
                         "paired_preparation_mixing_and_both_electrodes_installation")
        for index in range(len(session.actions)):
            command = self.complete_one(session, index + 1)
            if command["action"] == "place_electrode":
                self.assertIn(command["target"], {"left_half_cell", "right_half_cell"})
        result = session.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["state"]["cell_setup"]["left_electrode_placed"])
        self.assertTrue(result["state"]["cell_setup"]["right_electrode_placed"])
        self.assertTrue(result["state"]["cell_setup"]["electrodes_placed"])
        self.assertIn("盐桥尚未连接", result["voltage"]["reason"])

    def test_electrode_receipt_rejects_unimmersed_plate_without_committing_state(self):
        session = TransferSession(
            ENGINE_ROOT, conditions(), seed=7, executor="test_stub",
            include_discard_tip=True, include_left_water=True,
            include_paired_mix=True, include_left_electrode=True,
            include_right_electrode=True)
        for _ in range(len(session.actions)):
            command = session.prepare()
            if command["action"] == "place_electrode":
                break
            session.accept_receipt(successful_receipt(command))
        else:
            self.fail("实验计划中没有电极安装动作")
        receipt = successful_receipt(command)
        receipt["geometry_checks"]["working_plate_immersed_in_solution"] = False
        with self.assertRaisesRegex(ValueError, "目标铜电极位置"):
            session.accept_receipt(receipt)
        self.assertFalse(session.state.cell_setup["left_electrode_placed"])

    def test_left_mix_is_planned_last_and_commits_only_after_receipt(self):
        session = TransferSession(
            ENGINE_ROOT, conditions(), seed=7, executor="test_stub",
            include_discard_tip=True, include_left_water=True, include_left_mix=True)
        self.assertEqual(session.actions[-1].kind, "mix")
        for index in range(len(session.actions)-1):
            self.complete_one(session, index+1)
        before = session.snapshot()["state"]
        before_volume = session.state.containers["left_half_cell"].volume_l
        before_uniformity = session.state.containers["left_half_cell"].mixing_uniformity
        command = session.prepare()
        self.assertEqual(command["mixing_method"], "CHEM-008_IKA_Plate_RCT_digital")
        self.assertEqual(command["mixing_efficiency_source"], "existing_electrochemistry_simulation_mixing_error_model")
        self.assertEqual(command["liquid_volume_change_ul"], 0.0)
        self.assertEqual(session.snapshot()["state"], before)
        result = session.accept_receipt(successful_receipt(command, len(session.actions)+1))
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["state"]["containers"]["left_half_cell"]["mixing_uniformity"], before_uniformity)
        self.assertAlmostEqual(result["state"]["containers"]["left_half_cell"]["volume_ul"], before_volume*1_000_000)
        self.assertEqual(session.events[-2]["payload"]["mixing_efficiency"], command["mixing_efficiency"])

    def test_prepare_does_not_commit_and_retries_do_not_resample(self):
        session = self.session()
        self.complete_one(session)
        before = session.snapshot()["state"]
        command = session.prepare()
        rng_state = session.rng.getstate()
        profile = deepcopy(session.errors.snapshot())
        for _ in range(5):
            self.assertEqual(command, session.prepare())
        self.assertEqual(before, session.snapshot()["state"])
        self.assertEqual(rng_state, session.rng.getstate())
        self.assertEqual(profile, session.errors.snapshot())
        self.assertEqual(session.revision, 1)

    def test_bias_and_residual_propagate_once_with_conservation(self):
        session = self.session()
        self.fixed_errors(session)
        self.advance_to(session, 'aspirate')
        aspirate = self.complete_one(session, 2)
        self.assertAlmostEqual(aspirate["actual_volume_ul"], 101)
        self.assertAlmostEqual(session.state.tip.liquid.volume_l * 1_000_000, 101)
        dispense = session.prepare()
        self.assertAlmostEqual(dispense["requested_volume_ul"], 100)
        self.assertAlmostEqual(dispense["actual_volume_ul"], 100)
        self.assertAlmostEqual(dispense["retained_tip_volume_ul"], 1)
        self.assertAlmostEqual(dispense["tip_residual_fraction_of_aliquot"], 0.01)
        receipt = successful_receipt(dispense, 3)
        result = session.accept_receipt(receipt)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["full_experiment_completed"])
        self.assertAlmostEqual(result["state"]["containers"]["left_half_cell"]["volume_ul"], 100)
        self.assertAlmostEqual(result["state"]["containers"]["left_half_cell"]["amounts_mol"]["Cu"], 0.00001)
        self.assertAlmostEqual(result["state"]["containers"]["copper_stock"]["volume_ul"], 4899)
        self.assertAlmostEqual(result["state"]["tip"]["liquid"]["volume_ul"], 1)
        self.assertTrue(result["conservation"]["passed"])

    def test_absolute_tip_residual_has_dose_dependent_relative_share(self):
        shares = []
        for target_concentration in (0.01, 0.05):
            session = self.session(left_concentration_m=target_concentration)
            self.fixed_errors(session)
            self.advance_to(session, 'dispense')
            command = session.prepare()
            shares.append(command["tip_residual_fraction_of_aliquot"])
        self.assertAlmostEqual(shares[0], 0.01)
        self.assertAlmostEqual(shares[1], 0.002)

    def test_feedback_replaces_volume_without_second_residual_deduction(self):
        session = self.session()
        self.fixed_errors(session)
        self.advance_to(session, 'aspirate')
        self.complete_one(session, 2, actual_volume_ul=102.0,
                          actual_source="virtual_device_report", difference_reason="test feedback")
        command = session.prepare()
        self.assertAlmostEqual(command["actual_volume_ul"], 101)
        result = session.accept_receipt(successful_receipt(
            command, 3, actual_volume_ul=99.0, actual_source="virtual_device_report",
            difference_reason="test delivered-volume feedback"))
        self.assertAlmostEqual(result["state"]["containers"]["left_half_cell"]["volume_ul"], 99)
        self.assertAlmostEqual(result["state"]["tip"]["liquid"]["volume_ul"], 3)
        self.assertTrue(result["conservation"]["passed"])

    def test_wrong_run_step_executor_or_command_cannot_commit(self):
        for field in ("run_id", "command_id", "step_id", "executor"):
            session = self.session()
            command = session.prepare()
            with self.subTest(field=field), self.assertRaises(ValueError):
                session.accept_receipt(successful_receipt(command, **{field: "wrong"}))
            self.assertEqual(session.revision, 0)

    def test_conflicting_duplicate_receipt_rejected(self):
        session = self.session()
        command = session.prepare()
        receipt = successful_receipt(command)
        session.accept_receipt(receipt)
        with self.assertRaisesRegex(ValueError, "冲突"):
            session.accept_receipt({**receipt, "simulation_time_s": 2})
        self.assertEqual(session.revision, 1)

    def test_unverified_position_and_second_error_rejected(self):
        session = self.session()
        self.advance_to(session, 'aspirate')
        revision = session.revision
        command = session.prepare()
        with self.assertRaisesRegex(ValueError, "前提"):
            session.accept_receipt(successful_receipt(command, revision + 1, preconditions_verified=False))
        with self.assertRaisesRegex(ValueError, "再次添加误差"):
            session.accept_receipt(successful_receipt(command, revision + 1, actual_volume_ul=command["actual_volume_ul"] + 1))
        self.assertEqual(session.revision, revision)

    def test_failure_keeps_state_and_stops_next_action(self):
        session = self.session()
        command = session.prepare()
        before = session.snapshot()["state"]
        result = session.accept_receipt(successful_receipt(
            command, status="failed", no_state_change=True, reason="tip attachment failed"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["state"], before)
        self.assertEqual(session.revision, 0)
        with self.assertRaises(ValueError):
            session.prepare()

    def test_partial_execution_pauses_with_uncertain_state(self):
        session = self.session()
        self.complete_one(session)
        command = session.prepare()
        before = session.snapshot()["state"]
        result = session.accept_receipt(successful_receipt(
            command, status="partial", reason="interrupted after aspiration began"))
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["state_uncertain"])
        self.assertEqual(result["state"], before)  # last confirmed ledger only
        with self.assertRaises(ValueError):
            session.prepare()

    def test_capacity_failure_does_not_consume_random_draw(self):
        session = TransferSession(ENGINE_ROOT, conditions(), executor="test_stub", tip_capacity_ul=50)
        self.advance_to(session, 'aspirate')
        revision = session.revision
        before_rng = session.rng.getstate()
        before_profile = deepcopy(session.errors.snapshot())
        with self.assertRaisesRegex(ValueError, "容量"):
            session.prepare()
        self.assertEqual(session.rng.getstate(), before_rng)
        self.assertEqual(session.errors.snapshot(), before_profile)
        self.assertEqual(session.revision, revision)

    def test_multiple_aliquots_follow_condition_generated_protocol(self):
        session = self.session(left_concentration_m=0.1, left_volume_ml=1.8, errors_enabled=False)
        self.assertEqual(len(session.actions), 6)
        for i in range(len(session.actions)):
            self.complete_one(session, i + 1)
        self.assertAlmostEqual(session.state.containers["left_half_cell"].volume_l * 1_000_000, 1800)
        self.assertTrue(session.snapshot()["conservation"]["passed"])

    def test_events_are_ordered_and_seed_reproduces_actual_quantities(self):
        sessions = [self.session(), self.session()]
        actuals = []
        for session in sessions:
            values = []
            for i in range(3):
                values.append(self.complete_one(session, i + 1)["actual_volume_ul"])
            actuals.append(values)
            events = session.events_after()
            self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(session.events_after(3), events[3:])
            self.assertTrue(all(e["run_id"] == session.run_id for e in events))
        self.assertEqual(*actuals)

    def test_receipt_requires_finite_time_and_execution_evidence(self):
        session = self.session()
        self.advance_to(session, 'load_tip')
        revision = session.revision
        command = session.prepare()
        for update in ({"simulation_time_s": float("nan")}, {"evidence": {}}, {"tip_attached": False}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                session.accept_receipt(successful_receipt(command, **update))
        self.assertEqual(session.revision, revision)

    def test_already_executed_but_impossible_feedback_pauses_reconciliation(self):
        session = self.session(errors_enabled=False)
        self.advance_to(session, 'dispense')
        revision = session.revision
        command = session.prepare()
        result = session.accept_receipt(successful_receipt(
            command, 3, actual_volume_ul=200.0, actual_source="virtual_device_report",
            difference_reason="test inconsistent executor report"))
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["state_uncertain"])
        self.assertEqual(session.revision, revision)
        self.assertEqual(session.state.containers["left_half_cell"].volume_l, 0)
        with self.assertRaises(ValueError):
            session.prepare()

    def test_video_result_is_associated_with_the_run_and_not_voltage(self):
        session = self.session(errors_enabled=False)
        for i in range(3):
            self.complete_one(session, i + 1)
        session.update_video("ready", "/runs/" + session.run_id + "/first_transfer.mp4", frame_count=113)
        self.assertEqual(session.snapshot()["video"]["status"], "ready")
        self.assertEqual(session.events[-1]["type"], "video.ready")
        self.assertEqual(session.events[-1]["run_id"], session.run_id)
        self.assertFalse(session.snapshot()["voltage"]["valid"])


if __name__ == "__main__":
    unittest.main()

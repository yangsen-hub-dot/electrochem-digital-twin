import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cloud_experiment_server import DEFAULT_CONDITIONS, BusyError, ExperimentCoordinator, create_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integration" / "first_transfer"))
from transfer_session import TransferSession
from test_transfer_session import successful_receipt


class ProcessDouble:
    pid = 12345
    code = None
    def poll(self):
        return self.code


class CoordinatorTests(unittest.TestCase):
    def test_authorized_junction_passes_preflight_without_launching_extra_workers(self):
        self.service.start({**DEFAULT_CONDITIONS,'liquid_junction_enabled':True},'authorized_junction_12345')
        self.assertEqual(len(self.launched),1)

    def test_missing_ion_and_strict_policy_fail_before_worker_launch(self):
        for extra in ({'salt_bridge_type':'KCl'},
                      {'liquid_junction_parameter_use_policy':'condition_matched_only'}):
            with self.assertRaisesRegex(ValueError,'启动前液接参数检查未通过'):
                self.service.start({**DEFAULT_CONDITIONS,'liquid_junction_enabled':True,**extra},'invalid_junction_12345')
        self.assertEqual(self.launched,[])

    def test_specific_runner_reason_survives_zero_exit_code(self):
        state=self.service.start(DEFAULT_CONDITIONS,'failure_reason_12345')
        folder=self.path/'runs'/state['run_id'];folder.mkdir()
        (folder/'run.json').write_text(json.dumps({'snapshot':{'run_id':state['run_id'],
             'status':'failed','video':{'status':'failed','reason':'specific chemistry failure'}}}))
        self.launched[-1][1].code=0
        self.service._reap()
        self.assertEqual(self.service.jobs[state['run_id']]['failure'],'specific chemistry failure')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.launched = []
        def launch(argv, **kwargs):
            process = ProcessDouble()
            self.launched.append((argv, process))
            return process
        self.service = ExperimentCoordinator(ROOT / "electrochemistry_simulation",
            ROOT / "integration" / "first_transfer" / "matterix_first_transfer.py", sys.executable,
            self.path / "runs", self.path / "jobs", launcher=launch)

    def tearDown(self):
        self.temp.cleanup()

    def finish(self, job):
        # Executes the real chemistry/session, but deliberately uses test receipts.
        session = TransferSession(ROOT / "electrochemistry_simulation", job["conditions"],
                                  seed=job["seed"], executor="test_stub", run_id=job["run_id"],
                                  include_discard_tip=True, include_paired_mix=True,
                                  include_left_electrode=True, include_right_electrode=True,
                                  include_salt_bridge=True, include_measurement=True)
        for i in range(len(session.actions)):
            command = session.prepare()
            receipt = successful_receipt(command, i + 1)
            session.accept_receipt(receipt)
        session.update_video("ready", "/runs/" + job["run_id"] + "/first_transfer.mp4")
        folder = self.path / "runs" / job["run_id"]
        folder.mkdir()
        (folder / "run.json").write_text(json.dumps(session.archive()), encoding="utf-8")
        self.launched[-1][1].code = 0

    def test_idle_exposes_conditions_and_no_voltage(self):
        state = self.service.state()
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["condition_editor"]["current"]["salt_bridge_type"], "KNO3")
        self.assertIsNone(state["voltage_mv"])

    def test_start_deduplicates_and_passes_same_run_to_runner(self):
        state = self.service.start(DEFAULT_CONDITIONS, "request_test_123456")
        again = self.service.start(DEFAULT_CONDITIONS, "request_test_123456")
        self.assertEqual(state["run_id"], again["run_id"])
        self.assertEqual(len(self.launched), 1)
        self.assertTrue(self.service.jobs[state["run_id"]]["auto_correction_chain"])
        argv = self.launched[0][0]
        self.assertEqual(argv[argv.index("--run-id") + 1], state["run_id"])
        self.assertIn('--include-paired-mix', argv)
        self.assertIn('--include-left-electrode', argv)
        self.assertIn('--include-right-electrode', argv)
        self.assertIn('--include-salt-bridge', argv)
        with self.assertRaises(BusyError):
            self.service.start(DEFAULT_CONDITIONS, "request_test_654321")
        changed = {**DEFAULT_CONDITIONS, "left_volume_ml": 2}
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.service.start(changed, "request_test_123456")

    def test_finished_audit_automatically_starts_evidence_gated_correction(self):
        state = self.service.start(DEFAULT_CONDITIONS, "automatic_correction_12345")
        run_id = state["run_id"]
        run_dir = self.path / "runs" / run_id
        validation_dir = run_dir / "process_validation"
        validation_dir.mkdir(parents=True)
        pick = "ui_pick_left_copper_electrode"
        place = "ui_place_left_copper_electrode"
        target = [0.288, 0.0, 0.145]
        actual = [0.2785, 0.1289, 0.2717]
        timeout = {
            "phase": "pick_electrode/align", "failed_waypoint_index": 2,
            "failed_waypoint_position_m": target,
            "planned_waypoints_m": [[0.288, -0.08, 0.32], [0.288, 0.0, 0.32], target],
            "actual_ee_position_m": actual, "requested_final_position_m": target,
        }
        archive = {
            "snapshot": {"run_id": run_id,
                         "plan": [{"step_id": "ui_mix_pair", "action": "mix_pair"},
                                  {"step_id": pick, "action": "pick_electrode", "target": "left_half_cell"},
                                  {"step_id": place, "action": "place_electrode", "target": "left_half_cell"}],
                         "completed_steps": 1, "state_revision": 1,
                         "state_uncertain": True},
            "events": [{"type": "step.completed", "step_id": "ui_mix_pair"}],
        }
        receipt = {'run_id': run_id, 'step_id': 'ui_mix_pair', 'command_id': 'confirmed_mix', 'status': 'succeeded'}
        archive['events'][0]['payload'] = {'executor_receipt': receipt}
        archive['accepted_receipts'] = {'confirmed_mix': {'receipt': receipt, 'result': {
            'state_revision': 1, 'state': {'cell_setup': {'held_component': None,
                'active_pipette_id': None, 'pipette_parked': True, 'left_electrode_placed':False,
                'right_electrode_placed':False}, 'tip': {'attached': False},
                'containers': {'left_half_cell': {'volume_ul':1500.}, 'right_half_cell': {'volume_ul':1500.}}}}}}
        (run_dir / "run.json").write_text(json.dumps(archive), encoding="utf-8")
        report = {"run_id": run_id, "issues": [{
            "group": "records", "status": "fail", "code": "execution_failed", "step_id": pick,
            "message": "机械臂动作超时: " + json.dumps(timeout),
        }]}
        (validation_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (validation_dir / "audit_status.json").write_text(
            json.dumps({"status": "finished"}), encoding="utf-8")
        manifest = {
            "run_id": run_id,
            "correction": {"attempt_index": 5, "side": "left",
                           "attempts_by_side": {"left": 5},
                           "parameters": {pick: {"approach_offset_xy_m": [0.0, -0.08]}},
                           "pose_preflight": {"pick_electrode/align": {
                               "selected_orientation_wxyz": [0.7071067811865476, 0.0, 0.0,
                                                             -0.7071067811865476]}}},
            "frames": [{"step_id": pick, "gripper_gap_m": 0.08}],
        }
        (run_dir / "video_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        corners = [[x, y, z] for x in (0.2855, 0.2885)
                   for y in (0.07, 0.09) for z in (0.147, 0.149)]
        (validation_dir / "geometry.json").write_text(json.dumps({
            "run_id": run_id, "obstacles": [{
                "path": "/World/ElectrochemTransferProxy/PipetteStand_P200/ForkR",
                "corners": corners,
            }],
        }), encoding="utf-8")
        # The planner must see a completed audit bound to the same source files.
        # Geometry here is synthetic: this test covers coordinator dispatch,
        # while geometry correctness is covered by the separate trace tests.
        from process_validation.continuation import source_revisions
        (validation_dir / "trace.jsonl").write_text('', encoding='utf-8')
        report.update(checks=[], status='fail', counts={
            'pass': 0, 'fail': 1, 'review': 0, 'uncovered': 0},
            source_revisions=source_revisions(run_dir))
        (validation_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        job = self.service.jobs[run_id]
        job["status"] = "paused"
        job["worker_pid"] = None
        job["auto_correction_chain"] = True
        self.service.process = None
        next_run_id = "b" * 32

        # Changed evidence must stop the chain before start_correction is called.
        (run_dir / "run.json").write_text(json.dumps({**archive, 'seed': 123}), encoding='utf-8')
        with patch.object(self.service, "start_correction") as rejected:
            self.service._advance_auto_correction()
        rejected.assert_not_called()
        self.assertFalse(job['auto_correction_chain'])
        self.assertIn('证据', job['auto_correction_stop_reason'])

        # Re-audit/rebind the restored fixture, then the known strategy can dispatch.
        (run_dir / "run.json").write_text(json.dumps(archive), encoding='utf-8')
        report['source_revisions'] = source_revisions(run_dir)
        (validation_dir / "report.json").write_text(json.dumps(report), encoding='utf-8')
        job['auto_correction_chain'] = True
        with patch.object(self.service, "start_correction", return_value={"run_id": next_run_id}) as start:
            self.service._advance_auto_correction()

        start.assert_called_once_with(run_id, auto_chain=True)
        self.assertTrue(job["auto_correction_advanced"])
        self.assertEqual(job["auto_correction_run_id"], next_run_id)
        self.assertEqual(job["status"], "auto_correcting")

    def test_invalid_conditions_never_launch_worker(self):
        with self.assertRaises(ValueError):
            self.service.start({**DEFAULT_CONDITIONS, "left_concentration_m": 1}, "request_test_123456")
        self.assertEqual(self.launched, [])

    def test_audited_repair_continues_only_when_checkpoint_is_eligible(self):
        run_id = 'a' * 32
        folder = self.path / 'runs' / run_id / 'process_validation'
        folder.mkdir(parents=True)
        (folder / 'audit_status.json').write_text(json.dumps({'status':'finished'}))
        (folder / 'report.json').write_text(json.dumps({'status':'pass','checks':[],'issues':[]}))
        job = {'run_id':run_id, 'status':'awaiting_validation', 'correction':{'candidate_id':'left'},
               'auto_correction_chain':True}
        self.service.jobs[run_id] = job
        self.service.active = run_id
        with patch('process_validation.repairs.build_repair_plan',
                   return_value={'eligible':False,'status':'not_applicable'}), \
             patch('process_validation.continuation.build_continuation_plan',
                   return_value={'eligible':False,'reason':'unverified'}), \
             patch.object(self.service, 'start_continuation') as start:
            self.service._advance_auto_correction()
            start.assert_not_called()
        job.update(status='awaiting_validation', auto_correction_chain=True)
        with patch('process_validation.repairs.build_repair_plan',
                   return_value={'eligible':False,'status':'not_applicable'}), \
             patch('process_validation.continuation.build_continuation_plan',
                   return_value={'eligible':True,'expected_revision':23,'checkpoint_signature':'sig'}), \
             patch.object(self.service, 'start_continuation', return_value={'run_id':'b'*32}) as start:
            self.service._advance_auto_correction()
            start.assert_called_once_with(run_id, 23, 'sig', auto_chain=True)
        self.assertEqual(job['auto_correction_run_id'], 'b'*32)

    def test_completed_state_and_video_use_same_run_and_conditions(self):
        conditions = {**DEFAULT_CONDITIONS, "left_concentration_m": 0.005, "left_volume_ml": 2}
        start = self.service.start(conditions, "request_test_123456")
        self.finish(self.service.jobs[start["run_id"]])
        end = self.service.state()
        self.assertEqual(end["status"], "completed")
        self.assertEqual(end['plan'][-1]['step_id'], 'ui_measure_voltage')
        self.assertEqual(end['plan'][-12]['step_id'], 'ui_park_pipette')
        self.assertEqual(end['completed_steps'], end['total_steps'])
        self.assertEqual(end['total_steps'], len(self.service.jobs[start['run_id']]['plan']))
        self.assertEqual(end["run_id"], start["run_id"])
        self.assertEqual(end["conditions"]["left_volume_ml"], 2)
        self.assertEqual(end["conditions"]["left_concentration_m"], 0.005)
        self.assertTrue(end["conservation"]["passed"])
        self.assertTrue(all(e["run_id"] == start["run_id"] for e in end["events"]))
        self.assertEqual(end["video"]["status"], "ready")
        self.assertIsInstance(end["voltage_mv"],float)
        self.assertTrue(end['full_experiment_completed'])
        self.assertIsNone(end['voltage']['measured_mv'])

    def test_worker_failure_is_not_zero_voltage_or_completion(self):
        self.service.start(DEFAULT_CONDITIONS, "request_test_123456")
        self.launched[-1][1].code = 1
        state = self.service.state()
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["voltage_mv"])
        self.assertIsNotNone(state["failure"])

    def test_restart_never_relaunches_unconfirmed_job(self):
        self.service.start(DEFAULT_CONDITIONS, "request_test_123456")
        restored = ExperimentCoordinator(ROOT / "electrochemistry_simulation", "unused.py", sys.executable,
            self.path / "runs", self.path / "jobs", launcher=lambda *a, **k: self.fail("unexpected relaunch"))
        self.assertEqual(restored.state()["status"], "paused")
        with self.assertRaises(BusyError):
            restored.start(DEFAULT_CONDITIONS, "request_test_654321")

    def test_http_auth_and_asynchronous_submit(self):
        token = "test-only-coordinator-code-12345678"
        server = create_server("127.0.0.1", 0, self.service, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(base + "/state")
            self.assertEqual(denied.exception.code, 401)
            request = Request(base + "/simulate", data=json.dumps(DEFAULT_CONDITIONS).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "Idempotency-Key": "request_http_123456", "Origin": "null"})
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "null")
                state = json.load(response)["state"]
                self.assertEqual(state["status"], "starting")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

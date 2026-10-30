"""CPU tests; receipts, video bytes and audit results are explicit test doubles."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for relative in ('integration', 'integration/first_transfer', 'integration/coordinator', 'integration/media_service'):
    sys.path.insert(0, str(ROOT / relative))
from process_validation.continuation import (REQUIRED_CHECKS, build_continuation_plan,
                                            execution_fingerprint, source_revisions,
                                            reviewed_guard_handoff_matches, REVIEWED_GUARD_HANDOFFS)
from cloud_experiment_server import ExperimentCoordinator, create_server
from transfer_session import TransferSession
from resume_checkpoint import restore_confirmed_prefix
from test_transfer_session import successful_receipt, conditions
from cloud_video_server import MediaStore
from history_records import summarize_archive


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class ProcessDouble:
    pid = 123456789
    code = None
    def poll(self):
        return self.code


class ContinuationTests(unittest.TestCase):
    def session(self, run_id):
        return TransferSession(ROOT / 'electrochemistry_simulation', conditions(), seed=7,
            executor='matterix', run_id=run_id, include_discard_tip=True, include_left_water=True,
            include_paired_mix=True, include_left_electrode=True, include_right_electrode=True,
            include_salt_bridge=True, include_measurement=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_id = 'a' * 32
        self.folder = self.root / 'runs' / self.run_id
        (self.folder / 'process_validation').mkdir(parents=True)
        self.original = self.session(self.run_id)
        self.original.scope = 'targeted_segment_diagnostic'
        while True:
            command = self.original.prepare()
            self.original.accept_receipt(successful_receipt(command, self.original.index + 1.))
            if command['step_id'] == 'ui_place_left_copper_electrode':
                break
        self.original.update_video('ready', str(self.folder / 'first_transfer.mp4'))
        self.archive = self.original.archive()
        state = self.archive['snapshot']['state']
        stores = {**state['containers'], 'tip': state['tip']['liquid'], 'waste': state['waste']}
        spec = {'orientation_wxyz': [1., 0., 0., 0.], 'transit_height_m': .42}
        self.manifest = {'run_id': self.run_id, 'status': 'segment_diagnostic_ready',
            'execution_fingerprint': execution_fingerprint(),
            'correction': {'source_run_id': 'b' * 32, 'side': 'left', 'attempt_index': 1,
                'candidate_id': 'side_candidate_1', 'attempts_by_side': {'left': 1},
                'parameters': {f'ui_{kind}_left_copper_electrode': deepcopy(spec) for kind in ('pick', 'place')}},
            'diagnostic': {'stop_after_step': 'ui_place_left_copper_electrode',
                'executed_step_ids': ['ui_pick_left_copper_electrode', 'ui_place_left_copper_electrode']},
            'presentation': {'runtime': {'isaacsim': 'test_double', 'isaaclab': 'test_double'},
                'continuity': {'end_state': {'checkpoint_step_id': 'ui_place_left_copper_electrode',
                    'robot_joint_positions_rad': [0.] * 9, 'robot_joint_velocities': [0.] * 9,
                    'end_effector_position_m': [.4, 0., .42], 'gripper_gap_m': .08,
                    'pipette_parked': True, 'tip_attached': False, 'held_component': None,
                    'left_copper_electrode_installed': True, 'right_electrode_installed': False,
                    'volumes_ul': {k: v['volume_ul'] for k, v in stores.items()}}}}}
        save(self.folder / 'run.json', self.archive)
        save(self.folder / 'video_manifest.json', self.manifest)
        (self.folder / 'first_transfer.mp4').write_bytes(b'TEST DOUBLE: not a decodable video')
        (self.folder / 'process_validation/geometry.json').write_text('{}')
        (self.folder / 'process_validation/trace.jsonl').write_text('test double')
        self.report = {'run_id': self.run_id, 'checks': [
            {'group': group, 'code': code, 'status': 'pass', 'message': 'test double'}
            for group, code in REQUIRED_CHECKS], 'issues': []}
        self.report['checks'] += [
            {'group': 'records', 'code': 'scope', 'status': 'review', 'message': '局部片段'},
            {'group': 'coverage', 'code': 'not_implemented', 'status': 'uncovered', 'message': '接触力未覆盖'}]
        self.refresh_report()
        self.launched = []
        def launch(argv, **kwargs):
            worker = ProcessDouble()
            self.launched.append((argv, worker))
            return worker
        self.launch = launch
        self.coordinator = ExperimentCoordinator(ROOT / 'electrochemistry_simulation',
            ROOT / 'integration/first_transfer/matterix_first_transfer.py', sys.executable,
            self.root / 'runs', self.root / 'jobs', launcher=launch)

    def tearDown(self):
        self.temp.cleanup()

    def refresh_report(self):
        self.report['source_revisions'] = source_revisions(self.folder)
        save(self.folder / 'process_validation/report.json', self.report)

    def plan(self):
        return build_continuation_plan(self.folder)

    def start(self, plan=None):
        plan = plan or self.plan()
        return self.coordinator.start_continuation(self.run_id, plan['expected_revision'], plan['checkpoint_signature'])

    def test_completed_correction_offers_remaining_steps_and_preserves_limits(self):
        plan = self.plan()
        self.assertTrue(plan['eligible'], plan)
        self.assertEqual(plan['remaining_steps'], len(self.original.actions) - self.original.index)
        self.assertEqual(plan['coverage_limits'], ['接触力未覆盖'])
        actions = plan['correction_profile']['actions']
        self.assertIn('ui_pick_left_copper_electrode', actions)
        self.assertNotIn('ui_pick_right_copper_electrode', actions)

    def test_user_review_permits_only_bounded_salt_bridge_with_exact_current_evidence(self):
        self.original = self.session(self.run_id)
        self.original.scope = 'targeted_segment_diagnostic'
        while True:
            command = self.original.prepare()
            self.original.accept_receipt(successful_receipt(command, self.original.index + 1.))
            if command['step_id'] == 'ui_place_right_copper_electrode':
                break
        self.original.update_video('ready', str(self.folder / 'first_transfer.mp4'))
        self.archive = self.original.archive()
        state = self.archive['snapshot']['state']
        stores = {**state['containers'], 'tip': state['tip']['liquid'], 'waste': state['waste']}
        self.manifest['diagnostic'] = {'stop_after_step':'ui_place_right_copper_electrode',
            'executed_step_ids':['ui_pick_right_copper_electrode', 'ui_place_right_copper_electrode']}
        spec = {'orientation_wxyz':[1.,0.,0.,0.], 'transit_height_m':.42}
        self.manifest['correction'].update(side='right', dependency_plan={'scope':'test'},
            parameters={f'ui_{kind}_right_copper_electrode':deepcopy(spec) for kind in ('pick','place')})
        self.manifest['presentation']['continuity']['end_state'].update(
            checkpoint_step_id='ui_place_right_copper_electrode',
            left_copper_electrode_installed=True, right_electrode_installed=True,
            salt_bridge_connected=False, measurement_leads_connected=False,
            volumes_ul={k:v['volume_ul'] for k,v in stores.items()})
        save(self.folder/'run.json', self.archive)
        save(self.folder/'video_manifest.json', self.manifest)
        self.report['checker_fingerprint'] = 'c'*64
        self.refresh_report()
        review = {'schema_version':'human_reviewed_bounded_v1', 'reviewer':'user',
            'decision':'accepted', 'review_message':'这个可以', 'continuation_request':'接着往后',
            'run_id':self.run_id, 'checkpoint_step_id':'ui_place_right_copper_electrode',
            'reviewed_step_ids':self.manifest['diagnostic']['executed_step_ids'],
            'next_step_ids':['ui_pick_salt_bridge','ui_connect_salt_bridge'],
            'stop_after_step':'ui_connect_salt_bridge', 'source_revisions':source_revisions(self.folder),
            'report_sha256':hashlib.sha256((self.folder/'process_validation/report.json').read_bytes()).hexdigest()}
        version_only = {'status':'inconclusive', 'eligible_for_continuation':False,
            'reasons':['检查器/策略版本不一致或旧报告缺少版本证据，不能直接归因于修复']}
        with patch('process_validation.repairs.effects.storage.evaluate_repair', return_value=version_only) as evaluate, \
             patch('process_validation.repairs.effects.version_compatibility._checker', return_value='c'*64):
            self.assertFalse(self.plan()['eligible'])
            accepted = build_continuation_plan(self.folder, reviewed_handoff=review)
            self.assertTrue(accepted['eligible'], accepted)
            self.assertEqual(accepted['status'], 'ready_reviewed_segment')
            self.assertFalse(accepted['reviewed_handoff']['automatic_continuation_allowed'])
            self.assertEqual(accepted['reviewed_handoff']['repair_effect_status'], 'inconclusive')
            started = self.coordinator.start_reviewed_segment(self.run_id, review)
            argv, worker = self.launched[-1]
            self.assertEqual(argv[argv.index('--resume-after-step')+1], 'ui_place_right_copper_electrode')
            self.assertEqual(argv[argv.index('--stop-after-step')+1], 'ui_connect_salt_bridge')
            self.assertNotIn('--continuation-profile', argv)
            self.assertFalse(self.coordinator.jobs[started['run_id']]['auto_correction_chain'])
            self.assertEqual(self.coordinator.start_reviewed_segment(self.run_id, review)['run_id'],
                             started['run_id'])
            self.assertEqual(len(self.launched), 1)
            new_folder = self.root/'runs'/started['run_id']
            new_folder.mkdir()
            resumed = self.session(started['run_id'])
            restore_confirmed_prefix(resumed, self.archive, 'ui_place_right_copper_electrode',
                                     require_terminal=True)
            for step in ('ui_pick_salt_bridge','ui_connect_salt_bridge'):
                cmd = resumed.prepare(); self.assertEqual(cmd['step_id'], step)
                resumed.accept_receipt(successful_receipt(cmd, resumed.index+1.))
            resumed.update_video('ready', str(new_folder/'first_transfer.mp4'))
            save(new_folder/'run.json', resumed.archive())
            save(new_folder/'video_manifest.json', {'run_id':started['run_id'],
                'status':'segment_diagnostic_ready', 'diagnostic':{'source_run_id':self.run_id,
                'stop_after_step':'ui_connect_salt_bridge',
                'executed_step_ids':['ui_pick_salt_bridge','ui_connect_salt_bridge']}})
            worker.code = 0
            self.assertEqual(self.coordinator.state(started['run_id'])['status'], 'awaiting_validation')
            for key, bad in [('reviewer','assistant'), ('report_sha256','0'*64),
                             ('next_step_ids',['ui_pick_left_lead']),
                             ('stop_after_step','ui_connect_left_lead')]:
                modified=deepcopy(review); modified[key]=bad
                with self.subTest(key=key):
                    self.assertFalse(build_continuation_plan(self.folder, reviewed_handoff=modified)['eligible'])
            evaluate.return_value = {'status':'regressed', 'eligible_for_continuation':False,
                                     'reasons':['new failure']}
            self.assertFalse(build_continuation_plan(self.folder, reviewed_handoff=review)['eligible'])
            evaluate.return_value = version_only
            self.report['issues'].append({'group':'geometry','code':'fixture_intersection',
                                          'status':'fail','message':'detected collision'})
            self.refresh_report()
            self.assertFalse(build_continuation_plan(self.folder, reviewed_handoff=review)['eligible'])

    def test_user_review_after_salt_bridge_allows_only_left_lead_pair(self):
        from process_validation.repairs.strategies.salt_bridge import route_for_salt_bridge
        self.original = self.session(self.run_id)
        self.original.scope = 'targeted_segment_diagnostic'
        while True:
            command = self.original.prepare()
            self.original.accept_receipt(successful_receipt(command, self.original.index + 1.))
            if command['step_id'] == 'ui_connect_salt_bridge':
                break
        self.original.update_video('ready', str(self.folder / 'first_transfer.mp4'))
        self.archive = self.original.archive()
        state = self.archive['snapshot']['state']
        stores = {**state['containers'], 'tip': state['tip']['liquid'], 'waste': state['waste']}
        self.manifest['diagnostic'] = {'stop_after_step':'ui_connect_salt_bridge',
            'executed_step_ids':['ui_pick_salt_bridge', 'ui_connect_salt_bridge']}
        salt_context = {'presentation': {'salt_bridge': {'rack_center_m':[.32,-.20,.20],
                                                         'target_center_m':[.55,-.10,.20]}}}
        self.manifest['correction'].update(source_run_id='b'*32, side='salt', dependency_plan={'scope':'test'},
            parameters={}, routes={step:route_for_salt_bridge(step, [], salt_context, 2)
                                   for step in ('ui_pick_salt_bridge','ui_connect_salt_bridge')})
        self.manifest['presentation']['continuity']['end_state'].update(
            checkpoint_step_id='ui_connect_salt_bridge', left_copper_electrode_installed=True,
            right_electrode_installed=True, salt_bridge_connected=True,
            measurement_leads_connected=False, volumes_ul={k:v['volume_ul'] for k,v in stores.items()})
        save(self.folder/'run.json', self.archive)
        save(self.folder/'video_manifest.json', self.manifest)
        self.report['checker_fingerprint'] = 'c'*64
        self.report['checks'].append({'group':'geometry','code':'registered_repair_pose',
                                      'status':'pass','message':'test double'})
        self.refresh_report()
        next_ids = ['ui_pick_left_lead', 'ui_connect_left_lead']
        review = {'schema_version':'human_reviewed_bounded_v1', 'reviewer':'user',
            'decision':'accepted', 'review_message':'这次我看着没有问题',
            'continuation_request':'继续左侧引线局部步骤，本段结束后给我看视频',
            'run_id':self.run_id, 'checkpoint_step_id':'ui_connect_salt_bridge',
            'reviewed_step_ids':self.manifest['diagnostic']['executed_step_ids'],
            'next_step_ids':next_ids, 'stop_after_step':next_ids[-1],
            'source_revisions':source_revisions(self.folder),
            'report_sha256':hashlib.sha256((self.folder/'process_validation/report.json').read_bytes()).hexdigest()}
        improved = {'status':'improved', 'eligible_for_continuation':True, 'reasons':[]}
        with patch('process_validation.repairs.effects.storage.evaluate_repair', return_value=improved), \
             patch('process_validation.repairs.effects.version_compatibility._checker', return_value='c'*64):
            plan = build_continuation_plan(self.folder, reviewed_handoff=review)
            self.assertTrue(plan['eligible'], plan)
            self.assertEqual(plan['reviewed_handoff']['next_step_ids'], next_ids)
            self.assertFalse(plan['reviewed_handoff']['automatic_continuation_allowed'])
            started = self.coordinator.start_reviewed_segment(self.run_id, review)
        argv = self.launched[-1][0]
        self.assertEqual(argv[argv.index('--resume-after-step')+1], 'ui_connect_salt_bridge')
        self.assertEqual(argv[argv.index('--stop-after-step')+1], 'ui_connect_left_lead')
        self.assertNotIn('--continuation-profile', argv)
        self.assertFalse(self.coordinator.jobs[started['run_id']]['auto_correction_chain'])

    def test_independent_chemistry_defers_only_the_future_measurement(self):
        from process_validation.chemistry_checks import check_chemistry
        from process_validation.report import Report
        policy = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))
        chemistry = Report(self.run_id, policy)
        check_chemistry(self.archive, chemistry)
        result = chemistry.finish()
        self.assertEqual(result['counts']['fail'], 0, result)
        pending = [row for row in result['checks'] if row.get('reason_code') == 'not_yet_measured']
        self.assertEqual({row['code'] for row in pending}, {'nernst_baseline', 'voltage_sum'})
        self.report['checks'] += result['checks']
        self.report['issues'] += result['issues']
        self.refresh_report()
        self.assertTrue(self.plan()['eligible'], self.plan())
        pending[0]['reason_code'] = 'missing_evidence'
        self.refresh_report()
        self.assertFalse(self.plan()['eligible'])

    def test_lead_release_handoff_requires_verified_improvement_and_keeps_remaining_work(self):
        from process_validation.repairs.dependency_plan import build_dependency_plan
        from process_validation.repairs.strategies.measurement_lead import route_for_lead
        from process_validation.report import Report
        from process_validation.repairs.effects.storage import evaluate_repair
        policy = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))
        source_id = 'b'*32
        source = self.session(source_id)
        while source.index < len(source.actions):
            command = source.prepare()
            source.accept_receipt(successful_receipt(command, source.index+1.))
        source_archive = source.archive()
        pick, connect = 'ui_pick_left_lead', 'ui_connect_left_lead'
        dependency = build_dependency_plan(source_archive, [pick, connect])
        contract = {'source_run_id': source_id, 'fault_step_ids':[connect], 'dependency_plan':dependency}
        branch = self.session(self.run_id)
        restore_confirmed_prefix(branch, source_archive, dependency['checkpoint_step_id'],
                                 candidate_id='lead_local', branch_type='local_correction', repair_contract=contract)
        for _ in range(2):
            command = branch.prepare()
            branch.accept_receipt(successful_receipt(command, branch.index+1.))
        self.archive = branch.archive()
        setup, state = self.archive['snapshot']['state']['cell_setup'], self.archive['snapshot']['state']
        self.manifest['diagnostic'] = {'stop_after_step':connect,'executed_step_ids':[pick,connect],
                                      'prior_actions_replayed':False,'restored_after_step':dependency['checkpoint_step_id']}
        presentation = {'presentation': {'measurement': {'rack_grip_centers_m': {'left':[.65,-.24,.115]},
                                                         'targets_m': {'left':[.5,0.,.2]}}}}
        self.manifest['correction'] = {'source_run_id':source_id,'candidate_id':'lead_local',
            'attempt_index':2,'parameters':{},'dependency_plan':dependency,'repair_contract':contract,
            'routes':{step:route_for_lead(step,[],presentation,2) for step in (pick,connect)}}
        self.manifest['frames'] = [{'step_id':pick},{'step_id':connect}]
        stores = {**state['containers'],'tip':state['tip']['liquid'],'waste':state['waste']}
        end = self.manifest['presentation']['continuity']['end_state']
        end.update(checkpoint_step_id=connect, left_copper_electrode_installed=True,
                   right_electrode_installed=True,salt_bridge_connected=True,
                   measurement_leads_connected=bool(setup.get('measurement_leads_connected')),
                   volumes_ul={key:value['volume_ul'] for key,value in stores.items()})
        save(self.folder/'run.json',self.archive);save(self.folder/'video_manifest.json',self.manifest)
        def make_report(run_id, failed=False):
            r=Report(run_id,policy)
            r.data.update(checker_fingerprint='c'*64,policy_fingerprint='d'*64)
            for group,code in REQUIRED_CHECKS - {('geometry','grasp_orientation')}:
                r.check(group,code,'pass','synthetic independent coverage')
            r.check('geometry','registered_repair_pose','pass','synthetic pose coverage')
            r.check('records','scope','review','local suffix')
            if failed:r.issue('geometry','fixture_intersection','fail','injected fixture contact',
                               step_id=connect,body='hand',obstacle='fixture')
            return r.finish()
        parent=self.root/'runs'/source_id
        (parent/'process_validation').mkdir(parents=True)
        save(parent/'run.json',source_archive);save(parent/'video_manifest.json',{'run_id':source_id,
            'execution_fingerprint':self.manifest['execution_fingerprint']})
        (parent/'first_transfer.mp4').write_bytes(b'synthetic video')
        (parent/'process_validation/geometry.json').write_text('{}')
        (parent/'process_validation/trace.jsonl').write_text('synthetic trace')
        parent_report=make_report(source_id,True)
        parent_report['source_revisions']=source_revisions(parent)
        save(parent/'process_validation/report.json',parent_report)
        self.report=make_report(self.run_id)
        self.refresh_report()
        self.assertEqual(evaluate_repair(self.folder)['status'],'improved')
        plan=self.plan()
        self.assertTrue(plan['eligible'],plan)
        self.assertEqual(plan['remaining_steps'],3)
        self.assertTrue(plan['correction_profile']['routes'])
        # A newly discovered failure must block continuation even if all other
        # fake pass rows remain present.
        self.report['issues'].append({'group':'geometry','code':'fixture_intersection','status':'fail',
                                     'message':'new collision','step_id':pick})
        self.refresh_report()
        self.assertFalse(self.plan()['eligible'])

    def test_fail_review_and_missing_geometry_do_not_enable_continue(self):
        base = deepcopy(self.report)
        for status in ('fail', 'review', 'uncovered'):
            self.report = deepcopy(base)
            self.report['issues'].append({'group': 'geometry', 'code': 'near_fixture',
                'status': status, 'message': '夹爪间隙未确认'})
            self.refresh_report()
            self.assertFalse(self.plan()['eligible'])
        self.report = deepcopy(base)
        self.report['checks'] = [row for row in self.report['checks'] if row['code'] != 'grasp_orientation']
        self.refresh_report()
        self.assertFalse(self.plan()['eligible'])

    def test_changed_evidence_and_changed_execution_code_are_rejected(self):
        self.manifest['execution_fingerprint'] = '0' * 64
        save(self.folder / 'video_manifest.json', self.manifest)
        self.assertIn('证据', self.plan()['reason'])
        self.refresh_report()
        self.assertIn('执行代码', self.plan()['reason'])

    def test_continuation_accepts_only_proven_release_equivalence(self):
        self.manifest['execution_fingerprint'] = '0' * 64
        save(self.folder / 'video_manifest.json', self.manifest)
        self.refresh_report()
        with patch('process_validation.repairs.effects.version_compatibility.prove_compatible',
                   return_value={'method':'fingerprint_matched_releases'}) as proof:
            result = self.plan()
        self.assertTrue(result['eligible'], result)
        proof.assert_called_once()

    def test_reviewed_guard_handoff_is_bound_to_one_run_and_two_right_lead_steps(self):
        old='295f22ed7978e0ea0a80313f88066c4a2b157fed9775c495ff8ec06b0917fae1'
        new=REVIEWED_GUARD_HANDOFFS['cd6fc7238a03480cbae1752e0d445717']['target_execution_fingerprint']
        review={'run_id':'cd6fc7238a03480cbae1752e0d445717',
            'checkpoint_step_id':'ui_connect_left_lead',
            'next_step_ids':['ui_pick_right_lead','ui_connect_right_lead'],
            'stop_after_step':'ui_connect_right_lead'}
        self.assertTrue(reviewed_guard_handoff_matches(review,old,new))
        self.assertFalse(reviewed_guard_handoff_matches(review,'0'*64,new))
        changed=deepcopy(review);changed['next_step_ids']=['ui_measure_voltage']
        self.assertFalse(reviewed_guard_handoff_matches(changed,old,new))

    def test_reviewed_right_lead_pair_can_proceed_with_documented_cable_mechanics_uncovered(self):
        # Keep the guard narrow: the known cable-mechanics limitation is
        # permitted only for the accepted, exact two-step reviewed handoff.
        self.manifest.update(execution_fingerprint='295f22ed7978e0ea0a80313f88066c4a2b157fed9775c495ff8ec06b0917fae1')
        self.manifest['diagnostic']={'stop_after_step':'ui_connect_left_lead',
            'executed_step_ids':['ui_pick_left_lead','ui_connect_left_lead']}
        self.archive['snapshot']['completed_steps']=len(self.archive['snapshot']['plan'])-3
        self.archive['snapshot']['state_revision']=self.archive['snapshot']['completed_steps']
        # This fixture is replaced below with an internally consistent archive
        # by using a real session prefix through the left-lead connection.
        self.original=self.session(self.run_id);self.original.scope='targeted_segment_diagnostic'
        while True:
            command=self.original.prepare()
            self.original.accept_receipt(successful_receipt(command,self.original.index+1.))
            if command['step_id']=='ui_connect_left_lead': break
        self.original.update_video('ready',str(self.folder/'first_transfer.mp4'))
        self.archive=self.original.archive()
        self.archive['snapshot']['state_revision']=self.archive['snapshot']['completed_steps']
        save(self.folder/'run.json',self.archive)
        state=self.archive['snapshot']['state'];stores={**state['containers'],'tip':state['tip']['liquid'],'waste':state['waste']}
        self.manifest.update(run_id=self.run_id,status='segment_diagnostic_ready',
            execution_fingerprint='295f22ed7978e0ea0a80313f88066c4a2b157fed9775c495ff8ec06b0917fae1',
            correction={'source_run_id':'b'*32,'side':'left','attempt_index':1,
                'candidate_id':'test','dependency_plan':{'scope':'test'},'routes':{},
                'parameters':{'ui_pick_left_copper_electrode':{'orientation_wxyz':[1.,0.,0.,0.],
                                                             'transit_height_m':.42}}},
            diagnostic={'stop_after_step':'ui_connect_left_lead',
                'executed_step_ids':['ui_pick_left_lead','ui_connect_left_lead']},
            presentation={'continuity':{'end_state':{'checkpoint_step_id':'ui_connect_left_lead',
                'robot_joint_positions_rad':[0.]*9,'robot_joint_velocities':[0.]*9,
                'end_effector_position_m':[.4,0.,.42],'gripper_gap_m':.08,'pipette_parked':True,
                'tip_attached':False,'held_component':None,'left_copper_electrode_installed':True,
                'right_electrode_installed':True,'salt_bridge_connected':True,
                'measurement_leads_connected':False,'volumes_ul':{k:v['volume_ul'] for k,v in stores.items()}}}})
        save(self.folder/'video_manifest.json',self.manifest)
        self.report['issues'].append({'group':'geometry','code':'cable_mechanics','status':'uncovered',
                                      'message':'导线力学未校准'})
        self.report['checker_fingerprint']='c'*64
        self.refresh_report()
        review={'schema_version':'human_reviewed_bounded_v1','reviewer':'user','decision':'accepted',
            'review_message':'用户已确认左引线视频并要求继续','continuation_request':'只运行右引线两步并停下给用户看视频',
            'run_id':self.run_id,'checkpoint_step_id':'ui_connect_left_lead',
            'reviewed_step_ids':['ui_pick_left_lead','ui_connect_left_lead'],
            'next_step_ids':['ui_pick_right_lead','ui_connect_right_lead'],
            'stop_after_step':'ui_connect_right_lead','source_revisions':source_revisions(self.folder),
            'report_sha256':hashlib.sha256((self.folder/'process_validation/report.json').read_bytes()).hexdigest()}
        scope={**REVIEWED_GUARD_HANDOFFS['cd6fc7238a03480cbae1752e0d445717'],
               'target_execution_fingerprint':execution_fingerprint(ROOT)}
        effect={'status':'inconclusive','eligible_for_continuation':False,'reasons':[
            '检查器/策略版本不一致或旧报告缺少版本证据，不能直接归因于修复']}
        with patch('process_validation.repairs.effects.version_compatibility._checker',return_value='c'*64), \
             patch.dict(REVIEWED_GUARD_HANDOFFS,{self.run_id:scope}), \
             patch('process_validation.repairs.effects.storage.evaluate_repair',return_value=effect), \
             patch('process_validation.repairs.effects.version_compatibility.prove_compatible',return_value=None):
            plan=build_continuation_plan(self.folder,reviewed_handoff=review,project_root=ROOT)
        self.assertTrue(plan['eligible'],plan)
        self.assertEqual(plan['reviewed_handoff']['next_step_ids'],review['next_step_ids'])

    def test_missing_robot_pose_held_tool_and_unsettled_robot_are_rejected(self):
        initial = deepcopy(self.manifest)
        for key, value in (('robot_joint_positions_rad', None), ('held_component', 'copper_electrode'),
                           ('robot_joint_velocities', [1.] * 9), ('volumes_ul', {})):
            self.manifest = deepcopy(initial)
            self.manifest['presentation']['continuity']['end_state'][key] = value
            save(self.folder / 'video_manifest.json', self.manifest)
            self.refresh_report()
            self.assertFalse(self.plan()['eligible'])

    def test_partial_attempt_needs_explicit_user_retry_from_same_confirmed_source(self):
        source='b'*32;checkpoint='ui_connect_left_lead'
        next_ids=['ui_pick_right_lead','ui_connect_right_lead']
        review={'run_id':source,'checkpoint_step_id':checkpoint,
                'next_step_ids':next_ids,'stop_after_step':next_ids[-1]}
        job={'run_id':self.run_id,'source_run_id':source,'status':'paused',
             'reviewed_segment':{'reviewed_run_id':source,'next_step_ids':next_ids,
                 'stop_after_step':next_ids[-1],'review':deepcopy(review)}}
        self.manifest.update(status='failed',frame_count=263,diagnostic={
            'source_run_id':source,'restored_after_step':checkpoint,
            'stop_after_step':next_ids[-1],'executed_step_ids':[]})
        save(self.folder/'video_manifest.json',self.manifest)
        self.assertFalse(self.coordinator._reviewed_preflight_retryable(job,source,review))
        review.update(retry_of_run_id=self.run_id,retry_authorization='user_requested',
                      retry_message='用户要求再次尝试右引线这一段')
        self.assertTrue(self.coordinator._reviewed_preflight_retryable(job,source,review))
        review['next_step_ids']=['ui_measure_voltage']
        self.assertFalse(self.coordinator._reviewed_preflight_retryable(job,source,review))

    def test_restart_request_index_uses_latest_attempt_independent_of_file_order(self):
        req='reviewed_segment_'+'b'*32
        newest={'run_id':'c'*32,'request_id':req,'status':'paused','created_at_utc':'2026-10-29T11:00:00Z'}
        older={'run_id':'d'*32,'request_id':req,'status':'paused','created_at_utc':'2026-10-29T10:00:00Z'}
        paths=[]
        for job in (newest,older):
            self.coordinator._save_job(job)
            paths.append(self.root/'jobs'/(job['run_id']+'.job.json'))
        with patch.object(Path,'glob',return_value=paths):
            reopened=ExperimentCoordinator(ROOT/'electrochemistry_simulation',
                ROOT/'integration/first_transfer/matterix_first_transfer.py',sys.executable,
                self.root/'runs',self.root/'jobs',launcher=self.launch)
        self.assertEqual(reopened.requests[req],newest['run_id'])

    def test_restore_reuses_history_errors_and_remaining_results(self):
        restored = self.session('c' * 32)
        restore_confirmed_prefix(restored, self.archive, 'ui_place_left_copper_electrode', require_terminal=True)
        before = deepcopy(self.archive)
        self.assertEqual(restored.snapshot()['state'], self.archive['snapshot']['state'])
        self.assertEqual(restored.snapshot()['error_model_realized'], self.archive['snapshot']['error_model_realized'])
        while self.original.status == 'running':
            first, second = self.original.prepare(), restored.prepare()
            for key in ('step_id', 'actual_volume_ul', 'error', 'mixing_efficiencies'):
                self.assertEqual(first.get(key), second.get(key))
            when = self.original.index + 1.
            self.original.accept_receipt(successful_receipt(first, when))
            restored.accept_receipt(successful_receipt(second, when))
        self.assertEqual(restored.snapshot()['state'], self.original.snapshot()['state'])
        self.assertEqual(restored.snapshot()['voltage'], self.original.snapshot()['voltage'])
        self.assertEqual(self.archive, before)

    def test_restore_rejects_tampered_ledger(self):
        self.archive['snapshot']['state']['containers']['left_half_cell']['volume_ul'] += 1
        with self.assertRaisesRegex(ValueError, 'state'):
            restore_confirmed_prefix(self.session('c' * 32), self.archive,
                                     'ui_place_left_copper_electrode', require_terminal=True)

    def test_continuation_launch_and_network_retry_are_idempotent(self):
        before = (self.folder / 'run.json').read_bytes()
        first, second = self.start(), self.start()
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(len(self.launched), 1)
        argv = self.launched[0][0]
        self.assertIn('--continuation-profile', argv)
        self.assertIn('--live-preview', argv)
        self.assertNotIn('--stop-after-step', argv)
        self.assertEqual((self.folder / 'run.json').read_bytes(), before)
        reopened = ExperimentCoordinator(ROOT / 'electrochemistry_simulation',
            ROOT / 'integration/first_transfer/matterix_first_transfer.py', sys.executable,
            self.root / 'runs', self.root / 'jobs', launcher=self.launch)
        plan = self.plan()
        repeated = reopened.start_continuation(self.run_id, plan['expected_revision'], plan['checkpoint_signature'])
        self.assertEqual(repeated['run_id'], first['run_id'])
        self.assertEqual(len(self.launched), 1)

    def test_stale_signature_never_launches(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, '变化'):
            self.coordinator.start_continuation(self.run_id, plan['expected_revision'], '0' * 64)
        self.assertEqual(self.launched, [])

    def test_successful_local_correction_is_not_reported_as_failed(self):
        self.coordinator.jobs[self.run_id] = {'run_id': self.run_id, 'request_id': 'correction_test',
            'status': 'running', 'conditions': conditions(), 'created_at_utc': '2026-10-23',
            'correction': {'source_run_id': 'b' * 32}, 'failure': None,
            'plan': self.archive['snapshot']['plan'], 'total_steps': len(self.original.actions)}
        worker = ProcessDouble(); worker.code = 0
        self.coordinator.active, self.coordinator.process = self.run_id, worker
        self.assertEqual(self.coordinator.state()['status'], 'awaiting_validation')
        self.assertIsNone(self.coordinator.jobs[self.run_id]['failure'])

    def test_http_requires_auth_and_validated_checkpoint_then_deduplicates(self):
        token = 'test-token-not-a-real-secret-123456789'
        server = create_server('127.0.0.1', 0, self.coordinator, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/validation/continue'
        plan = self.plan()
        body = json.dumps({'run_id': self.run_id, 'expected_revision': plan['expected_revision'],
                           'checkpoint_signature': plan['checkpoint_signature']}).encode()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=body, headers={'Content-Type': 'application/json'}))
            self.assertEqual(error.exception.code, 401)
            headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
            with urlopen(Request(url, data=body, headers=headers)) as response:
                first = json.load(response)
            with urlopen(Request(url, data=body, headers=headers)) as response:
                self.assertEqual(json.load(response)['run_id'], first['run_id'])
            self.assertEqual(len(self.launched), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_reviewed_segment_http_is_authenticated_and_forwards_review(self):
        token = 'test-token-not-a-real-secret-123456789'
        server = create_server('127.0.0.1', 0, self.coordinator, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/validation/reviewed-segment'
        review = {'reviewer':'user', 'decision':'accepted'}
        body = json.dumps({'run_id':self.run_id, 'review':review}).encode()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=body, headers={'Content-Type':'application/json'}))
            self.assertEqual(error.exception.code, 401)
            with patch.object(self.coordinator, 'start_reviewed_segment',
                              return_value={'run_id':'c'*32}) as launch:
                headers = {'Authorization':'Bearer '+token, 'Content-Type':'application/json'}
                with urlopen(Request(url, data=body, headers=headers)) as response:
                    result = json.load(response)
                self.assertEqual(result['scope'], 'human_reviewed_bounded_segment')
                launch.assert_called_once_with(self.run_id, review)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_http_local_repair_stops_for_review_unless_auto_chain_is_explicit(self):
        token = 'test-token-not-a-real-secret-123456789'
        server = create_server('127.0.0.1', 0, self.coordinator, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/validation/correct'
        headers = {'Authorization':'Bearer '+token, 'Content-Type':'application/json'}
        try:
            with patch.object(self.coordinator, 'start_correction', return_value={'run_id':'c'*32}) as launch:
                for body in ({'run_id':self.run_id}, {'run_id':self.run_id, 'auto_chain':True}):
                    with urlopen(Request(url, data=json.dumps(body).encode(), headers=headers)) as response:
                        self.assertEqual(response.status, 202)
                self.assertEqual(launch.call_args_list[0].kwargs['auto_chain'], False)
                self.assertEqual(launch.call_args_list[1].kwargs['auto_chain'], True)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_continued_video_and_history_are_labeled_as_segments(self):
        self.manifest.update(status='ready', continuation={'source_run_id': 'd' * 32},
                             recording_scope='remaining_workflow_segment', full_workflow_video=False)
        save(self.folder / 'video_manifest.json', self.manifest)
        entry = MediaStore(self.root / 'runs', 'test-token-with-at-least-24-chars').recording(self.run_id)[1]
        self.assertTrue(entry['continuation_segment'])
        self.assertFalse(entry['full_workflow_video'])
        self.assertIn('续', entry['title'])
        row = summarize_archive(self.archive, self.run_id, {'continuation': {'root_run_id': 'b' * 32},
                                                           'source_run_id': 'd' * 32})
        self.assertEqual(row['record_kind'], 'continued_workflow')


if __name__ == '__main__':
    unittest.main()

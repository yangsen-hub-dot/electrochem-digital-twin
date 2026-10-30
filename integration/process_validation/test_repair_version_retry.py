"""Tests for proven release compatibility and bounded timeout retries."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import hashlib
import unittest
from unittest.mock import patch

from process_validation.repairs.effects.version_compatibility import (
    prove_compatible, _semantic, _checker, _profile_semantic_digest,
    _resumed_gripper_change_is_overridden, _RESUMED_GRIPPER_OVERRIDE_REVISIONS,
)
from process_validation.repairs.failed_retry import retryable_timeout
from process_validation.repairs.strategies.electrode_probe import (
    uncompensated_probe, refine_probe, unstable_probe, upward_pregrasp_descent,
)
from process_validation.repairs.profile import validate_repair_profile
from process_validation.correction import _unreachable_probe_issue, build_correction_plan


class CompatibilityTests(unittest.TestCase):
    def test_resume_open_equivalence_requires_free_checkpoint_and_matching_pick_target(self):
        def record(target=.012, side='right'):
            pick = f'ui_pick_{side}_copper_electrode'
            place = f'ui_place_{side}_copper_electrode'
            checkpoint = 'ui_place_left_copper_electrode'
            return {'manifest': {'correction': {
                'side': side,
                'dependency_plan': {
                    'confirmed_checkpoint_facts': ['gripper:free'],
                    'rerun_step_ids': [pick, place],
                    'checkpoint_step_id': checkpoint,
                },
                'parameters': {pick: {'gripper_open_joint_target_m': target}},
            }, 'diagnostic': {
                'executed_step_ids': [pick],
                'prior_actions_replayed': False,
                'restored_after_step': checkpoint,
                'stop_after_step': place,
            }}}

        before, after = record(), record()
        self.assertTrue(_resumed_gripper_change_is_overridden(before, after))
        self.assertFalse(_resumed_gripper_change_is_overridden(before, record(target=.04)))
        held = record()
        held['manifest']['correction']['dependency_plan']['confirmed_checkpoint_facts'] = []
        self.assertFalse(_resumed_gripper_change_is_overridden(before, held))
        self.assertIn('5aafdae9cc70e0a8809f8c7485cc5393cab856b304d581e1c289e8b52abbf075',
                      _RESUMED_GRIPPER_OVERRIDE_REVISIONS[
                          'integration/first_transfer/matterix_first_transfer.py'])
        self.assertIn('13122e2d83397697427ab325910e1e05086e711d751373384a2af7aaaff099ef',
                      _RESUMED_GRIPPER_OVERRIDE_REVISIONS[
                          'integration/first_transfer/repair_execution.py'])

    def test_full_open_pick_guard_revision_has_exact_reviewed_equivalence(self):
        reviewed = 'reviewed-stricter-probe-profile-v1'
        prior = '742da68e3cfb9035203cc7b60eb0f09245f99e60af5d7e44dcb878768abe8eee'
        current = 'd724c07c475b22214918184260fed61e92294dbb4cd3be7b0c3caab43d2ff900'
        self.assertEqual(_profile_semantic_digest(prior), reviewed)
        self.assertEqual(_profile_semantic_digest(current), reviewed)
        self.assertEqual(_profile_semantic_digest('f' * 64), 'f' * 64)

    def test_checker_fingerprint_uses_platform_independent_relative_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            checker_dir = root / 'integration/process_validation'
            (checker_dir / 'chemistry_checks').mkdir(parents=True)
            (checker_dir / 'audit_run.py').write_text('audit')
            (checker_dir / 'chemistry_checks/audit.py').write_text('chemistry')
            files = {
                'audit_run.py': hashlib.sha256(b'audit').hexdigest(),
                'chemistry_checks/audit.py': hashlib.sha256(b'chemistry').hexdigest(),
            }
            expected = hashlib.sha256(
                json.dumps(files, sort_keys=True, allow_nan=False).encode()).hexdigest()
            self.assertEqual(_checker(root), expected)

    def test_only_verified_orchestration_differences_are_allowed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('old', 'new'):
                base = root / name / 'integration/process_validation'
                (base / 'repairs').mkdir(parents=True)
                (base / 'audit_run.py').write_text('same audit')
                (base / 'repairs/pipeline.py').write_text(name)
            before = {'manifest': {'execution_fingerprint':'a'*64},
                      'report': {'checker_fingerprint':'c'*64}}
            after = {'manifest': {'execution_fingerprint':'b'*64},
                     'report': {'checker_fingerprint':'d'*64}}
            with patch('process_validation.repairs.effects.version_compatibility._checker',
                       side_effect=lambda p: ('c' if p.name == 'old' else 'd')*64), \
                 patch('process_validation.continuation.execution_fingerprint',
                       side_effect=lambda p: ('a' if p.name == 'old' else 'b')*64):
                self.assertIsNotNone(prove_compatible(before, after, root))
                (root / 'new/integration/process_validation/audit_run.py').write_text('changed audit')
                self.assertIsNone(prove_compatible(before, after, root))
                (root / 'new/integration/process_validation/audit_run.py').write_text('same audit')
                after['manifest']['execution_fingerprint'] = 'e'*64
                self.assertIsNone(prove_compatible(before, after, root))

    def test_reaudit_can_bind_execution_and_checker_to_separate_semantic_release(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('before', 'runtime', 'checker'):
                base = root / name / 'integration/process_validation'
                (base / 'repairs').mkdir(parents=True)
                (base / 'audit_run.py').write_text('same audit semantics')
                (base / 'repairs/pipeline.py').write_text(name)
            before = {'manifest': {'execution_fingerprint':'a'*64},
                      'report': {'checker_fingerprint':'c'*64}}
            after = {'manifest': {'execution_fingerprint':'b'*64},
                     'report': {'checker_fingerprint':'d'*64}}
            checker_by_release = {'before':'c', 'runtime':'x', 'checker':'d'}
            execution_by_release = {'before':'a', 'runtime':'b', 'checker':'z'}
            with patch('process_validation.repairs.effects.version_compatibility._checker',
                       side_effect=lambda p: checker_by_release[p.name]*64), \
                 patch('process_validation.continuation.execution_fingerprint',
                       side_effect=lambda p: execution_by_release[p.name]*64):
                evidence = prove_compatible(before, after, root)
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence['method'],
                                 'independent_fingerprint_and_semantic_match')
                self.assertEqual(evidence['releases'][1],
                                 {'execution':'runtime', 'checker':'checker'})
                (root / 'checker/integration/process_validation/audit_run.py').write_text(
                    'changed checker semantics')
                self.assertIsNone(prove_compatible(before, after, root))

    def test_continuation_helper_changes_are_not_silently_ignored(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'integration/process_validation/continuation.py'
            p.parent.mkdir(parents=True)
            p.write_text('def source_revisions():\n return 1\ndef build_continuation_plan():\n return 1\n'); a = _semantic(Path(tmp))
            p.write_text('def source_revisions():\n return 1\ndef build_continuation_plan():\n return 2\n'); self.assertEqual(a, _semantic(Path(tmp)))
            p.write_text('def source_revisions():\n return 2\ndef build_continuation_plan():\n return 1\n'); self.assertNotEqual(a, _semantic(Path(tmp)))


class RetryTests(unittest.TestCase):
    def test_upward_fingertip_descent_timeout_selects_only_verified_top_down_retry(self):
        step = 'ui_pick_left_copper_electrode'
        orientation = [.7071067811865476, 0., 0., -.7071067811865476]
        segment_rows = []
        for index in range(1, 6):
            segment_rows.append({'segment_index':index,'orientation_wxyz':orientation,
                                 'position_error_m':[.001, .002, -.003]})
        manifest = {
            'correction': {
                'attempt_index':11,'candidate_id':'electrode_slot_pregrasp_kinematic_probe_11',
                'parameters':{step:{'orientation_wxyz':orientation,
                    'orientation_probe_at_pregrasp':True,'gripper_open_joint_target_m':.012}},
                'pose_preflight':{'pick_electrode/align':{
                    'selected_orientation_wxyz':orientation,
                    'closing_axis_world':[1.,0.,0.],
                    'gripper_approach_axis_world':[0.,0.,1.]}},
                'orientation_probe_segments':[{'phase':'pick_electrode/align/orientation_probe',
                    'pregrasp_position_m':[.288,0.,.48],'segments':segment_rows}],
            }
        }
        target=[.288,0.,.145]
        actual=[.278,.128,.271]
        failure={'phase':'pick_electrode/align','failed_waypoint_index':1,
                 'failed_waypoint_position_m':target,'requested_final_position_m':target,
                 'planned_waypoints_m':[[.288,0.,.32],target],'actual_ee_position_m':actual,
                 'failed_waypoint_error_m':[actual[i]-target[i] for i in range(3)]}
        report={'checks':[{'group':'geometry','code':'sampled_intersections','status':'pass'}],
                'issues':[]}
        self.assertIsNotNone(upward_pregrasp_descent(manifest,report,step,failure))
        manifest['correction']['pose_preflight']['pick_electrode/align'][
            'gripper_approach_axis_world']=[0.,0.,-1.]
        self.assertIsNone(upward_pregrasp_descent(manifest,report,step,failure))

    def test_attempt_12_plan_uses_top_down_descent_without_reusing_bias(self):
        run_id = 'd' * 32
        step = 'ui_pick_left_copper_electrode'
        checkpoint = 'ui_park_pipette'
        old_orientation = [.7071067811865476, 0., 0., -.7071067811865476]
        top_down = [0., .7071067811865476, .7071067811865476, 0.]
        segments = [{'segment_index': index, 'orientation_wxyz': old_orientation,
                     'position_error_m': [.001, .002, -.003]} for index in range(1, 6)]
        target, actual = [.288, 0., .145], [.278, .128, .271]
        failure = {'phase': 'pick_electrode/align', 'failed_waypoint_index': 1,
                   'failed_waypoint_position_m': target, 'requested_final_position_m': target,
                   'planned_waypoints_m': [[.288, 0., .32], target],
                   'actual_ee_position_m': actual,
                   'failed_waypoint_error_m': [actual[i] - target[i] for i in range(3)]}
        issue = {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
                 'step_id': step,
                 'message': '机械臂动作超时: ' + json.dumps(failure, ensure_ascii=False)}
        manifest = {'run_id': run_id, 'correction': {
            'attempt_index': 11, 'side': 'left',
            'candidate_id': 'electrode_slot_pregrasp_kinematic_probe_11',
            'parameters': {step: {'orientation_wxyz': old_orientation,
                                  'transit_height_m': .48,
                                  'orientation_probe_at_pregrasp': True,
                                  'gripper_open_joint_target_m': .012}},
            'pose_preflight': {'pick_electrode/align': {
                'closing_axis_world': [1., 0., 0.],
                'gripper_approach_axis_world': [0., 0., 1.]}},
            'orientation_probe_segments': [{
                'phase': 'pick_electrode/align/orientation_probe', 'segments': segments}]}}
        report = {'run_id': run_id,
                  'checks': [{'group': 'geometry', 'code': 'sampled_intersections', 'status': 'pass'}],
                  'issues': [issue]}
        archive = {'snapshot': {'run_id': run_id, 'state_uncertain': True,
                                'plan': [{'step_id': checkpoint}, {'step_id': step}]},
                   'events': [{'type': 'step.completed', 'step_id': checkpoint},
                              {'type': 'step.failed', 'step_id': step,
                               'payload': {'executor_receipt': {'reason': issue['message']}}}]}
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / run_id
            (run_dir / 'process_validation').mkdir(parents=True)
            (run_dir / 'run.json').write_text(json.dumps(archive), encoding='utf-8')
            (run_dir / 'video_manifest.json').write_text(json.dumps({**manifest,
                'motion_failure': failure}), encoding='utf-8')
            (run_dir / 'process_validation' / 'report.json').write_text(
                json.dumps(report, ensure_ascii=False), encoding='utf-8')
            (run_dir / 'process_validation' / 'geometry.json').write_text(
                json.dumps({'run_id': run_id, 'obstacles': []}), encoding='utf-8')
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan['eligible'], plan)
        self.assertEqual(plan['attempt_index'], 12)
        self.assertEqual(plan['candidate_id'], 'electrode_top_down_after_upward_pregrasp_12')
        self.assertEqual(plan['checkpoint_step_id'], checkpoint)
        self.assertEqual(plan['stop_after_step'], step)
        pick = plan['actions'][step]
        self.assertEqual(pick['orientation_wxyz'], top_down)
        self.assertEqual(pick['high_transit_orientation_wxyz'], top_down)
        self.assertTrue(pick['top_down_vertical_descent'])
        self.assertTrue(pick['orientation_probe_at_pregrasp'])
        self.assertNotIn('side_pose_observed_bias_xyz_m', pick)
        self.assertNotIn('orientation_probe_bias_xyz_m', pick)
        self.assertEqual(pick['approach_offset_xy_m'], [0., 0.])
        validate_repair_profile(plan, run_id)
        pick['orientation_wxyz'] = old_orientation
        with self.assertRaises(ValueError):
            validate_repair_profile(plan, run_id)

    def test_feedback_refinement_requires_actual_command_and_geometry(self):
        step = 'ui_pick_left_copper_electrode'
        bias = [.028, -.013, -.021]
        target = [.427, -.013, .48]
        error = [.011, 0., -.007]
        spec = {'orientation_probe_bias_xyz_m':bias, 'side_pose_observed_bias_xyz_m':bias,
                'orientation_wxyz':[.7071067811865476,0,0,-.7071067811865476]}
        failure = {'phase':'pick_electrode/align/orientation_probe', 'failed_waypoint_index':0,
                   'failed_waypoint_position_m':target, 'failed_waypoint_error_m':error,
                   'actual_ee_position_m':[target[i]+error[i] for i in range(3)],
                   'controller_waypoint_position_m':[target[i]-bias[i] for i in range(3)]}
        manifest = {'correction':{'attempt_index':9,'parameters':{step:spec}},'motion_failure':failure}
        report = {'checks':[{'group':'geometry','code':'sampled_intersections','status':'pass'}]}
        self.assertIsNotNone(refine_probe(manifest, report, step))
        failure['controller_waypoint_position_m'][0] += .02
        self.assertIsNone(refine_probe(manifest, report, step))

    def test_unstable_compensated_probe_is_classified_for_safe_stop(self):
        step = 'ui_pick_left_copper_electrode'
        target = [.42746448516845703, -.012800214812159538, .48]
        bias = [.03959624767303466, -.013182774186134338, -.028708240985870326]
        error = [.03308182954788208, -.0005594007670879364, -.029653083086013776]
        command = [target[i] - bias[i] for i in range(3)]
        manifest = {'correction': {'attempt_index':10,'parameters':{step:{
            'orientation_probe_bias_xyz_m':bias}}}, 'motion_failure': {
            'phase':'pick_electrode/align/orientation_probe','failed_waypoint_index':0,
            'failed_waypoint_position_m':target,'actual_ee_position_m':[target[i]+error[i] for i in range(3)],
            'failed_waypoint_error_m':error,'controller_waypoint_position_m':command}}
        report = {'checks':[{'group':'geometry','code':'sampled_intersections','status':'pass'}]}
        issue = {'code':'execution_failed','status':'fail','step_id':step,
                 'message':'安全高位姿态预检未找到可达的侧向夹持角度: measured'}
        self.assertTrue(_unreachable_probe_issue(issue, manifest, report))
        self.assertIsNone(refine_probe(manifest, report, step))
        self.assertTrue(unstable_probe(manifest, report, step))

    def test_uncompensated_probe_requires_bound_measured_residual(self):
        step = 'ui_pick_left_copper_electrode'
        bias = [.028, -.013, -.021]
        spec = {'side_pose_observed_bias_xyz_m':bias, 'orientation_wxyz':[.7071067811865476,0,0,-.7071067811865476],
                'transit_height_m':.48}
        manifest = {'correction':{'attempt_index':8,'parameters':{step:spec}},
                    'motion_failure':{'phase':'pick_electrode/align/orientation_probe',
                     'failed_waypoint_index':0,'failed_waypoint_position_m':[.427,-.013,.48],
                     'actual_ee_position_m':[.455,-.026,.459], 'failed_waypoint_error_m':bias}}
        report = {'checks':[{'group':'geometry','code':'sampled_intersections','status':'pass'}]}
        self.assertIsNotNone(uncompensated_probe(manifest, report, step))
        manifest['motion_failure']['actual_ee_position_m'][0] += .02
        self.assertIsNone(uncompensated_probe(manifest, report, step))
        spec['orientation_probe_bias_xyz_m'] = [.02,.02,.02]
        with self.assertRaises(ValueError):
            validate_repair_profile({'source_run_id':'a'*32,'actions':{step:spec}}, 'a'*32)

    def test_only_evidenced_target_timeout_can_be_retried(self):
        step = 'ui_pick_left_copper_electrode'
        effect = {'comparable': True, 'status':'regressed', 'preservation_violations':[],
                  'new_failures':[{'group':'records', 'code':'execution_failed', 'step_id':step,
                                   'message':'机械臂动作超时: measured'}]}
        archive = {'snapshot': {'state_uncertain': True},
                   'events':[{'type':'step.failed','step_id':step}]}
        manifest = {'correction': {'fault_step_ids':[step], 'dependency_plan':{
            'checkpoint_step_id':'park', 'rerun_step_ids':[step]}}}
        self.assertTrue(retryable_timeout(effect, archive, manifest))
        effect['preservation_violations'] = ['confirmed_prefix']
        self.assertFalse(retryable_timeout(effect, archive, manifest))
        effect['preservation_violations'] = []
        effect['new_failures'][0]['step_id'] = 'other'
        self.assertFalse(retryable_timeout(effect, archive, manifest))

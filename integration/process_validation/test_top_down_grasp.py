"""Tests for evidence-bounded top-down clearance repairs."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from first_transfer.repair_execution import resumed_gripper_open_target
from process_validation.correction import build_correction_plan
from process_validation.geometry import transform
from process_validation.repairs.profile import validate_repair_profile
from process_validation.repairs.strategies.horizontal_grasp import TOP_DOWN_HORIZONTAL_WXYZ
from process_validation.repairs.strategies.top_down_grasp import (
    FINGER_PATH, FORK_PATH, SIDE_GEOMETRY, measured_top_down_fork_clearance,
    measured_top_down_release_gap, retryable_top_down_fork_collision,
    retryable_top_down_release_gap,
)


RUN_ID = 'c' * 32
PICK = 'ui_pick_left_copper_electrode'
CHECKPOINT = 'ui_park_pipette'


def _scene(side='left'):
    paths = SIDE_GEOMETRY[side]
    matrix = np.eye(4)
    matrix[3, 2] = .005
    inverse = np.linalg.inv(matrix)
    size = .01
    local_corners = [[x, y, z] for x in (-size / 2, size / 2)
                     for y in (-size / 2, size / 2) for z in (-size / 2, size / 2)]
    return {
        'run_id': RUN_ID,
        'bodies': {paths['finger_path']: {
            'points': [[-.002, -.002, .009], [.002, -.002, .009], [0., .002, .009]],
            'triangles': [[0, 1, 2]],
        }},
        'obstacles': [{
            'path': paths['fork_path'], 'shape': 'exact_usd_cube', 'size': size,
            'matrix': matrix.tolist(), 'inverse_matrix': inverse.tolist(),
            'corners': transform(local_corners, matrix).tolist(),
        }],
    }


def _records(root, side='left'):
    paths = SIDE_GEOMETRY[side]
    pick_step, place_step = paths['pick_step'], paths['place_step']
    params = {
        'orientation_wxyz': list(TOP_DOWN_HORIZONTAL_WXYZ),
        'orientation_candidates_wxyz': [list(TOP_DOWN_HORIZONTAL_WXYZ)],
        'transit_height_m': .48,
        'top_down_vertical_descent': True,
        'top_down_approach_z_m': .32,
        'orientation_probe_at_pregrasp': True,
        'high_transit_orientation_wxyz': list(TOP_DOWN_HORIZONTAL_WXYZ),
        'high_transit_destination_pose_bias': False,
        'high_destination_position_feedback': False,
        'approach_offset_xy_m': [0., 0.],
        'gripper_open_joint_target_m': .012,
    }
    collision = {'body': paths['finger_path'], 'obstacle': paths['fork_path']}
    manifest = {
        'run_id': RUN_ID,
        'correction': {
            'attempt_index': 12,
            'candidate_id': 'electrode_top_down_after_upward_pregrasp_12',
            'side': side, 'fault_step_ids': [pick_step],
            'dependency_plan': {'checkpoint_step_id': CHECKPOINT,
                                'rerun_step_ids': [pick_step, place_step]},
            'parameters': {pick_step: params},
            'pose_preflight': {'pick_electrode/align': {
                'closing_axis_world': [1., 0., 0.],
                'gripper_approach_axis_world': [0., 0., -1.]}},
        },
        'motion_failure': {'phase': 'pick_electrode/align',
                           'preflight_collision': [collision]},
    }
    issue = {'group': 'geometry', 'code': 'fixture_intersection', 'status': 'fail',
             'step_id': pick_step, 'frame_index': 1, **collision}
    report = {'run_id': RUN_ID, 'issues': [
        issue,
        {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
         'step_id': pick_step, 'message': '预抓取路径发现手部网格与支架相交'},
    ], 'checks': []}
    geometry = _scene(side)
    trace = {'run_id': RUN_ID, 'frame_index': 1, 'step_id': pick_step,
             'body_transforms': {paths['finger_path']: np.eye(4).tolist()}}
    archive = {
        'snapshot': {'run_id': RUN_ID, 'state_uncertain': True,
                     'completed_steps': 1, 'state_revision': 1,
                     'plan': [{'step_id': CHECKPOINT, 'action': 'park_pipette'},
                              {'step_id': pick_step, 'action': 'pick_electrode'},
                              {'step_id': place_step, 'action': 'place_electrode'}]},
        'events': [
            {'type': 'step.completed', 'step_id': CHECKPOINT},
            {'type': 'step.failed', 'step_id': pick_step,
             'payload': {'executor_receipt': {'reason': '预抓取路径发现手部网格与支架相交'}}},
        ],
    }
    (root / 'process_validation').mkdir(parents=True)
    (root / 'video_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (root / 'run.json').write_text(json.dumps(archive), encoding='utf-8')
    (root / 'process_validation' / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False), encoding='utf-8')
    (root / 'process_validation' / 'geometry.json').write_text(
        json.dumps(geometry), encoding='utf-8')
    (root / 'process_validation' / 'trace.jsonl').write_text(
        json.dumps(trace) + '\n', encoding='utf-8')
    return manifest, report, archive


class TopDownForkRepairTests(unittest.TestCase):
    def test_rebuilt_free_gripper_restores_full_open_target(self):
        self.assertEqual(resumed_gripper_open_target(True, False), .04)
        self.assertIsNone(resumed_gripper_open_target(False, False))
        self.assertIsNone(resumed_gripper_open_target(True, True))

    def test_full_open_restore_is_explicit_and_pick_only(self):
        profile = {'source_run_id': RUN_ID, 'routes': {}, 'actions': {
            PICK: {'orientation_wxyz': [1., 0., 0., 0.],
                   'transit_height_m': .42,
                   'restore_full_open_before_pick': True},
        }}
        validate_repair_profile(profile, RUN_ID)
        profile['actions'] = {
            'ui_place_left_copper_electrode': {
                'orientation_wxyz': [1., 0., 0., 0.],
                'transit_height_m': .42,
                'restore_full_open_before_pick': True,
            },
        }
        with self.assertRaises(ValueError):
            validate_repair_profile(profile, RUN_ID)

    def test_measured_fork_hit_derives_only_a_collision_free_bounded_offset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, report, _ = _records(root)
            evidence = measured_top_down_fork_clearance(manifest, report, root)
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence['target_offset_z_m'], .008)
            self.assertGreaterEqual(evidence['predicted_clearance_m'], .005)
            self.assertEqual(evidence['shifted_collision_check'], 'pass')
            manifest['correction']['parameters'][PICK]['orientation_wxyz'] = [1., 0., 0., 0.]
            self.assertIsNone(measured_top_down_fork_clearance(manifest, report, root))

    def test_right_side_uses_its_measured_finger_and_support_fork(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, report, _ = _records(root, side='right')
            evidence = measured_top_down_fork_clearance(manifest, report, root)
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence['side'], 'right')
            self.assertEqual(evidence['body'], SIDE_GEOMETRY['right']['finger_path'])
            self.assertEqual(evidence['obstacle'], SIDE_GEOMETRY['right']['fork_path'])
            self.assertAlmostEqual(evidence['measured_penetration_m'], .001, places=6)
            self.assertGreaterEqual(evidence['predicted_clearance_m'], .005)
            report['issues'][0]['obstacle'] = FORK_PATH
            self.assertIsNone(measured_top_down_fork_clearance(manifest, report, root))

    def test_retry_gate_requires_only_the_exact_measured_fork_collision(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, report, archive = _records(root)
            effect = {'comparable': True, 'status': 'regressed',
                      'preservation_violations': [],
                      'new_failures': [
                          {'group': 'geometry', 'code': 'fixture_intersection', 'step_id': PICK},
                          {'group': 'records', 'code': 'execution_failed', 'step_id': PICK},
                      ]}
            self.assertTrue(retryable_top_down_fork_collision(
                effect, archive, manifest, report, root))
            effect['preservation_violations'] = ['confirmed_prefix']
            self.assertFalse(retryable_top_down_fork_collision(
                effect, archive, manifest, report, root))

    def test_retry_gate_accepts_only_the_exact_measured_right_fork_collision(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, report, archive = _records(root, side='right')
            pick = SIDE_GEOMETRY['right']['pick_step']
            effect = {'comparable': True, 'status': 'regressed',
                      'preservation_violations': [],
                      'new_failures': [
                          {'group': 'geometry', 'code': 'fixture_intersection', 'step_id': pick},
                          {'group': 'records', 'code': 'execution_failed', 'step_id': pick},
                      ]}
            self.assertTrue(retryable_top_down_fork_collision(
                effect, archive, manifest, report, root))
            effect['new_failures'][0]['step_id'] = PICK
            self.assertFalse(retryable_top_down_fork_collision(
                effect, archive, manifest, report, root))

    def test_release_retry_requires_measured_24mm_gap_and_settled_pose(self):
        place = 'ui_place_left_copper_electrode'
        manifest = {'run_id': RUN_ID, 'correction': {
            'attempt_index': 13,
            'candidate_id': 'electrode_top_down_fork_clearance_pick_13',
            'side': 'left',
            'dependency_plan': {'rerun_step_ids': [PICK, place]},
            'parameters': {
                PICK: {'top_down_vertical_descent': True,
                       'top_down_grasp_target_offset_z_m': .008,
                       'orientation_wxyz': list(TOP_DOWN_HORIZONTAL_WXYZ),
                       'gripper_open_joint_target_m': .012},
                place: {'orientation_wxyz': list(TOP_DOWN_HORIZONTAL_WXYZ)},
            },
        }}
        archive = {'snapshot': {'state_uncertain': True}, 'events': [
            {'type': 'step.completed', 'step_id': PICK},
            {'type': 'step.failed', 'step_id': place, 'payload': {
                'executor_receipt': {
                    'reason': 'left侧电极释放后夹爪仍未打开: 0.024000 m',
                    'state_observation': {
                        'robot': {'gripper_gap_m': .024},
                        'semantic_components': {
                            'held_electrode_side': 'left',
                            'electrode_installed': {'left': False},
                        },
                    },
                },
            }},
        ]}
        issues = [
            {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
             'step_id': place,
             'message': 'left侧电极释放后夹爪仍未打开: 0.024000 m'},
            {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
             'step_id': None,
             'message': 'left侧电极释放后夹爪仍未打开: 0.024000 m'},
            {'group': 'geometry', 'code': 'upward_grasp_axis', 'status': 'fail',
             'step_id': place, 'frame_index': 1},
            {'group': 'geometry', 'code': 'vertical_grasp_axis', 'status': 'fail',
             'step_id': place, 'frame_index': 2},
        ]
        report = {'issues': issues}
        trace = [
            {'frame_index': 1, 'step_id': place,
             'phase': 'place_electrode/align_and_insert/safe_staging'},
            {'frame_index': 2, 'step_id': place,
             'phase': 'place_electrode/align_and_insert/orientation_probe'},
            {'frame_index': 3, 'step_id': place,
             'phase': 'place_electrode/align_and_insert',
             'gripper_approach_axis_world': [0., 0., -1.],
             'closing_axis_world': [1., 0., 0.]},
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'process_validation').mkdir()
            (root / 'process_validation' / 'trace.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in trace), encoding='utf-8')
            evidence = measured_top_down_release_gap(
                manifest, report, archive, root)
            self.assertIsNotNone(evidence)
            self.assertAlmostEqual(evidence['measured_gap_m'], .024)
            effect = {'comparable': True, 'status': 'regressed',
                      'preservation_violations': [],
                      'new_failures': [
                          {'group': 'records', 'code': 'execution_failed', 'step_id': place},
                          {'group': 'geometry', 'code': 'upward_grasp_axis', 'step_id': place},
                          {'group': 'geometry', 'code': 'vertical_grasp_axis', 'step_id': place},
                      ]}
            self.assertTrue(retryable_top_down_release_gap(
                effect, archive, manifest, report, root))
            trace[-1]['closing_axis_world'] = [0., 0., 1.]
            (root / 'process_validation' / 'trace.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in trace), encoding='utf-8')
            self.assertFalse(retryable_top_down_release_gap(
                effect, archive, manifest, report, root))

    def test_right_release_retry_requires_confirmed_right_electrode_state(self):
        side = 'right'
        pick = SIDE_GEOMETRY[side]['pick_step']
        place = SIDE_GEOMETRY[side]['place_step']
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, archive = _records(root, side=side)
            manifest['correction'].update({
                'attempt_index': 13,
                'candidate_id': 'electrode_top_down_fork_clearance_pick_13',
                'dependency_plan': {'rerun_step_ids': [pick, place]},
            })
            manifest['correction']['parameters'][pick]['top_down_grasp_target_offset_z_m'] = .008
            manifest['correction']['parameters'][place] = {
                'orientation_wxyz': list(TOP_DOWN_HORIZONTAL_WXYZ),
            }
            archive['snapshot']['state_uncertain'] = True
            archive['events'] = [
                    {'type': 'step.completed', 'step_id': CHECKPOINT},
                    {'type': 'step.completed', 'step_id': pick},
                    {'type': 'step.failed', 'step_id': place, 'payload': {
                        'executor_receipt': {
                            'reason': 'right侧电极释放后夹爪仍未打开: 0.024000 m',
                            'state_observation': {
                                'robot': {'gripper_gap_m': .024},
                                'semantic_components': {
                                    'held_electrode_side': side,
                                    'electrode_installed': {side: False},
                                },
                            },
                        },
                    }},
                ]
            issue = {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
                     'step_id': place,
                     'message': 'right侧电极释放后夹爪仍未打开: 0.024000 m'}
            report = {'issues': [issue]}
            trace = [{'frame_index': 1, 'step_id': place,
                      'phase': 'place_electrode/align_and_insert',
                      'gripper_approach_axis_world': [0., 0., -1.],
                      'closing_axis_world': [1., 0., 0.]}]
            (root / 'process_validation' / 'trace.jsonl').write_text(
                json.dumps(trace[0]) + '\n', encoding='utf-8')
            evidence = measured_top_down_release_gap(
                manifest, report, archive, root, pick, place)
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence['side'], side)
            self.assertAlmostEqual(evidence['measured_gap_m'], .024)
            effect = {'comparable': True, 'status': 'regressed',
                      'preservation_violations': [],
                      'new_failures': [
                          {'group': 'records', 'code': 'execution_failed', 'step_id': place},
                      ]}
            self.assertTrue(retryable_top_down_release_gap(
                effect, archive, manifest, report, root))
            (root / 'video_manifest.json').write_text(
                json.dumps(manifest), encoding='utf-8')
            (root / 'run.json').write_text(
                json.dumps(archive), encoding='utf-8')
            (root / 'process_validation' / 'report.json').write_text(
                json.dumps({'run_id': RUN_ID, **report}, ensure_ascii=False),
                encoding='utf-8')
            plan = build_correction_plan(root)
            self.assertTrue(plan['eligible'], plan)
            self.assertEqual(plan['attempt_index'], 14)
            self.assertEqual(plan['candidate_id'], 'electrode_top_down_release_opening_14')
            self.assertEqual(plan['stop_after_step'], place)
            self.assertEqual(plan['actions'][place]['gripper_open_joint_target_m'], .012)
            manifest['correction']['side'] = 'left'
            self.assertIsNone(measured_top_down_release_gap(
                manifest, report, archive, root, pick, place))

    def test_correction_plan_offsets_pick_but_preserves_final_installation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _ = _records(root)
            manifest = json.loads((root / 'video_manifest.json').read_text(encoding='utf-8'))
            manifest['correction']['attempt_index'] = 12
            manifest['correction']['side'] = 'left'
            (root / 'video_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            plan = build_correction_plan(root)
            self.assertTrue(plan['eligible'], plan)
            self.assertEqual(plan['attempt_index'], 13)
            self.assertEqual(plan['candidate_id'], 'electrode_top_down_fork_clearance_pick_13')
            pick = plan['actions'][PICK]
            self.assertEqual(pick['top_down_grasp_target_offset_z_m'], .008)
            self.assertTrue(pick['top_down_vertical_descent'])
            self.assertNotIn('side_pose_observed_bias_xyz_m', pick)
            self.assertTrue(plan['final_target_unchanged'])
            validate_repair_profile(plan, RUN_ID)

    def test_right_correction_plan_targets_right_electrode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _records(root, side='right')
            manifest = json.loads((root / 'video_manifest.json').read_text(encoding='utf-8'))
            manifest['correction']['attempt_index'] = 12
            (root / 'video_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            plan = build_correction_plan(root)
            pick = SIDE_GEOMETRY['right']['pick_step']
            place = SIDE_GEOMETRY['right']['place_step']
            self.assertTrue(plan['eligible'], plan)
            self.assertEqual(plan['side'], 'right')
            self.assertIn(pick, plan['actions'])
            self.assertEqual(plan['actions'][pick]['top_down_grasp_target_offset_z_m'], .008)
            self.assertIn(place, plan['actions'])
            self.assertTrue(plan['final_target_unchanged'])
            validate_repair_profile(plan, RUN_ID)


if __name__ == '__main__':
    unittest.main()

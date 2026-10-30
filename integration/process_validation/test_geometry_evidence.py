"""Fault injection for geometry preflight and pose evidence, CPU only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.geometry_evidence import unit_axis, GeometryEvidenceError
from process_validation.geometry_checks import check_trace
from process_validation.repairs.pose_checks import check_registered_pose
from process_validation.repairs.strategies.palm_clearance import predict_opposed_hand_clearance
from process_validation.report import Report

RUN = 'a' * 32
STEP = 'ui_pick_left_lead'
POLICY = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))


class GeometryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / RUN
        self.root = self.folder / 'process_validation'
        self.root.mkdir(parents=True)
        paths = [f'/robot/{name}/visuals/{name}' for name in ('panda_hand', 'panda_leftfinger', 'panda_rightfinger')]
        self.geometry = {'run_id': RUN, 'pose_source': 'physx_articulation_body_poses',
            'bound_body_count': 3, 'unsupported_meshes': [],
            'bodies': {path: {'points': [[-.01, -.01, 0.], [.01, -.01, 0.], [0., .01, 0.]],
                              'triangles': [[0, 1, 2]]} for path in paths},
            'obstacles': [{'path': '/fixture/crossbar', 'size': .1,
                'matrix': np.eye(4).tolist(), 'inverse_matrix': np.eye(4).tolist(),
                'corners': [[x, y, z] for x in (-.05, .05) for y in (-.05, .05) for z in (-.05, .05)]}]}
        matrix = np.eye(4); matrix[3, 0] = .5
        self.sample = {'run_id': RUN, 'frame_index': 0, 'step_id': STEP, 'action': 'pick_lead',
            'phase': 'pick_lead/grip', 'simulation_time_s': 2., 'state_revision': 0,
            'completed_steps': 0, 'voltage_valid': False,
            'closing_axis_world': [1., 0., 0.], 'gripper_approach_axis_world': [0., 0., -1.],
            'body_transforms': {path: matrix.tolist() for path in paths}}
        self.frame = {'frame': 0, 'step_id': STEP, 'phase': 'pick_lead/grip',
                      'simulation_time_s': 2., 'robot_grasp_frame_m': [.5, 0., 0.], 'gripper_gap_m': .01}
        self.manifest = {'run_id': RUN, 'fps': 12, 'frames': [self.frame]}

    def predict(self, samples=None):
        (self.root/'geometry.json').write_text(json.dumps(self.geometry), encoding='utf-8')
        (self.root/'trace.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in (samples or [self.sample])), encoding='utf-8')
        return predict_opposed_hand_clearance(self.folder, self.manifest, 135.)

    def test_valid_noncolliding_samples_pass_without_modifying_inputs(self):
        before = deepcopy((self.geometry, self.manifest, self.sample))
        result = self.predict()
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['sample_frames'], 1)
        self.assertEqual((self.geometry, self.manifest, self.sample), before)

    def test_known_collision_still_fails(self):
        self.frame['robot_grasp_frame_m'] = [0., 0., 0.]
        for matrix in self.sample['body_transforms'].values():
            matrix[3][0] = 0.
        result = self.predict()
        self.assertFalse(result['passed'])
        self.assertEqual(result['predicted_intersections'], 3)

    def test_empty_mesh_or_obstacle_collection_never_passes(self):
        for key, empty in [('bodies', {}), ('obstacles', [])]:
            with self.subTest(key=key):
                original = self.geometry[key]
                self.geometry[key] = empty
                self.assertFalse(self.predict()['passed'])
                self.geometry[key] = original

    def test_invalid_meshes_never_pass(self):
        key = next(iter(self.geometry['bodies']))
        good = deepcopy(self.geometry['bodies'][key])
        for points, faces in [([], []), (good['points'], []),
                (good['points'], [[0, 1, 9]]), (good['points'], [[0., .5, 2.]]),
                (good['points'], [[0, 0, 0]]),
                ([[float('nan'), 0., 0.], [.01, 0., 0.], [0., .01, 0.]], [[0, 1, 2]])]:
            with self.subTest(faces=faces):
                self.geometry['bodies'][key] = {'points': points, 'triangles': faces}
                self.assertFalse(self.predict()['passed'])

    def test_invalid_fixture_cannot_make_broad_phase_miss_a_collision(self):
        good = deepcopy(self.geometry['obstacles'][0])
        for mutation in ('size', 'inverse', 'corners', 'missing_matrix'):
            with self.subTest(mutation=mutation):
                row = deepcopy(good)
                if mutation == 'size': row['size'] = -1.
                elif mutation == 'inverse': row['inverse_matrix'][3][0] = 10.
                elif mutation == 'corners': row['corners'][0][0] += 10.
                else: del row['matrix']
                self.geometry['obstacles'] = [row]
                self.assertFalse(self.predict()['passed'])

    def test_invalid_transforms_pivots_and_asset_units_are_rejected(self):
        good = deepcopy(self.sample['body_transforms'])
        for mutation in ('missing', 'nan', 'singular', 'wrong_units', 'invalid_pivot'):
            with self.subTest(mutation=mutation):
                self.sample['body_transforms'] = deepcopy(good)
                self.frame['robot_grasp_frame_m'] = [.5, 0., 0.]
                key = next(iter(good))
                matrix = self.sample['body_transforms'][key]
                if mutation == 'missing': del self.sample['body_transforms'][key]
                elif mutation == 'nan': matrix[0][0] = float('nan')
                elif mutation == 'singular': matrix[0][:3] = [0., 0., 0.]
                elif mutation == 'wrong_units': matrix[0][0] = 1000.
                else: self.frame['robot_grasp_frame_m'] = [float('inf'), 0., 0.]
                self.assertFalse(self.predict()['passed'])

    def test_foreign_runs_gaps_and_moved_obstacles_are_rejected(self):
        for mutation in ('geometry_run', 'manifest_run', 'trace_run', 'index', 'time', 'phase', 'moved'):
            with self.subTest(mutation=mutation):
                before = deepcopy((self.geometry, self.manifest, self.sample))
                if mutation == 'geometry_run': self.geometry['run_id'] = 'b'*32
                elif mutation == 'manifest_run': self.manifest['run_id'] = 'b'*32
                elif mutation == 'trace_run': self.sample['run_id'] = 'b'*32
                elif mutation == 'index': self.sample['frame_index'] = -1
                elif mutation == 'time': self.sample['simulation_time_s'] += 1.
                elif mutation == 'phase': self.sample['phase'] = 'pick_lead/other'
                else: self.sample['moved_obstacles'] = ['/fixture/crossbar']
                self.assertFalse(self.predict()['passed'])
                self.geometry, self.manifest, self.sample = before

    def test_unbound_or_unsupported_meshes_and_trace_errors_are_rejected(self):
        self.geometry['bound_body_count'] = 2
        self.assertFalse(self.predict()['passed'])
        self.geometry['bound_body_count'] = 3
        self.geometry['unsupported_meshes'] = ['missing_finger']
        self.assertFalse(self.predict()['passed'])
        self.geometry['unsupported_meshes'] = []
        (self.root/'trace_error.json').write_text('{}')
        self.assertFalse(self.predict()['passed'])

    def test_no_relevant_action_samples_cannot_pass(self):
        self.sample['action'] = 'measure_voltage'
        self.assertFalse(self.predict()['passed'])

    def test_unit_axes_allow_roundoff_but_reject_invalid_data(self):
        self.assertTrue(np.allclose(unit_axis([1.000001, 0., 0.]), [1., 0., 0.]))
        for axis in ([0., 0., 0.], [2., 0., 1.], [True, 0., 0.], ['1', 0., 0.], [float('nan'), 0., 0.]):
            with self.subTest(axis=axis), self.assertRaises(GeometryEvidenceError):
                unit_axis(axis)

    def pose(self, closing):
        sample = {**self.sample, 'closing_axis_world': closing}
        manifest = {**self.manifest, 'correction': {'routes': {STEP: {
            'orientation_wxyz': [0., .7071067811865476, .7071067811865476, 0.],
            'expected_closing_axis_world': [1., 0., 0.], 'expected_approach_axis_world': [0., 0., -1.]}}}}
        report = Report(RUN, POLICY)
        return check_registered_pose(sample, manifest, report, {'step_id': STEP}), report.finish()

    def test_scaled_direction_that_previously_passed_dot_product_is_rejected(self):
        passed, report = self.pose([2., 0., 1.])
        self.assertFalse(passed)
        self.assertEqual(report['status'], 'fail')

    def test_valid_horizontal_pose_passes_and_vertical_pose_fails(self):
        self.assertTrue(self.pose([1., 0., 0.])[0])
        self.assertFalse(self.pose([0., 0., 1.])[0])

    def test_partial_geometry_cannot_emit_aggregate_collision_free_pass(self):
        second = deepcopy(self.sample)
        second['frame_index'] = 1
        second['simulation_time_s'] += 1.
        second['body_transforms'].pop(next(iter(second['body_transforms'])))
        self.manifest['frames'].append({**self.frame, 'frame': 1, 'simulation_time_s': 3.})
        self.predict([self.sample, second])
        report = Report(RUN, POLICY)
        check_trace(self.root, {}, self.manifest, POLICY, report)
        rows = report.finish()['checks']
        self.assertFalse(any(r['code'] == 'sampled_intersections' and r['status'] == 'pass' for r in rows))


if __name__ == '__main__': unittest.main()

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from robot_checkpoint import make_robot_checkpoint, select_robot_checkpoint, verify_restored_robot
from pregrasp_planner import orientation_error_deg


RUNTIME = {'isaacsim': '5.0.0', 'isaaclab': '2.2.0'}
NAMES = ['panda_joint_' + str(i) for i in range(7)] + ['finger_left', 'finger_right']
STEP = 'ui_place_left_copper_electrode'


def fixture():
    robot = {'joint_names': NAMES, 'joint_positions': [.1, -.6, -.5, -2.1, -.3, 1.6, -.4, .04, .04],
             'joint_velocities': [0.] * 9, 'end_effector_position_m': [.4, 0., .48],
             'end_effector_orientation_wxyz': [0., 2**-.5, 2**-.5, 0.], 'gripper_gap_m': .08}
    observation = {'source': 'matterix_physx_articulation', 'robot': robot,
                   'semantic_components': {'held_electrode_side': None,
                                           'electrode_installed': {'left': True, 'right': False}}}
    receipt = {'run_id': 'a'*32, 'command_id': 'command1', 'step_id': STEP,
               'status': 'succeeded', 'state_observation': observation}
    archive = {'events': [{'type': 'step.completed', 'step_id': STEP,
                          'payload': {'executor_receipt': deepcopy(receipt)}},
                         {'type': 'step.failed', 'step_id': 'ui_pick_right_copper_electrode'}],
               'accepted_receipts': {'command1': {'receipt': deepcopy(receipt),
                                                   'result': {'state_revision': 1}}}}
    manifest = {'presentation': {'runtime': RUNTIME, 'continuity': {}}}
    return archive, manifest, receipt, observation


class RobotCheckpointTests(unittest.TestCase):
    def test_intermediate_success_uses_its_receipt_not_failed_suffix_pose(self):
        archive, manifest, _, observation = fixture()
        manifest['presentation']['continuity']['end_state'] = {
            'checkpoint_step_id': 'ui_pick_right_copper_electrode',
            'robot_joint_positions_rad': [9.] * 9}
        checkpoint = select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)
        self.assertEqual(checkpoint['source'], 'successful_step_receipt')
        self.assertEqual(checkpoint['robot'], observation['robot'])
        self.assertTrue(verify_restored_robot(checkpoint, observation['robot'])['verified'])

    def test_new_settled_checkpoint_is_saved_and_bound_to_exact_step(self):
        archive, manifest, receipt, observation = fixture()
        saved = make_robot_checkpoint(STEP, 1, receipt, observation, RUNTIME)
        manifest['presentation']['continuity']['checkpoints'] = {STEP: saved}
        checkpoint = select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)
        self.assertEqual(checkpoint['source'], 'step_checkpoint')
        # Branch reconstruction changes only run_id; original command identity stays valid.
        for row in [archive['events'][0]['payload']['executor_receipt'],
                    archive['accepted_receipts']['command1']['receipt']]:
            row['run_id'] = 'b'*32
        self.assertIsNotNone(select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES))
        saved['state_revision'] = 2
        with self.assertRaisesRegex(ValueError, '绑定'):
            select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)

    def test_terminal_legacy_end_state_still_restores_exact_success(self):
        archive, manifest, _, observation = fixture()
        robot = observation['robot']
        manifest['presentation']['continuity']['end_state'] = {
            'checkpoint_step_id': STEP, 'robot_joint_positions_rad': robot['joint_positions'],
            'end_effector_position_m': robot['end_effector_position_m'], 'gripper_gap_m': .08}
        checkpoint = select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)
        self.assertEqual(checkpoint['source'], 'source_end_state')

    def test_missing_or_unbound_receipt_cannot_fall_back_to_default_pose(self):
        archive, manifest, _, _ = fixture()
        archive['accepted_receipts']['command1']['result']['state_revision'] = 2
        with self.assertRaises(ValueError):
            select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)
        archive['accepted_receipts']['command1']['result']['state_revision'] = 1
        for r in [archive['events'][0]['payload']['executor_receipt'],
                  archive['accepted_receipts']['command1']['receipt']]:
            r.pop('state_observation')
        with self.assertRaisesRegex(ValueError, '没有机械臂'):
            select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)

    def test_runtime_joint_order_count_and_finite_values_are_required(self):
        for mutation in ('runtime', 'order', 'count', 'nan'):
            archive, manifest, _, _ = fixture()
            names, runtime = NAMES[:], dict(RUNTIME)
            if mutation == 'runtime': runtime['isaaclab'] = '3.0.0'
            if mutation == 'order': names.reverse()
            if mutation == 'count': names.pop()
            if mutation == 'nan':
                for r in [archive['events'][0]['payload']['executor_receipt'],
                          archive['accepted_receipts']['command1']['receipt']]:
                    r['state_observation']['robot']['joint_positions'][0] = float('nan')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                select_robot_checkpoint(archive, manifest, STEP, runtime, names)

    def test_forward_kinematics_gap_orientation_and_joint_mismatch_stop_before_motion(self):
        archive, manifest, _, observation = fixture()
        checkpoint = select_robot_checkpoint(archive, manifest, STEP, RUNTIME, NAMES)
        for field, value in [('end_effector_position_m', [.402, 0., .48]),
                             ('joint_positions', [0.] * 9), ('gripper_gap_m', .024),
                             ('gripper_gap_m', float('nan')),
                             ('end_effector_orientation_wxyz', [1., 0., 0., 0.])]:
            actual = deepcopy(observation['robot']); actual[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_restored_robot(checkpoint, actual)

    def test_already_downward_pose_needs_no_large_turn_and_sign_is_irrelevant(self):
        q = [0., 2**-.5, 2**-.5, 0.]
        self.assertAlmostEqual(orientation_error_deg(q, [-x for x in q]), 0.)
        self.assertGreater(orientation_error_deg([1., 0., 0., 0.], q), 30.)


if __name__ == '__main__':
    unittest.main()

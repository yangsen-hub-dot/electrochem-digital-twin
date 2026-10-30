"""Receipt-bound Franka checkpoints; arm positions are rad, finger positions m.

This module selects and verifies recorded articulation poses. It never invents a
pose or performs motion planning. Restore a settled pose with zero velocity, then
check simulator forward kinematics before recording or executing another action.
"""
from copy import deepcopy
import hashlib
import json
import math


def _vector(value, size, name):
    if (not isinstance(value, (list, tuple)) or len(value) != size
            or any(isinstance(x, bool) or not isinstance(x, (int, float))
                   or not math.isfinite(float(x)) for x in value)):
        raise ValueError('检查点缺少有效的' + name)
    return list(map(float, value))


def _receipt_digest(receipt):
    value = deepcopy(receipt)
    value.pop('run_id', None)  # A confirmed prefix keeps command IDs across branches.
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _robot_pose(robot, joint_names):
    pose = deepcopy(robot)
    pose['joint_positions'] = _vector(robot.get('joint_positions'), len(joint_names), '关节位置')
    pose['end_effector_position_m'] = _vector(robot.get('end_effector_position_m'), 3, '末端位置')
    if robot.get('joint_names') is not None and robot['joint_names'] != list(joint_names):
        raise ValueError('检查点关节名称或顺序与当前机械臂不一致')
    gap = robot.get('gripper_gap_m')
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
        raise ValueError('检查点缺少有效的夹爪开口')
    if robot.get('end_effector_orientation_wxyz') is not None:
        quat = _vector(robot['end_effector_orientation_wxyz'], 4, '末端姿态')
        if abs(sum(x*x for x in quat) - 1.0) > .001:
            raise ValueError('检查点末端姿态不是单位四元数')
        pose['end_effector_orientation_wxyz'] = quat
    return pose


def make_robot_checkpoint(step_id, revision, receipt, observation, runtime):
    """Capture a settled articulation state after one accepted step."""
    if (receipt.get('status') != 'succeeded' or receipt.get('step_id') != step_id
            or not receipt.get('command_id') or observation.get('source') != 'matterix_physx_articulation'):
        raise ValueError('机器人检查点必须绑定同一步的成功物理回执')
    if observation.get('semantic_components') != receipt.get('state_observation', {}).get('semantic_components'):
        raise ValueError('机器人检查点的组件状态与成功回执不一致')
    robot = observation.get('robot', {})
    names = robot.get('joint_names')
    if not isinstance(names, list) or not names or len(set(names)) != len(names):
        raise ValueError('机器人检查点缺少唯一的关节名称')
    return {'schema_version': '1.0', 'step_id': step_id, 'state_revision': revision,
            'command_id': receipt['command_id'], 'receipt_sha256': _receipt_digest(receipt),
            'runtime': deepcopy(runtime), 'robot': _robot_pose(robot, names),
            'semantic_components': deepcopy(observation['semantic_components'])}


def select_robot_checkpoint(archive, manifest, step_id, runtime, joint_names):
    """Use the requested successful step, never a failed suffix's robot pose."""
    completed = [e for e in archive.get('events', []) if e.get('type') == 'step.completed']
    matches = [(i + 1, e) for i, e in enumerate(completed) if e.get('step_id') == step_id]
    if len(matches) != 1:
        raise ValueError('机器人恢复点缺少唯一的成功步骤')
    revision, event = matches[0]
    receipt = event.get('payload', {}).get('executor_receipt') or {}
    accepted = archive.get('accepted_receipts', {}).get(receipt.get('command_id'), {})
    if (receipt.get('status') != 'succeeded' or receipt.get('step_id') != step_id
            or accepted.get('receipt') != receipt
            or accepted.get('result', {}).get('state_revision') != revision):
        raise ValueError('机器人恢复点的成功回执与账本版本不一致')
    presentation = manifest.get('presentation', {})
    source_runtime = presentation.get('runtime', {})
    for key in ('isaacsim', 'isaaclab'):
        if not source_runtime.get(key) or source_runtime[key] != runtime.get(key):
            raise ValueError('机器人检查点与当前仿真运行环境不一致')
    continuity = presentation.get('continuity', {})
    saved = continuity.get('checkpoints', {}).get(step_id)
    if saved is not None:
        if (saved.get('step_id') != step_id or saved.get('state_revision') != revision
                or saved.get('command_id') != receipt.get('command_id')
                or saved.get('receipt_sha256') != _receipt_digest(receipt)
                or saved.get('runtime') != source_runtime
                or saved.get('semantic_components') != receipt.get('state_observation', {}).get('semantic_components')):
            raise ValueError('机器人检查点绑定已变化')
        robot, source = saved.get('robot', {}), 'step_checkpoint'
    else:
        end = continuity.get('end_state', {})
        if (completed[-1].get('step_id') == step_id and end.get('checkpoint_step_id') == step_id
                and end.get('robot_joint_positions_rad')):
            robot = {'joint_positions': end['robot_joint_positions_rad'],
                     'end_effector_position_m': end.get('end_effector_position_m'),
                     'gripper_gap_m': end.get('gripper_gap_m')}
            source = 'source_end_state'
        else:
            observation = receipt.get('state_observation', {})
            if observation.get('source') != 'matterix_physx_articulation':
                raise ValueError('所选成功检查点没有机械臂实测姿态，停止局部重跑')
            robot, source = observation.get('robot', {}), 'successful_step_receipt'
    return {'step_id': step_id, 'state_revision': revision, 'command_id': receipt['command_id'],
            'receipt_sha256': _receipt_digest(receipt), 'source': source,
            'robot': _robot_pose(robot, joint_names)}


def verify_restored_robot(checkpoint, actual):
    """Fail before motion if articulation state/FK differs from the saved step."""
    saved = checkpoint['robot']
    q = _vector(actual.get('joint_positions'), len(saved['joint_positions']), '恢复后关节位置')
    joint_error = max(abs(a-b) for a, b in zip(q, saved['joint_positions']))
    position = _vector(actual.get('end_effector_position_m'), 3, '恢复后末端位置')
    position_error = math.dist(position, saved['end_effector_position_m'])
    actual_gap = _vector([actual.get('gripper_gap_m')], 1, '恢复后夹爪开口')[0]
    gap_error = abs(actual_gap - saved['gripper_gap_m'])
    if joint_error > 1e-5 or position_error > .001 or gap_error > .0005:
        raise ValueError('机械臂检查点恢复偏差超限，停止局部重跑')
    orientation_error_deg = None
    if saved.get('end_effector_orientation_wxyz') is not None:
        if __package__:
            from .pregrasp_planner import orientation_error_deg as quat_error
        else:
            from pregrasp_planner import orientation_error_deg as quat_error
        orientation_error_deg = quat_error(actual.get('end_effector_orientation_wxyz'),
                                           saved['end_effector_orientation_wxyz'])
        if orientation_error_deg > 1.0:
            raise ValueError('机械臂检查点恢复朝向偏差超过1度')
    return {'verified': True, 'joint_position_max_error': joint_error,
            'end_effector_position_error_m': position_error, 'gripper_gap_error_m': gap_error,
            'orientation_error_deg': orientation_error_deg, 'velocities_reset_to_zero': True}

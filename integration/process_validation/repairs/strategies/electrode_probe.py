"""Identify an evidenced, uncompensated high-orientation probe."""
import math


def uncompensated_probe(manifest, report, step_id):
    correction = manifest.get('correction') or {}
    params = (correction.get('parameters') or {}).get(step_id) or {}
    failure = manifest.get('motion_failure') or {}
    bias = params.get('side_pose_observed_bias_xyz_m')
    target = failure.get('failed_waypoint_position_m')
    actual = failure.get('actual_ee_position_m')
    error = failure.get('failed_waypoint_error_m')
    orientation = params.get('orientation_wxyz')
    vectors = (bias, target, actual, error)
    if (correction.get('attempt_index') != 8
            or failure.get('phase') != 'pick_electrode/align/orientation_probe'
            or failure.get('failed_waypoint_index') != 0
            or params.get('orientation_probe_bias_xyz_m') is not None
            or failure.get('controller_waypoint_position_m') is not None
            or any(not isinstance(v, list) or len(v) != 3 or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in v) for v in vectors)
            or not isinstance(orientation, list) or len(orientation) != 4
            or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in orientation)
            or not .42 <= target[2] <= .55
            or any(abs(actual[i] - target[i] - error[i]) > .001 for i in range(3))
            or math.dist(error, bias) > .004 or max(abs(x) for x in bias) > .05
            or not any(x.get('group') == 'geometry' and x.get('code') == 'sampled_intersections'
                       and x.get('status') == 'pass' for x in report.get('checks', []))):
        return None
    command = [target[i] - bias[i] for i in range(3)]
    if not (.20 <= command[0] <= .50 and -.20 <= command[1] <= .20 and .42 <= command[2] <= .55):
        return None
    return {'bias': bias, 'orientation': orientation, 'target': target,
            'measured_error': error, 'controller_command': command}


def refine_probe(manifest, report, step_id):
    correction = manifest.get('correction') or {}
    params = (correction.get('parameters') or {}).get(step_id) or {}
    failure = manifest.get('motion_failure') or {}
    target, actual = failure.get('failed_waypoint_position_m'), failure.get('actual_ee_position_m')
    error, command = failure.get('failed_waypoint_error_m'), failure.get('controller_waypoint_position_m')
    bias, orientation = params.get('orientation_probe_bias_xyz_m'), params.get('orientation_wxyz')
    vectors = (target, actual, error, command, bias)
    if (correction.get('attempt_index') != 9
            or failure.get('phase') != 'pick_electrode/align/orientation_probe'
            or failure.get('failed_waypoint_index') != 0
            or any(not isinstance(v, list) or len(v) != 3 or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in v) for v in vectors)
            or not isinstance(orientation, list) or len(orientation) != 4
            or not .42 <= target[2] <= .55
            or any(abs(actual[i] - target[i] - error[i]) > .001 for i in range(3))
            or any(abs(target[i] - command[i] - bias[i]) > .001 for i in range(3))
            or not .008 < math.dist(error, [0, 0, 0]) <= .018
            or not any(x.get('group') == 'geometry' and x.get('code') == 'sampled_intersections'
                       and x.get('status') == 'pass' for x in report.get('checks', []))):
        return None
    refined = [bias[i] + error[i] for i in range(3)]
    commanded = [target[i] - refined[i] for i in range(3)]
    if (max(abs(x) for x in refined) > .05 or not .015 <= math.dist(refined, [0, 0, 0]) <= .06
            or not (.20 <= commanded[0] <= .50 and -.20 <= commanded[1] <= .20
                    and .42 <= commanded[2] <= .55)):
        return None
    return {'bias': refined, 'orientation': orientation, 'target': target,
            'measured_error': error, 'controller_command': commanded}


def unstable_probe(manifest, report, step_id):
    correction = manifest.get('correction') or {}
    failure = manifest.get('motion_failure') or {}
    params = (correction.get('parameters') or {}).get(step_id) or {}
    target, actual = failure.get('failed_waypoint_position_m'), failure.get('actual_ee_position_m')
    error = failure.get('failed_waypoint_error_m')
    command, bias = failure.get('controller_waypoint_position_m'), params.get('orientation_probe_bias_xyz_m')
    vectors = (target, actual, error, command, bias)
    return (correction.get('attempt_index') == 10
            and failure.get('phase') == 'pick_electrode/align/orientation_probe'
            and failure.get('failed_waypoint_index') == 0
            and all(isinstance(v, list) and len(v) == 3 and all(
                isinstance(x, (int, float)) and math.isfinite(x) for x in v) for v in vectors)
            and math.dist(error, [0., 0., 0.]) > .018
            and all(abs(target[i] - command[i] - bias[i]) <= .001 for i in range(3))
            and all(abs(actual[i] - target[i] - error[i]) <= .001 for i in range(3))
            and any(x.get('group') == 'geometry' and x.get('code') == 'sampled_intersections'
                    and x.get('status') == 'pass' for x in report.get('checks', [])))


def upward_pregrasp_descent(manifest, report, step_id, failure):
    """Recognize a bounded descent timeout after a clear, stable pregrasp probe."""
    correction = manifest.get('correction') or {}
    params = (correction.get('parameters') or {}).get(step_id) or {}
    preflight = ((correction.get('pose_preflight') or {}).get('pick_electrode/align') or {})
    axes = (preflight.get('closing_axis_world'), preflight.get('gripper_approach_axis_world'))
    target, actual = failure.get('failed_waypoint_position_m'), failure.get('actual_ee_position_m')
    error, requested = failure.get('failed_waypoint_error_m'), failure.get('requested_final_position_m')
    waypoints = failure.get('planned_waypoints_m')
    recorded = [row for row in correction.get('orientation_probe_segments', [])
                if isinstance(row, dict) and row.get('phase') == 'pick_electrode/align/orientation_probe']
    segments = recorded[-1].get('segments') if recorded else None
    orientation = params.get('orientation_wxyz')
    clear = any(row.get('group') == 'geometry' and row.get('code') == 'sampled_intersections'
                and row.get('status') == 'pass' for row in (report or {}).get('checks', []))
    intersections = [row for row in (report or {}).get('issues', [])
                     if row.get('group') == 'geometry' and row.get('code') == 'fixture_intersection'
                     and row.get('status') == 'fail' and row.get('step_id') == step_id]
    vectors = (target, actual, error, requested, *axes)
    if (correction.get('attempt_index') != 11
            or correction.get('candidate_id') != 'electrode_slot_pregrasp_kinematic_probe_11'
            or correction.get('strategy_family') not in (None, 'electrode_pose_path')
            or params.get('orientation_probe_at_pregrasp') is not True
            or params.get('side_pose_observed_bias_xyz_m') is not None
            or params.get('orientation_probe_bias_xyz_m') is not None
            or failure.get('phase') != 'pick_electrode/align'
            or failure.get('failed_waypoint_index') != 1
            or not isinstance(waypoints, list) or len(waypoints) != 2
            or not all(isinstance(v, list) and len(v) == 3 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in v) for v in waypoints)
            or not all(isinstance(v, list) and len(v) == 3 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in v) for v in vectors)
            or not all(isinstance(axis, list) and len(axis) == 3 and all(
                isinstance(x, (int, float)) and math.isfinite(x) for x in axis) for axis in axes)
            or not isinstance(orientation, list) or len(orientation) != 4
            or not clear or intersections or not isinstance(segments, list) or not segments):
        return None
    if (any(abs(float(actual[i]) - float(target[i]) - float(error[i])) > .001 for i in range(3))
            or math.dist(target, waypoints[-1]) > .001
            or math.dist(requested, waypoints[-1]) > .001
            or math.dist(actual, target) < .05
            or abs(float(axes[0][2])) > .2 or float(axes[1][2]) < .85):
        return None
    for index, row in enumerate(segments, 1):
        position_error = row.get('position_error_m') if isinstance(row, dict) else None
        step_orientation = row.get('orientation_wxyz') if isinstance(row, dict) else None
        if (row.get('segment_index') != index
                or not isinstance(position_error, list) or len(position_error) != 3
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           and math.isfinite(x) for x in position_error)
                or math.dist(position_error, [0., 0., 0.]) > .008
                or not isinstance(step_orientation, list) or len(step_orientation) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           and math.isfinite(x) for x in step_orientation)):
            return None
    if math.dist(segments[-1].get('orientation_wxyz', []), orientation) > 1e-5:
        return None
    return {
        'parameter': 'top_down_after_upward_pregrasp',
        'source_attempt': 11,
        'measured_closing_axis_world': axes[0],
        'measured_approach_axis_world': axes[1],
        'descent_target_m': target,
        'descent_actual_m': actual,
        'descent_error_m': error,
        'pregrasp_orientation_segments': len(segments),
        'position_bias_reused': False,
        'rationale': '高位预抓取姿态各分段位置稳定且网格无相交；但已记录的手指长轴朝上，沿原侧夹姿态下降时末端漂移超过50 mm。本次改为经过验证的指端朝下姿态，并在预抓取点重新逐段核验后才允许下降。',
    }

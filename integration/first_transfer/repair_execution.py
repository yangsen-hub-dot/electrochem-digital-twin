"""Small geometry helpers for executing a registered local repair, metres."""
import math

FULL_OPEN_JOINT_TARGET_M = 0.04


def latest_approach_axis(sample, *, frame_index, run_id):
    """Use the same-frame in-memory mesh sample, independent of JSON row size."""
    if (not isinstance(sample, dict) or sample.get('frame_index') != frame_index
            or sample.get('run_id') != run_id):
        raise ValueError('missing or stale same-frame mesh sample')
    axis = sample.get('gripper_approach_axis_world')
    if (not isinstance(axis,list) or len(axis)!=3
            or not all(isinstance(v,(int,float)) and not isinstance(v,bool)
                       and math.isfinite(v) for v in axis)):
        raise ValueError('missing/invalid mesh-derived axis')
    return list(map(float,axis))


def resumed_gripper_open_target(resuming, holding_component):
    """Reapply the safe free-gripper target when a simulator scene is rebuilt."""
    if resuming and not holding_component:
        return FULL_OPEN_JOINT_TARGET_M
    return None


def ee_route_waypoints(spec, ee_position, grasp_position):
    points = spec.get("waypoints_m", [])
    if spec.get("frame") == "grasp_world":
        offset = [grasp_position[i] - ee_position[i] for i in range(3)]
        return [[float(point[i]) - offset[i] for i in range(3)] for point in points]
    return [[float(value) for value in point] for point in points]


def expand_route(start, points, max_step_m=.035):
    result = []
    current = [float(value) for value in start]
    for point in points:
        segments = max(1, math.ceil(math.dist(current, point) / max_step_m))
        result.extend(tuple(current[k] + (point[k] - current[k]) * i / segments
                            for k in range(3)) for i in range(1, segments + 1))
        current = list(point)
    return result


def pose_axis_evidence(spec, closing_axis, approach_axis):
    expected_closing = spec.get("expected_closing_axis_world")
    expected_approach = spec.get("expected_approach_axis_world")
    closing_dot = (abs(sum(closing_axis[i] * expected_closing[i] for i in range(3)))
                   if expected_closing else 1.0)
    approach_dot = (sum(approach_axis[i] * expected_approach[i] for i in range(3))
                    if expected_approach else 1.0)
    return {"passed": closing_dot >= .95 and approach_dot >= .85,
            "closing_axis_world": list(closing_axis),
            "gripper_approach_axis_world": list(approach_axis),
            "closing_alignment": closing_dot, "approach_alignment": approach_dot}


def open_gap_verified(spec, gap_m):
    joint_target = spec.get("gripper_open_joint_target_m")
    if joint_target is None:
        return gap_m >= .065
    expected = 2 * float(joint_target)
    return expected * .8 <= gap_m <= expected * 1.2

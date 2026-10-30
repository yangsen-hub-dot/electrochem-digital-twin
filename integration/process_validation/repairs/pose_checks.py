"""Independent checks of a registered grasp pose in captured low-action frames."""
import numpy as np
from ..geometry_evidence import unit_axis, GeometryEvidenceError


def check_registered_pose(sample, manifest, report, location):
    spec = (manifest.get('correction', {}).get('routes', {})
            .get(sample.get('step_id'), {}))
    if not spec.get('orientation_wxyz'):
        return None
    phase = sample.get('phase', '')
    if '/horizontal_pose_probe' in phase or '/repair_high_route' in phase:
        return None
    if not phase.startswith(('pick_lead/', 'connect_lead/',
                             'pick_salt_bridge/', 'connect_salt_bridge/')):
        return None
    closing = sample.get('closing_axis_world')
    approach = sample.get('gripper_approach_axis_world')
    if (not isinstance(closing, list) or len(closing) != 3
            or not isinstance(approach, list) or len(approach) != 3):
        report.issue('geometry', 'repair_pose_missing', 'uncovered',
                     '局部夹持动作缺少可用的实测闭合轴或指端方向', **location)
        return False
    try:
        closing = unit_axis(closing)
        approach = unit_axis(approach)
        expected_closing = unit_axis(spec.get('expected_closing_axis_world'))
        expected_approach = unit_axis(spec.get('expected_approach_axis_world'))
    except GeometryEvidenceError as exc:
        report.issue('geometry', 'repair_pose_invalid', 'fail', str(exc), **location)
        return False
    closing_alignment = abs(float(np.dot(closing, expected_closing)))
    approach_alignment = float(np.dot(approach, expected_approach))
    if closing_alignment < .95 or approach_alignment < .85:
        report.issue('geometry', 'repair_pose_deviation', 'fail',
                     '盐桥/引线夹持偏离已登记的指端方向和左右闭合姿态',
                     closing_axis_world=closing.tolist(), gripper_approach_axis_world=approach.tolist(),
                     closing_alignment=closing_alignment, approach_alignment=approach_alignment,
                     **location)
        return False
    return True

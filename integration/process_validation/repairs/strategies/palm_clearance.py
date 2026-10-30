"""Recognize the measured lead-pick palm/support conflict after horizontal grasp."""
import json
import math
from pathlib import Path

import numpy as np

from ...geometry import transform, aabb_gap, triangles_in_box
from ...geometry_evidence import validated_scene, numeric_array, affine_matrix, GeometryEvidenceError


def supports_opposed_lead_approach(targets, manifest):
    if not targets:
        return False
    routes = manifest.get('correction', {}).get('routes', {})
    for target in targets:
        if target.family != 'measurement_lead' or not target.step_id.startswith('ui_pick_'):
            return False
        spec = routes.get(target.step_id, {})
        if (spec.get('expected_approach_axis_world') != [0., -1., 0.]
                or spec.get('expected_closing_axis_world') != [1., 0., 0.]):
            return False
        if not all(issue.get('code') == 'fixture_intersection'
                   and '/panda_hand/' in issue.get('body', '')
                   and '/DualCellElectrodeSupport/' in issue.get('obstacle', '')
                   for issue in target.issues):
            return False
    return True


def predict_opposed_hand_clearance(run_dir, manifest, rotation_degrees=180.):
    """Rotate measured rigid hand meshes about the same grasp point.

    This screens the candidate against captured supports; it does not establish
    IK reachability or continuous collision freedom.
    """
    root = Path(run_dir) / 'process_validation'
    try:
        geometry = json.loads((root / 'geometry.json').read_text(encoding='utf8'))
        policy = json.loads((Path(__file__).parents[2] / 'policy.json').read_text(encoding='utf8'))
        samples = [json.loads(line) for line in (root / 'trace.jsonl').read_text(encoding='utf8').splitlines() if line.strip()]
    except (OSError, ValueError):
        return {'passed': False, 'reason': '缺少测量网格或轨迹，不能预检手掌反向候选'}
    try:
        return _predict_validated(run_dir, manifest, geometry, samples, policy, rotation_degrees)
    except (GeometryEvidenceError, KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        return {'passed': False, 'reason': f'测量证据不完整或无效：{exc}',
                'coverage': 'invalid_or_incomplete_evidence'}


def _predict_validated(run_dir, manifest, geometry, samples, policy, rotation_degrees):
    run_id = Path(run_dir).name
    if not isinstance(manifest, dict) or manifest.get('run_id') != run_id:
        raise GeometryEvidenceError('录像清单与预检来源编号不一致')
    bodies, obstacles = validated_scene(geometry, run_id)
    if (geometry.get('pose_source') != 'physx_articulation_body_poses'
            or geometry.get('bound_body_count') != len(bodies) or geometry.get('unsupported_meshes')
            or (Path(run_dir) / 'process_validation/trace_error.json').exists()):
        raise GeometryEvidenceError('手部网格未完整绑定实际位姿或采集存在缺口')
    frames = manifest.get('frames')
    if not isinstance(frames, list) or not frames or len(samples) != len(frames):
        raise GeometryEvidenceError('轨迹与帧清单为空或长度不一致')
    angle = math.radians(float(numeric_array([rotation_degrees], (1,))[0]))
    rotation = np.asarray([[math.cos(angle), -math.sin(angle), 0.],
                           [math.sin(angle), math.cos(angle), 0.], [0., 0., 1.]])
    checked = collisions = 0
    for index, sample in enumerate(samples):
        frame = frames[index]
        if (not isinstance(sample, dict) or not isinstance(frame, dict)
                or type(sample.get('frame_index')) is not int or sample['frame_index'] != index
                or sample.get('run_id') != run_id or sample.get('step_id') != frame.get('step_id')
                or sample.get('phase') != frame.get('phase') or sample.get('moved_obstacles')):
            raise GeometryEvidenceError('逐帧编号、来源、阶段或静态障碍不一致')
        times = numeric_array([sample.get('simulation_time_s'), frame.get('simulation_time_s')], (2,))
        if not math.isclose(*times, abs_tol=1e-9, rel_tol=1e-10):
            raise GeometryEvidenceError('轨迹与画面时间不一致')
        phase = sample.get('phase', '')
        if (sample.get('action') not in {'pick_lead', 'connect_lead'}
                or '/horizontal_pose_probe' in phase or '/repair_high_route' in phase):
            continue
        if not isinstance(phase, str) or not phase.startswith(('pick_lead/', 'connect_lead/')):
            raise GeometryEvidenceError('动作与阶段不匹配')
        pivot = numeric_array(frame.get('robot_grasp_frame_m'), (3,))
        checked += 1
        for path, body in bodies.items():
            matrix = affine_matrix(sample.get('body_transforms', {}).get(path))
            actual = transform(np.asarray(body['points']), matrix)
            if (not np.isfinite(actual).all()
                    or np.max(np.ptp(actual, axis=0)) > policy['max_hand_mesh_extent_m']):
                raise GeometryEvidenceError('实测手部网格尺度或世界坐标无效')
            candidate = (actual - pivot) @ rotation.T + pivot
            for obstacle in obstacles:
                if (aabb_gap(candidate, obstacle['lower'], obstacle['upper']) == 0
                        and triangles_in_box(candidate, body['triangles'], obstacle['inverse_matrix'],
                                             obstacle['size'], policy['geometry_numeric_tolerance_m'])):
                    collisions += 1
    if not checked:
        raise GeometryEvidenceError('没有可用于该候选的低位引线动作样本')
    return {'passed': collisions == 0, 'sample_frames': checked,
            'predicted_intersections': collisions,
            'source_run_id': Path(run_dir).name, 'rotation_degrees': rotation_degrees,
            'basis': 'recorded_rigid_hand_mesh_rotated_about_same_grasp_point',
            'coverage': 'captured_supports_only_not_ik_or_continuous_collision'}

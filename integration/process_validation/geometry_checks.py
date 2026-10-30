"""Audit captured geometry and same-frame confirmed data, without replaying actions."""
import json
import math
import numpy as np

from .geometry import (transform, triangles_in_box, aabb_gap, swept_aabb_possible,
                       derive_gripper_approach_axis_world)
from .repairs.pose_checks import check_registered_pose
from .cable_checks import check_cables
from .geometry_evidence import validated_scene, unit_axis, affine_matrix, GeometryEvidenceError


def should_check_electrode_grasp_axes(action, phase):
    """Check grasp orientation only at pick contact or settled placement insertion."""
    if action == "pick_electrode":
        return True
    return action == "place_electrode" and phase == "place_electrode/align_and_insert"


def check_trace(root, archive, manifest, policy, report):
    geometry_path, trace_path = root/"geometry.json", root/"trace.jsonl"
    if not geometry_path.is_file() or not trace_path.is_file():
        report.check("geometry", "trace", "uncovered", "该录像未保存实际几何轨迹，无法自动判断夹爪碰架/夹持方向")
        report.check("sync", "same_frame", "uncovered", "该旧录像缺逐帧确认状态，不能追认画面与账本完全同步")
        return
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    try:
        bodies, obstacles = validated_scene(geometry, report.data['run_id'])
    except GeometryEvidenceError as exc:
        report.issue('geometry', exc.code, exc.status, str(exc))
        return
    if not bodies or not obstacles:
        report.check("geometry", "coverage", "uncovered", "未取得手掌/夹指网格或支架几何，几何检查不能通过", body_count=len(bodies), obstacle_count=len(obstacles))
    else:
        report.check("geometry", "coverage", "pass", f"取得 {len(bodies)} 个实际手掌/夹指网格与 {len(obstacles)} 个支架实体块")
    if geometry.get("pose_source") != "physx_articulation_body_poses" or geometry.get("bound_body_count") != len(bodies):
        report.check("geometry", "actual_pose_source", "uncovered", "部分手部网格未绑定实际物理连杆位姿，不能视为真实轨迹验证")
    frames = manifest.get("frames", [])
    check_cables(root, manifest, report)
    revisions = {row["result"]["state_revision"]:row["result"] for row in archive.get("accepted_receipts", {}).values() if row["receipt"].get("status") == "succeeded"}
    previous, count, sync_errors, actual_collisions, valid_geometry_samples = {}, 0, 0, 0, 0
    last_index = -1
    grasp_axis_samples, invalid_grasp_axes = 0, 0
    approach_axis_samples, invalid_approach_axes = 0, 0
    repair_pose_samples, invalid_repair_poses = 0, 0
    complete_geometry = (not geometry.get('unsupported_meshes') and not (root/'trace_error.json').exists()
                         and geometry.get('pose_source') == 'physx_articulation_body_poses'
                         and geometry.get('bound_body_count') == len(bodies))
    with trace_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            sample = json.loads(line)
            index = sample["frame_index"]
            when = sample.get('simulation_time_s')
            valid_time = isinstance(when, (int, float)) and not isinstance(when, bool) and math.isfinite(when)
            location = {"frame_index": index, "video_time_s": index/manifest["fps"] if type(index) is int else None, "step_id":sample.get("step_id"), "simulation_time_s":when if valid_time else None}
            count += 1
            if type(index) is not int or sample.get("run_id") != report.data["run_id"] or index != last_index+1 or index >= len(frames):
                report.issue("sync", "trace_index", "fail", "逐帧轨迹编号缺失、重复或串运行", **location)
                complete_geometry = False
                sync_errors += 1
                if type(index) is int:
                    last_index = index
                continue
            last_index = index
            frame = frames[index]
            frame_time = frame.get('simulation_time_s')
            valid_frame_time = isinstance(frame_time, (int, float)) and not isinstance(frame_time, bool) and math.isfinite(frame_time)
            if (sample["phase"] != frame["phase"] or sample.get("step_id") != frame.get("step_id")
                    or not valid_time or not valid_frame_time
                    or not math.isclose(when, frame_time, abs_tol=1e-9)):
                report.issue("sync", "frame_binding", "fail", "画面、动作阶段和仿真时间未同帧绑定", **location)
                sync_errors += 1
                complete_geometry = False
            revision = sample.get("state_revision")
            expected = revisions.get(revision)
            if revision == 0:
                mismatch = sample.get("completed_steps") != 0 or sample.get("voltage_valid") is True
            elif expected is None:
                mismatch = True
            else:
                mismatch = sample.get("completed_steps") != expected.get("completed_steps")
                for side in ("left", "right"):
                    liquid = expected["state"]["containers"][side+"_half_cell"]
                    shown = sample["cells"][side]
                    for key in ("volume_ul", "mixing_uniformity"):
                        mismatch |= not math.isclose(float(shown[key]), float(liquid[key]), abs_tol=1e-8, rel_tol=1e-10)
                    mismatch |= shown.get("concentrations_m") != liquid.get("concentrations_m")
                voltage = expected.get("voltage", {})
                expected_mv = voltage.get("virtual_meter_mv") if voltage.get("valid") is True else None
                mismatch |= sample.get("virtual_meter_mv") != expected_mv
            if mismatch:
                report.issue("sync", "confirmed_ledger", "fail", "画面绑定的数据与对应成功回执不一致或提前显示未来结果", **location)
                sync_errors += 1
            if sample.get("moved_obstacles"):
                complete_geometry = False
                report.issue("geometry", "obstacle_changed", "uncovered", "静态支架在运行中发生变换，当前几何规则不能继续覆盖", **location)
                continue
            axis = sample.get("closing_axis_world")
            repair_pose_result = check_registered_pose(sample, manifest, report, location)
            if repair_pose_result is not None:
                repair_pose_samples += 1
                invalid_repair_poses += int(not repair_pose_result)
            if (should_check_electrode_grasp_axes(sample.get("action"), sample.get("phase"))
                    and frame.get("gripper_gap_m", math.inf) <= .013):
                approach_axis = (sample.get("gripper_approach_axis_world")
                                 or derive_gripper_approach_axis_world(geometry, sample))
                def checked_axis(value):
                    try:
                        return unit_axis(value).tolist()
                    except GeometryEvidenceError:
                        return None
                valid_approach = checked_axis(approach_axis) if approach_axis is not None else None
                valid_closing = checked_axis(axis) if axis is not None else None
                if approach_axis is None:
                    invalid_approach_axes += 1
                    report.issue("geometry", "approach_axis_missing", "uncovered",
                                 "缺少夹爪指端相对掌部的实际朝向，无法确认是否从上方接近", **location)
                elif valid_approach is None:
                    invalid_approach_axes += 1
                    report.issue("geometry", "approach_axis_invalid", "fail",
                                 "夹爪指端接近方向记录无效", **location)
                elif float(valid_approach[2]) > -policy["minimum_downward_approach_component"]:
                    invalid_approach_axes += 1
                    report.issue("geometry", "upward_grasp_axis", "fail",
                                 "夹爪指端没有朝下，电极不是从上方接近",
                                 gripper_approach_axis_world=approach_axis, **location)
                else:
                    approach_axis_samples += 1
                if axis is None:
                    invalid_grasp_axes += 1
                    report.issue("geometry", "grasp_axis_missing", "uncovered", "缺少实际双夹指闭合方向", **location)
                elif valid_closing is None:
                    report.issue('geometry', 'grasp_axis_invalid', 'fail', '夹爪闭合方向记录无效', **location)
                    invalid_grasp_axes += 1
                elif abs(valid_closing[2]) > policy["max_vertical_closing_axis_component"]:
                    invalid_grasp_axes += 1
                    report.issue("geometry", "vertical_grasp_axis", "fail", "电极夹爪闭合方向偏离侧向夹持要求，呈上下夹持", closing_axis_world=axis, **location)
                else:
                    grasp_axis_samples += 1
            for path, body in bodies.items():
                matrix = sample.get("body_transforms", {}).get(path)
                if matrix is None:
                    complete_geometry = False
                    report.issue("geometry", "missing_body", "uncovered", "实际手部位姿缺失", body=path, **location)
                    continue
                try:
                    matrix = affine_matrix(matrix)
                except GeometryEvidenceError as exc:
                    complete_geometry = False
                    report.issue('geometry', 'invalid_transform', 'fail', str(exc), body=path, **location)
                    continue
                vertices = transform(body["points"], matrix)
                if not np.isfinite(vertices).all():
                    complete_geometry = False
                    report.issue("geometry", "nonfinite", "fail", "实际手部几何包含非法数值", body=path, **location)
                    continue
                if float(np.max(np.ptp(vertices,axis=0))) > policy["max_hand_mesh_extent_m"]:
                    complete_geometry = False
                    report.issue("geometry", "mesh_units", "uncovered", "手部网格尺度异常，可能遗漏资产单位或父变换；不据此判定碰撞", body=path, **location)
                    continue
                valid_geometry_samples += 1
                for obstacle in obstacles:
                    gap = aabb_gap(vertices, obstacle["lower"], obstacle["upper"])
                    hit = gap == 0 and triangles_in_box(vertices, body["triangles"], obstacle["inverse_matrix"], obstacle["size"], policy["geometry_numeric_tolerance_m"])
                    if hit:
                        actual_collisions += 1
                        report.issue("geometry", "fixture_intersection", "fail", "实际手掌/夹指网格与支架实体相交", body=path, obstacle=obstacle["path"], method="mesh_triangle_vs_exact_fixture_cube", **location)
                    elif gap <= policy["near_fixture_review_m"]:
                        report.issue("geometry", "near_fixture", "review", "手部与支架包围区域接近，需复核细节间隙", body=path, obstacle=obstacle["path"], aabb_gap_m=gap, **location)
                    old = previous.get(path)
                    # Conservative endpoint union only flags a possible missed
                    # transit; it is not a continuous collision pass/fail test.
                    if old is not None and not hit and np.max(np.linalg.norm(vertices-old,axis=1)) > policy["near_fixture_review_m"] and swept_aabb_possible(old, vertices, obstacle["lower"], obstacle["upper"]):
                        report.issue("geometry", "between_frames", "review", "帧间运动包络经过支架，离散采样不足以排除中间碰撞", body=path, obstacle=obstacle["path"], **location)
                previous[path] = vertices
    if count != len(frames):
        complete_geometry = False
        report.issue("sync", "trace_length", "fail", "几何/状态轨迹与编码帧清单长度不一致", trace_frames=count, manifest_frames=len(frames))
    else:
        report.check("sync", "trace_length", "pass", f"{count} 帧轨迹与录像帧清单一一对应")
    if count and not sync_errors:
        report.check("sync", "confirmed_ledger", "pass", "逐帧确认状态、动作阶段、时间与成功回执一致")
    if valid_geometry_samples and obstacles and not actual_collisions and complete_geometry:
        report.check("geometry", "sampled_intersections", "pass", "已采样位姿未发现手部网格与已纳入支架实体相交；帧间覆盖另报")
    elif bodies and obstacles and not actual_collisions:
        report.check("geometry", "sampled_intersections", "uncovered", "没有尺度与位姿有效的手部样本，未作碰撞通过判定")
    if (grasp_axis_samples and not invalid_grasp_axes
            and approach_axis_samples and not invalid_approach_axes):
        report.check('geometry', 'grasp_orientation', 'pass',
                     f'已核对 {approach_axis_samples} 帧指端朝下接近及 {grasp_axis_samples} 帧水平闭合方向')
    if repair_pose_samples and not invalid_repair_poses:
        report.check('geometry', 'registered_repair_pose', 'pass',
                     f'已独立核对 {repair_pose_samples} 个盐桥/引线动作帧的登记水平夹持姿态')
    if geometry.get("unsupported_meshes"):
        report.check("geometry", "unsupported_meshes", "uncovered", "部分手部网格拓扑未支持", paths=geometry["unsupported_meshes"])
    if (root/"trace_error.json").exists():
        report.check("geometry", "trace_error", "uncovered", "采集过程中发生错误，不能判定全过程通过")

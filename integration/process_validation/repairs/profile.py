"""Validation for bounded local-repair profiles accepted by the runner."""
import math
from .strategies.horizontal_grasp import (TOP_DOWN_HORIZONTAL_WXYZ,
                                         SIDE_FACING_HORIZONTAL_WXYZ, OPPOSED_HORIZONTAL_WXYZ,
                                         DIAGONAL_HORIZONTAL_WXYZ, DIAGONAL_CLOSING_AXIS,
                                         DIAGONAL_APPROACH_AXIS)


ELECTRODE_STEPS = {
    "ui_pick_left_copper_electrode", "ui_place_left_copper_electrode",
    "ui_pick_right_copper_electrode", "ui_place_right_copper_electrode",
}
ROUTE_FRAMES = {
    "ui_pick_salt_bridge": "ee_world",
    "ui_connect_salt_bridge": "ee_world",
    "ui_pick_left_lead": "grasp_world",
    "ui_pick_right_lead": "grasp_world",
    "ui_connect_left_lead": "grasp_world",
    "ui_connect_right_lead": "grasp_world",
}


def _finite_vector(value, length):
    return (isinstance(value, list) and len(value) == length
            and all(isinstance(item, (int, float)) and math.isfinite(float(item))
                    for item in value))


def _validate_electrode_actions(actions):
    for step_id, spec in actions.items():
        if step_id not in ELECTRODE_STEPS or not isinstance(spec, dict):
            raise ValueError(f"修正方案包含不支持的动作: {step_id}")
        quat = spec.get("orientation_wxyz")
        transit = spec.get("transit_height_m")
        if (not _finite_vector(quat, 4)
                or not math.isclose(sum(float(v) ** 2 for v in quat), 1.0, abs_tol=1e-5)):
            raise ValueError(f"{step_id} 的四元数无效")
        if (not isinstance(transit, (int, float)) or not math.isfinite(float(transit))
                or not .35 <= float(transit) <= .55):
            raise ValueError(f"{step_id} 的高位通道高度超出首版允许范围")
        candidates = spec.get("orientation_candidates_wxyz", [quat])
        if (not isinstance(candidates, list) or not 1 <= len(candidates) <= 3
                or any(not _finite_vector(item, 4)
                       or not math.isclose(sum(float(v) ** 2 for v in item), 1.0, abs_tol=1e-5)
                       for item in candidates)):
            raise ValueError(f"{step_id} 的有限姿态候选无效")
        for key in ("staging_offset_xy_m", "approach_offset_xy_m"):
            offset = spec.get(key)
            if offset is not None and (not _finite_vector(offset, 2)
                    or math.hypot(*map(float, offset)) > .15):
                raise ValueError(f"{step_id} 的受限接近偏移无效")
        open_joint_target = spec.get("gripper_open_joint_target_m")
        if open_joint_target is not None and (not step_id.startswith(("ui_pick_", "ui_place_"))
                or not isinstance(open_joint_target, (int, float))
                or not math.isfinite(float(open_joint_target))
                or not .006 <= float(open_joint_target) <= .02):
            raise ValueError(f"{step_id} 的夹爪开口目标超出已知安全范围")
        restore_full_open = spec.get("restore_full_open_before_pick")
        if (restore_full_open is not None
                and (not step_id.startswith("ui_pick_") or restore_full_open is not True
                     or open_joint_target is not None)):
            raise ValueError(f"{step_id} 的抓取前全开复位标记无效")
        pose_bias = spec.get("side_pose_observed_bias_xyz_m")
        if pose_bias is not None and (not _finite_vector(pose_bias, 3)
                or any(abs(float(value)) > .05 for value in pose_bias)
                or not .015 <= math.sqrt(sum(float(value) ** 2 for value in pose_bias)) <= .06):
            raise ValueError(f"{step_id} 的侧向姿态控制残差超出有界补偿范围")
        probe_bias = spec.get('orientation_probe_bias_xyz_m')
        if probe_bias is not None and (not step_id.startswith('ui_pick_')
                or not _finite_vector(probe_bias, 3) or pose_bias is None
                or max(abs(float(a)) for a in probe_bias) > .05
                or not .015 <= math.sqrt(sum(float(a)**2 for a in probe_bias)) <= .06
                or math.dist(probe_bias, pose_bias) > .018):
            raise ValueError(f'{step_id} 的姿态预检补偿未绑定已测得的残差')
        probe_tolerance = spec.get("high_probe_physical_tolerance_m")
        if probe_tolerance is not None and (pose_bias is None
                or not isinstance(probe_tolerance, (int, float))
                or not math.isfinite(float(probe_tolerance))
                or not .008 <= float(probe_tolerance) <= .015):
            raise ValueError(f"{step_id} 的高位预检位置公差无效")
        controller_margin = spec.get("high_probe_controller_margin_m")
        if controller_margin is not None and (pose_bias is None
                or not isinstance(controller_margin, (int, float))
                or not math.isfinite(float(controller_margin))
                or not .012 <= float(controller_margin) <= .025):
            raise ValueError(f"{step_id} 的高位预检控制余量无效")
        transit_orientation = spec.get("high_transit_orientation_wxyz")
        if (transit_orientation is not None
                and (not _finite_vector(transit_orientation, 4)
                     or not math.isclose(sum(float(value) ** 2 for value in transit_orientation),
                                         1.0, abs_tol=1e-5))):
            raise ValueError(f"{step_id} 的高位行进姿态无效")
        probe_at_pregrasp = spec.get("orientation_probe_at_pregrasp", False)
        if not isinstance(probe_at_pregrasp, bool):
            raise ValueError(f"{step_id} 的预抓取姿态预检开关无效")
        if probe_at_pregrasp and (not step_id.startswith("ui_pick_")
                                  or transit_orientation is None):
            raise ValueError(f"{step_id} 的预抓取姿态预检缺少受支持的抓取步骤或行进姿态")
        top_down = spec.get("top_down_vertical_descent", False)
        if not isinstance(top_down, bool):
            raise ValueError(f"{step_id} 的垂直抓取开关无效")
        approach_z = spec.get("top_down_approach_z_m")
        if approach_z is not None and (not isinstance(approach_z, (int, float))
                                       or not math.isfinite(float(approach_z))
                                       or not .24 <= float(approach_z) <= .42):
            raise ValueError(f"{step_id} 的垂直接近高度超出允许范围")
        grasp_offset = spec.get("top_down_grasp_target_offset_z_m")
        if (grasp_offset is not None
                and (not isinstance(grasp_offset, (int, float))
                     or not math.isfinite(float(grasp_offset))
                     or not 0. <= float(grasp_offset) <= .01)):
            raise ValueError(f"{step_id} 的垂直抓取参考高度偏移无效")
        if grasp_offset not in (None, 0, 0.) and not top_down:
            raise ValueError(f"{step_id} 的抓取参考高度偏移必须绑定朝下垂直抓取")
        if top_down:
            if (not step_id.startswith("ui_pick_")
                    or any(abs(float(a) - float(b)) > 1e-8
                           for a, b in zip(quat, TOP_DOWN_HORIZONTAL_WXYZ))
                    or transit_orientation != quat
                    or approach_z is None
                    or spec.get("side_pose_observed_bias_xyz_m") is not None
                    or spec.get("orientation_probe_bias_xyz_m") is not None
                    or spec.get("high_transit_destination_pose_bias", False) is not False
                    or spec.get("high_destination_position_feedback", False) is not False
                    or (spec.get("approach_offset_xy_m") is not None
                        and any(abs(float(value)) > 1e-8
                                for value in spec["approach_offset_xy_m"]))):
                raise ValueError(f"{step_id} 的朝下垂直抓取必须保留登记姿态、原位下降且不复用位置补偿")
        destination_bias = spec.get("high_transit_destination_pose_bias", False)
        if not isinstance(destination_bias, bool):
            raise ValueError(f"{step_id} 的高位目标姿态补偿开关无效")
        feedback_enabled = spec.get("high_destination_position_feedback", False)
        if not isinstance(feedback_enabled, bool) or (feedback_enabled and transit_orientation is None):
            raise ValueError(f"{step_id} 的高位目标位置反馈配置无效")
        transit_step = spec.get("high_transit_waypoint_step_m")
        if (transit_step is not None
                and (not isinstance(transit_step, (int, float))
                     or not math.isfinite(float(transit_step))
                     or not .02 <= float(transit_step) <= .06
                     or transit_orientation is None)):
            raise ValueError(f"{step_id} 的高位转场分段距离无效")


def validate_repair_profile(profile, source_run_id):
    """Reject any profile that can change an action target or exceed its route envelope."""
    if not isinstance(profile, dict) or profile.get("source_run_id") != source_run_id:
        raise ValueError("修正方案与来源运行编号不一致")
    actions = profile.get("actions", {})
    routes = profile.get("routes", {})
    if not isinstance(actions, dict) or not isinstance(routes, dict):
        raise ValueError("修正方案的动作或路线结构无效")
    if not actions and not routes:
        raise ValueError("修正方案没有允许的局部动作或路线参数")
    if actions:
        _validate_electrode_actions(actions)
    for step_id, spec in routes.items():
        if step_id not in ROUTE_FRAMES or not isinstance(spec, dict):
            raise ValueError(f"修正方案包含不支持的路线动作: {step_id}")
        target_frame = spec.get("target_frame", ROUTE_FRAMES[step_id])
        if (target_frame != ROUTE_FRAMES[step_id]
                or spec.get("frame") not in {ROUTE_FRAMES[step_id], "ee_world"}):
            raise ValueError(f"{step_id} 的路线坐标系不匹配")
        candidate_id = spec.get("candidate_id")
        points = spec.get("waypoints_m")
        approach_z = spec.get("approach_z_m")
        if (not isinstance(candidate_id, str) or not candidate_id
                or not isinstance(points, list) or not 2 <= len(points) <= 6
                or any(not _finite_vector(point, 3) for point in points)):
            raise ValueError(f"{step_id} 的路线候选结构无效")
        for x, y, z in points:
            if not (.15 <= float(x) <= .85 and -.45 <= float(y) <= .30
                    and .42 <= float(z) <= .55):
                raise ValueError(f"{step_id} 的高位路径点超出已核验工作空间")
        if (not isinstance(approach_z, (int, float)) or not math.isfinite(float(approach_z))
                or not .24 <= float(approach_z) <= .42):
            raise ValueError(f"{step_id} 的接近高度超出允许范围")
        if spec.get("preserves_target") is not True:
            raise ValueError(f"{step_id} 的修复候选必须保留原动作终点")
        orientation = spec.get("orientation_wxyz")
        direct = spec.get("direct_overhead", False)
        if (not isinstance(direct, bool) or (direct and
                ("lead" not in step_id or spec.get("frame") != "ee_world"
                 or orientation != TOP_DOWN_HORIZONTAL_WXYZ
                 or any(abs(float(p[axis]) - float(points[0][axis])) > 1e-8
                        for p in points for axis in (0, 1))))):
            raise ValueError(f"{step_id} 的直接上方路径必须绑定引线朝下姿态与同一目标轴线")
        if orientation is not None:
            expected_approach = (DIAGONAL_APPROACH_AXIS if orientation == DIAGONAL_HORIZONTAL_WXYZ else
                                 [0., 1., 0.] if orientation == OPPOSED_HORIZONTAL_WXYZ else
                                 [0., -1., 0.] if orientation == SIDE_FACING_HORIZONTAL_WXYZ
                                 else [0., 0., -1.])
            expected_closing = DIAGONAL_CLOSING_AXIS if orientation == DIAGONAL_HORIZONTAL_WXYZ else [1., 0., 0.]
            registered = any(_finite_vector(orientation, 4)
                             and all(abs(float(a) - b) <= 1e-8 for a, b in zip(orientation, pose))
                             for pose in (TOP_DOWN_HORIZONTAL_WXYZ, SIDE_FACING_HORIZONTAL_WXYZ,
                                          OPPOSED_HORIZONTAL_WXYZ, DIAGONAL_HORIZONTAL_WXYZ))
            if (not _finite_vector(orientation, 4)
                    or not registered
                    or spec.get("expected_closing_axis_world") != expected_closing
                    or spec.get("expected_approach_axis_world") != expected_approach
                    or (orientation in (SIDE_FACING_HORIZONTAL_WXYZ, OPPOSED_HORIZONTAL_WXYZ,
                                        DIAGONAL_HORIZONTAL_WXYZ)
                        and "lead" not in step_id)):
                raise ValueError(f"{step_id} 必须使用已登记的左右闭合姿态")
        open_target = spec.get("gripper_open_joint_target_m")
        if open_target is not None and (not isinstance(open_target, (int, float))
                or not math.isfinite(float(open_target)) or not .006 <= open_target <= .02):
            raise ValueError(f"{step_id} 的局部开口目标无效")
        offset = spec.get("grasp_reference_offset_z_m", 0.)
        if (not isinstance(offset, (int, float)) or not math.isfinite(float(offset))
                or not 0. <= offset <= .01
                or (offset and "salt_bridge" not in step_id)):
            raise ValueError(f"{step_id} 的夹持参考偏移无效")
        step = spec.get("waypoint_step_m", .035)
        if not isinstance(step, (int, float)) or not .02 <= step <= .06:
            raise ValueError(f"{step_id} 的局部路径分段无效")

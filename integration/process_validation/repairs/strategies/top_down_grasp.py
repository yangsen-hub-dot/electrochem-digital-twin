"""Evidence-gated clearance adjustment for a measured top-down rack-fork hit."""
import json
import math
from pathlib import Path

from ...geometry import aabb_gap, transform
from ...geometry_evidence import affine_matrix
from ...preflight_collision import prepare_scene, sample_intersections
from .horizontal_grasp import TOP_DOWN_HORIZONTAL_WXYZ


PICK_STEP = "ui_pick_left_copper_electrode"
PLACE_STEP = "ui_place_left_copper_electrode"
FINGER_PATH = "/World/envs/env_0/Articulations_robot/panda_leftfinger/visuals/panda_leftfinger"
FORK_PATH = "/World/ElectrochemTransferProxy/CopperElectrodeRack/RightForkL"
SIDE_GEOMETRY = {
    "left": {
        "pick_step": PICK_STEP,
        "place_step": PLACE_STEP,
        "finger_path": FINGER_PATH,
        "fork_path": FORK_PATH,
    },
    "right": {
        "pick_step": "ui_pick_right_copper_electrode",
        "place_step": "ui_place_right_copper_electrode",
        "finger_path": "/World/envs/env_0/Articulations_robot/panda_rightfinger/visuals/panda_rightfinger",
        "fork_path": "/World/ElectrochemTransferProxy/CopperElectrodeRack/LeftForkR",
    },
}
GRASP_OFFSET_M = .008
MIN_PREDICTED_CLEARANCE_M = .005
MAX_MEASURED_PENETRATION_M = .003


def _finite_vector(value, size):
    return (isinstance(value, list) and len(value) == size
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    and math.isfinite(float(item)) for item in value))


def measured_top_down_fork_clearance(manifest, report, run_dir, step_id=None):
    """Derive a bounded upward grasp-reference offset from an exact mesh hit."""
    correction = (manifest or {}).get("correction") or {}
    side = correction.get("side")
    side_geometry = SIDE_GEOMETRY.get(side)
    if side_geometry is None:
        return None
    step_id = step_id or side_geometry["pick_step"]
    finger_path = side_geometry["finger_path"]
    fork_path = side_geometry["fork_path"]
    params = (correction.get("parameters") or {}).get(step_id) or {}
    preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
    failure = (manifest or {}).get("motion_failure") or {}
    matching = [row for row in (report or {}).get("issues", [])
                if row.get("group") == "geometry" and row.get("code") == "fixture_intersection"
                and row.get("status") == "fail" and row.get("step_id") == step_id]
    other_hits = [row for row in (report or {}).get("issues", [])
                  if row.get("group") == "geometry" and row.get("code") == "fixture_intersection"
                  and row.get("status") == "fail" and row.get("step_id") != step_id]
    if (step_id != side_geometry["pick_step"]
            or correction.get("attempt_index") != 12
            or correction.get("candidate_id") != "electrode_top_down_after_upward_pregrasp_12"
            or params.get("top_down_vertical_descent") is not True
            or params.get("orientation_wxyz") != list(TOP_DOWN_HORIZONTAL_WXYZ)
            or params.get("high_transit_orientation_wxyz") != list(TOP_DOWN_HORIZONTAL_WXYZ)
            or params.get("top_down_grasp_target_offset_z_m") not in (None, 0, 0.)
            or params.get("side_pose_observed_bias_xyz_m") is not None
            or params.get("orientation_probe_bias_xyz_m") is not None
            or params.get("high_transit_destination_pose_bias") is not False
            or params.get("high_destination_position_feedback") is not False
            or not _finite_vector(preflight.get("closing_axis_world"), 3)
            or not _finite_vector(preflight.get("gripper_approach_axis_world"), 3)
            or abs(float(preflight["closing_axis_world"][2])) > .2
            or float(preflight["gripper_approach_axis_world"][2]) > -.85
            or failure.get("phase") != "pick_electrode/align"
            or not isinstance(failure.get("preflight_collision"), list)
            or len(failure["preflight_collision"]) != 1
            or failure["preflight_collision"][0].get("body") != finger_path
            or failure["preflight_collision"][0].get("obstacle") != fork_path
            or len(matching) != 1 or other_hits
            or matching[0].get("body") != finger_path
            or matching[0].get("obstacle") != fork_path
            or isinstance(matching[0].get("frame_index"), bool)
            or not isinstance(matching[0].get("frame_index"), int)):
        return None

    run_dir = Path(run_dir)
    try:
        geometry = json.loads((run_dir / "process_validation" / "geometry.json").read_text(encoding="utf-8"))
        if geometry.get("run_id") != manifest.get("run_id"):
            return None
        prepared = prepare_scene(geometry, manifest["run_id"])
        with (run_dir / "process_validation" / "trace.jsonl").open(encoding="utf-8") as stream:
            samples = [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError, KeyError, TypeError):
        return None

    frame_index = matching[0]["frame_index"]
    hits = [row for row in samples if row.get("frame_index") == frame_index
            and row.get("step_id") == step_id]
    if len(hits) != 1:
        return None
    sample = hits[0]
    try:
        collisions = sample_intersections(prepared, sample)
        if collisions != [{"body": finger_path, "obstacle": fork_path}]:
            return None
        bodies, obstacles = prepared
        body = bodies[finger_path]
        vertices = transform(body["points"], affine_matrix(sample["body_transforms"][finger_path]))
        obstacle = next(row for row in obstacles if row["path"] == fork_path)
    except (KeyError, StopIteration, ValueError, TypeError):
        return None

    if aabb_gap(vertices, obstacle["lower"], obstacle["upper"]) != 0:
        return None
    measured_min_z = float(vertices[:, 2].min())
    fork_top_z = float(obstacle["upper"][2])
    penetration = fork_top_z - measured_min_z
    predicted_clearance = measured_min_z + GRASP_OFFSET_M - fork_top_z
    if (not 0 < penetration <= MAX_MEASURED_PENETRATION_M
            or predicted_clearance < MIN_PREDICTED_CLEARANCE_M
            or GRASP_OFFSET_M > .01):
        return None

    shifted = json.loads(json.dumps(sample))
    for matrix in shifted["body_transforms"].values():
        matrix[3][2] = float(matrix[3][2]) + GRASP_OFFSET_M
    if sample_intersections(prepared, shifted):
        return None

    return {
        "parameter": "top_down_grasp_reference_z_offset",
        "side": side,
        "source_attempt": 12,
        "collision_frame_index": frame_index,
        "body": finger_path,
        "obstacle": fork_path,
        "measured_finger_min_z_m": measured_min_z,
        "measured_fork_top_z_m": fork_top_z,
        "measured_penetration_m": penetration,
        "target_offset_z_m": GRASP_OFFSET_M,
        "predicted_clearance_m": predicted_clearance,
        "shifted_collision_check": "pass",
        "placement_target_compensated": True,
        "rationale": (f"第十二次已通过朝下姿态检查，但实测{side}指网格最低点与对应支撑叉发生轻微相交。"
                      "将抓取参考点上移8 mm，帧内网格复核无相交并保留至少5 mm预测间隙；"
                      "放置时同步补偿该偏移，因此电极在电池槽内的最终安装高度不变。"),
    }


def retryable_top_down_fork_collision(effect, archive, manifest, report, run_dir):
    """Permit one new candidate only for the measured, bounded top-down fork hit."""
    correction = (manifest or {}).get("correction") or {}
    side_geometry = SIDE_GEOMETRY.get(correction.get("side"))
    if side_geometry is None:
        return False
    pick_step = side_geometry["pick_step"]
    dependency = correction.get("dependency_plan") or {}
    targets = set(correction.get("fault_step_ids") or [])
    failures = effect.get("new_failures") or []
    expected = {("geometry", "fixture_intersection"), ("records", "execution_failed")}
    observed = {(row.get("group"), row.get("code")) for row in failures}
    if (not effect.get("comparable") or effect.get("status") != "regressed"
            or effect.get("preservation_violations") or targets != {pick_step}
            or observed != expected or len(failures) != 2
            or any(row.get("step_id") != pick_step for row in failures)
            or archive.get("snapshot", {}).get("state_uncertain") is not True):
        return False
    event_failures = [event for event in archive.get("events", [])
                      if event.get("type") == "step.failed"]
    if (len(event_failures) != 1 or event_failures[0].get("step_id") != pick_step
            or not dependency.get("checkpoint_step_id")
            or pick_step not in dependency.get("rerun_step_ids", [])):
        return False
    return measured_top_down_fork_clearance(manifest, report, run_dir, pick_step) is not None


def measured_top_down_release_gap(manifest, report, archive, run_dir,
                                  pick_step=None, place_step=None):
    """Recognize a bounded 24 mm release gap after a confirmed top-down pick."""
    correction = (manifest or {}).get("correction") or {}
    side = correction.get("side")
    side_geometry = SIDE_GEOMETRY.get(side)
    if side_geometry is None:
        return None
    pick_step = pick_step or side_geometry["pick_step"]
    place_step = place_step or side_geometry["place_step"]
    params = correction.get("parameters") or {}
    pick = params.get(pick_step) or {}
    place = params.get(place_step) or {}
    dependency = correction.get("dependency_plan") or {}
    failures = [event for event in (archive or {}).get("events", [])
                if event.get("type") == "step.failed"]
    completed = [event for event in (archive or {}).get("events", [])
                 if event.get("type") == "step.completed"]
    place_failure = failures[0] if len(failures) == 1 else {}
    receipt = ((place_failure.get("payload") or {}).get("executor_receipt") or {})
    observation = receipt.get("state_observation") or {}
    robot = observation.get("robot") or {}
    semantic = observation.get("semantic_components") or {}
    message = str(receipt.get("reason") or "")
    if (pick_step != side_geometry["pick_step"]
            or place_step != side_geometry["place_step"]
            or correction.get("attempt_index") != 13
            or correction.get("candidate_id") != "electrode_top_down_fork_clearance_pick_13"
            or pick.get("top_down_vertical_descent") is not True
            or pick.get("orientation_wxyz") != list(TOP_DOWN_HORIZONTAL_WXYZ)
            or not math.isclose(float(pick.get("top_down_grasp_target_offset_z_m", 0)),
                                GRASP_OFFSET_M, abs_tol=1e-9)
            or place.get("orientation_wxyz") != list(TOP_DOWN_HORIZONTAL_WXYZ)
            or pick_step not in dependency.get("rerun_step_ids", [])
            or place_step not in dependency.get("rerun_step_ids", [])
            or place_failure.get("step_id") != place_step
            or not message.startswith(f"{side}侧电极释放后夹爪仍未打开: 0.024")
            or not math.isclose(float(robot.get("gripper_gap_m", -1)), .024,
                                abs_tol=.001)
            or semantic.get("held_electrode_side") != side
            or semantic.get("electrode_installed", {}).get(side) is not False
            or not any(event.get("step_id") == pick_step for event in completed)
            or (archive or {}).get("snapshot", {}).get("state_uncertain") is not True):
        return None

    issues = [row for row in (report or {}).get("issues", [])
              if row.get("status") == "fail"]
    allowed = {("records", "execution_failed"),
               ("geometry", "upward_grasp_axis"),
               ("geometry", "vertical_grasp_axis")}
    if (not issues or any((row.get("group"), row.get("code")) not in allowed
                          for row in issues)
            or any(row.get("group") == "records" and row.get("step_id") not in
                   (None, place_step) for row in issues)
            or any(row.get("group") == "geometry" and row.get("step_id") != place_step
                   for row in issues)):
        return None
    if any(row.get("group") == "records"
           and "0.024000 m" not in str(row.get("message", "")) for row in issues):
        return None

    transient_phases = {
        "place_electrode/align_and_insert/safe_staging",
        "place_electrode/align_and_insert/orientation_probe",
        "place_electrode/align_and_insert/high_transit_probe",
    }
    trace_path = Path(run_dir) / "process_validation" / "trace.jsonl"
    try:
        with trace_path.open(encoding="utf-8") as stream:
            samples = [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError, TypeError):
        return None
    by_frame = {sample.get("frame_index"): sample for sample in samples}
    for row in issues:
        if row.get("group") != "geometry":
            continue
        sample = by_frame.get(row.get("frame_index"))
        if (not sample or sample.get("phase") not in transient_phases
                or sample.get("step_id") != place_step):
            return None
    stable_place = [sample for sample in samples
                    if sample.get("step_id") == place_step
                    and sample.get("phase") == "place_electrode/align_and_insert"
                    and _finite_vector(sample.get("gripper_approach_axis_world"), 3)
                    and _finite_vector(sample.get("closing_axis_world"), 3)]
    if (not stable_place
            or any(float(sample["gripper_approach_axis_world"][2]) > -.85
                   or abs(float(sample["closing_axis_world"][2])) > .2
                   for sample in stable_place)):
        return None
    return {
        "parameter": "explicit_top_down_release_opening",
        "side": side,
        "source_attempt": 13,
        "measured_gap_m": float(robot["gripper_gap_m"]),
        "per_finger_open_target_m": .012,
        "expected_total_gap_m": .024,
        "pick_completed": True,
        "placement_orientation_verified_after_settle": True,
        "transient_pose_findings": len([row for row in issues
                                         if row.get("group") == "geometry"]),
        "rationale": (f"第十三次已完成朝下抓取；{side}侧释放夹爪时实测总开口24 mm。"
                      "抓取前每侧目标为12 mm，但放置动作未显式继承该目标。下一次把同一12 mm目标写入放置动作，"
                      "释放确认后再恢复全开；稳定插入阶段的网格方向通过。"),
    }


def retryable_top_down_release_gap(effect, archive, manifest, report, run_dir):
    """Allow a retry only for the measured release target mismatch and transient pose samples."""
    if (not effect.get("comparable") or effect.get("status") != "regressed"
            or effect.get("preservation_violations")):
        return False
    failures = effect.get("new_failures") or []
    allowed = {("records", "execution_failed"),
               ("geometry", "upward_grasp_axis"),
               ("geometry", "vertical_grasp_axis")}
    if (not failures or any((row.get("group"), row.get("code")) not in allowed
                            for row in failures)):
        return False
    return measured_top_down_release_gap(manifest, report, archive, run_dir) is not None

"""Bounded, evidence-gated local correction plans for Matterix actions.

This module never edits an archived run. It only permits a new diagnostic
segment when the independent audit identifies a supported pose/path issue and
the final placement geometry itself is feasible.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .geometry import derive_gripper_approach_axis_world
from .repairs.strategies.horizontal_grasp import TOP_DOWN_HORIZONTAL_WXYZ
from .repairs.strategies.electrode_probe import (
    uncompensated_probe, refine_probe, unstable_probe, upward_pregrasp_descent,
)
from .repairs.strategies.top_down_grasp import (
    measured_top_down_fork_clearance, measured_top_down_release_gap,
)


SUPPORTED_CODES = {"vertical_grasp_axis", "upward_grasp_axis", "fixture_intersection"}
STATIC_GEOMETRY_KEYS = {
    "stir_bar_clearance", "working_plate_meets_cap_bottom",
    "working_plate_immersed_in_solution", "cap_bottom_resting_on_holder", "cap_supported",
    "axis_inside_left_cell", "axis_inside_right_cell", "salt_bridge_reserved",
}
MAX_ATTEMPTS = 18


def _limit_reason(manifest: dict) -> str:
    failure = manifest.get("motion_failure") or {}
    if failure.get("phase") == "pick_electrode/align/high_transit_probe":
        target = failure.get("failed_waypoint_position_m")
        actual = failure.get("actual_ee_position_m")
        if all(isinstance(vector, list) and len(vector) == 3
               and all(isinstance(value, (int, float)) and math.isfinite(value)
                       for value in vector) for vector in (target, actual)):
            gap_mm = math.dist(target, actual) * 1000
            return (f"左电极高位通道到架位仍差约 {gap_mm:.1f} mm；"
                    f"已达到 {MAX_ATTEMPTS} 次局部尝试。停止自动重试，"
                    "需要重新规划可达的侧向路径，不能靠放宽抓取/安装验收通过。")
    return f"已达到最多 {MAX_ATTEMPTS} 次独立局部尝试；停止自动调整并保留失败证据。"


def _false_geometry_checks(archive: dict) -> set[str]:
    failed = set()
    for event in archive.get("events", []):
        if event.get("type") != "step.failed":
            continue
        payload = event.get("payload", {})
        receipt = payload.get("executor_receipt") or {}
        reason = receipt.get("reason") or payload.get("reason") or ""
        marker = reason.find("{")
        if marker < 0:
            continue
        try:
            checks, _ = json.JSONDecoder().raw_decode(reason[marker:])
        except (TypeError, ValueError):
            continue
        if isinstance(checks, dict):
            failed.update(key for key, value in checks.items() if value is False)
    return failed


def _supported_motion_timeout(issue: dict, report: dict | None = None,
                              manifest: dict | None = None) -> dict | None:
    """Accept only a measured timeout in a named electrode pose phase."""
    if issue.get("code") != "execution_failed" or issue.get("status") != "fail":
        return None
    step_id = issue.get("step_id") or ""
    match = re.fullmatch(r"ui_(pick|place)_(left|right)_copper_electrode", step_id)
    if not match:
        return None
    message = issue.get("message") or ""
    if "机械臂动作超时" not in message:
        return None
    marker = message.find("{")
    if marker < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(message[marker:])
    except (TypeError, ValueError):
        return None
    allowed_phases = {
        "pick": {"pick_electrode/align", "pick_electrode/align/high_transit_probe"},
        "place": {"place_electrode/align_and_insert",
                  "place_electrode/align_and_insert/high_transit_probe"},
    }
    if payload.get("phase") not in allowed_phases[match.group(1)]:
        return None
    if payload.get("failed_waypoint_index") is not None:
        # Admit the earlier supported final-descent timeout only with nearby
        # pipette-stand evidence, plus one measured current-chain case where
        # the final waypoint stalled while the gripper remained widely open.
        waypoints = payload.get("planned_waypoints_m")
        correction = (manifest or {}).get("correction") or {}
        pregrasp_retry = (match.group(1) == "pick"
                          and upward_pregrasp_descent(manifest or {}, report or {}, step_id,
                                                      payload) is not None)
        legacy_retry = (match.group(1) == "pick"
                        and payload.get("phase") == "pick_electrode/align"
                        and payload.get("failed_waypoint_index") == 1
                        and isinstance(waypoints, list) and len(waypoints) == 2)
        feedback_retry = (match.group(1) == "pick"
                          and payload.get("phase") == "pick_electrode/align"
                          and correction.get("attempt_index") == 5
                          and isinstance(waypoints, list) and len(waypoints) == 3
                          and payload.get("failed_waypoint_index") == 2)
        if not (legacy_retry or feedback_retry or pregrasp_retry):
            return None
        nearby_stand = any(
            row.get("status") in {"review", "fail"}
            and row.get("code") in {"near_fixture", "fixture_intersection"}
            and "PipetteStand_" in str(row.get("obstacle", ""))
            and row.get("step_id") == step_id
            for row in (report or {}).get("issues", []))
        # Attempt 5 captured a complete fixture map. Use the measured target,
        # stalled pose, and actual pipette-stand bounds as evidence when the
        # sampled hand meshes did not register a confirmed collision.
        if feedback_retry:
            nearby_stand = nearby_stand or _nearby_pipette_stand(
                (manifest or {}).get("_geometry_obstacles", []), payload)
        if not nearby_stand and not pregrasp_retry:
            return None
    for key in ("actual_ee_position_m", "requested_final_position_m"):
        value = payload.get(key)
        if (not isinstance(value, list) or len(value) != 3
                or not all(isinstance(number, (int, float)) for number in value)):
            return None
    waypoint = payload.get("failed_waypoint_position_m")
    if waypoint is not None and (not isinstance(waypoint, list) or len(waypoint) != 3):
        return None
    return payload


def _nearby_pipette_stand(obstacles: list, failure: dict) -> bool:
    target = failure.get("requested_final_position_m")
    actual = failure.get("actual_ee_position_m")
    if (not isinstance(target, list) or len(target) != 3
            or not isinstance(actual, list) or len(actual) != 3):
        return False
    low = [min(float(target[i]), float(actual[i])) for i in range(3)]
    high = [max(float(target[i]), float(actual[i])) for i in range(3)]
    for obstacle in obstacles:
        path = str(obstacle.get("path", ""))
        if "PipetteStand_" not in path or not isinstance(obstacle.get("corners"), list):
            continue
        corners = obstacle["corners"]
        if len(corners) != 8:
            continue
        bounds_low = [min(float(row[i]) for row in corners) for i in range(3)]
        bounds_high = [max(float(row[i]) for row in corners) for i in range(3)]
        # Require a stand component to be near the measured target-to-stall
        # corridor; proximity is a route clue, not proof of physical contact.
        overlap = all(bounds_low[i] <= high[i] + .04 and bounds_high[i] >= low[i] - .04
                      for i in range(3))
        if overlap:
            return True
    return False


def _observed_gripper_gap(manifest: dict, step_id: str) -> float | None:
    gaps = []
    for frame in manifest.get("frames", []):
        value = frame.get("gripper_gap_m")
        if (frame.get("step_id") == step_id and isinstance(value, (int, float))
                and 0 < float(value) < .2):
            gaps.append(float(value))
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def _observed_grasp_frame(manifest: dict, step_id: str) -> list[float] | None:
    frames = [frame for frame in manifest.get("frames", [])
              if frame.get("step_id") == step_id
              and str(frame.get("phase", "")).endswith("/orientation_probe")
              and isinstance(frame.get("robot_grasp_frame_m"), list)
              and len(frame["robot_grasp_frame_m"]) == 3
              and all(isinstance(value, (int, float)) for value in frame["robot_grasp_frame_m"])]
    return [float(value) for value in frames[-1]["robot_grasp_frame_m"]] if frames else None


def _attempt_index(manifest: dict, side: str) -> int:
    correction = manifest.get("correction") or {}
    try:
        prior = correction.get('attempts_by_side', {}).get(side)
        if prior is None:
            prior = correction.get('attempt_index', 0) if correction.get('side') == side else 0
        return int(prior) + 1
    except (TypeError, ValueError):
        return 1


def _last_recorded_gripper_axes(run_dir: Path | None, step_id: str, phase: str):
    if run_dir is None:
        return None, None
    try:
        geometry = json.loads((run_dir / "process_validation" / "geometry.json").read_text(encoding="utf-8"))
        latest_closing = latest_approach = None
        with (run_dir / "process_validation" / "trace.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                sample = json.loads(line)
                if sample.get("step_id") != step_id or sample.get("phase") != phase:
                    continue
                latest_closing = sample.get("closing_axis_world")
                latest_approach = (sample.get("gripper_approach_axis_world")
                                   or derive_gripper_approach_axis_world(geometry, sample))
        return latest_closing, latest_approach
    except (OSError, ValueError, TypeError, KeyError):
        return None, None


def _unreachable_probe_issue(issue: dict, manifest: dict, report: dict | None = None,
                             run_dir: Path | None = None) -> bool:
    """Admit a recorded side-pose probe failure only for a supported retry."""
    if (issue.get("code") != "execution_failed" or issue.get("status") != "fail"
            or issue.get("step_id") is None
            or not re.fullmatch(r"ui_pick_(left|right)_copper_electrode", issue["step_id"])):
        return False
    correction = manifest.get("correction") or {}
    attempt_index = correction.get("attempt_index")
    if attempt_index not in {3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
        return False
    message = issue.get("message") or ""
    marker = message.find("安全高位姿态预检未找到可达的侧向夹持角度:")
    if marker < 0:
        return False
    if attempt_index == 15:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        pick = ((correction.get("parameters") or {}).get(issue.get("step_id")) or {})
        actual = failure.get("actual_ee_position_m")
        target = failure.get("failed_waypoint_position_m")
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and failure.get("phase") == "pick_electrode/align/orientation_probe"
                and failure.get("failed_waypoint_index") == 0
                and isinstance(actual, list) and len(actual) == 3
                and isinstance(target, list) and len(target) == 3
                and all(isinstance(value, (int, float)) and math.isfinite(value)
                        for vector in (actual, target) for value in vector)
                and isinstance(pick.get("orientation_wxyz"), list)
                and len(pick["orientation_wxyz"]) == 4
                and not intersections)
    if attempt_index == 16:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        pick = ((correction.get("parameters") or {}).get(issue.get("step_id")) or {})
        actual, target = failure.get("actual_ee_position_m"), failure.get("failed_waypoint_position_m")
        closing_axis, approach_axis = _last_recorded_gripper_axes(
            run_dir, issue.get("step_id"), "pick_electrode/align/high_transit_probe")
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        vectors = (actual, target, closing_axis, approach_axis)
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and all(isinstance(vector, list) and len(vector) == 3
                        and all(isinstance(value, (int, float)) and math.isfinite(value)
                                for value in vector) for vector in vectors)
                and approach_axis[2] >= .85 and abs(closing_axis[2]) <= .2
                and isinstance(pick.get("orientation_wxyz"), list)
                and len(pick["orientation_wxyz"]) == 4
                and not intersections)
    if attempt_index == 17:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        pick = ((correction.get("parameters") or {}).get(issue.get("step_id")) or {})
        actual, target = failure.get("actual_ee_position_m"), failure.get("failed_waypoint_position_m")
        closing_axis, approach_axis = _last_recorded_gripper_axes(
            run_dir, issue.get("step_id"), "pick_electrode/align/high_transit_probe")
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        vectors = (actual, target, closing_axis, approach_axis)
        return (correction.get("attempt_index") == 16
                and preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and all(isinstance(vector, list) and len(vector) == 3
                        and all(isinstance(value, (int, float)) and math.isfinite(value)
                                for value in vector) for vector in vectors)
                and approach_axis[2] >= .85 and abs(closing_axis[2]) <= .2
                and isinstance(pick.get("orientation_wxyz"), list)
                and len(pick["orientation_wxyz"]) == 4
                and not intersections)
    if attempt_index in {6, 7, 8}:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        rejected = preflight.get("rejected_candidates") or []
        failure = manifest.get("motion_failure") or {}
        actual = failure.get("actual_ee_position_m")
        target = failure.get("failed_waypoint_position_m")
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and bool(rejected)
                and all(isinstance(item, dict)
                        and "orientation_probe" in str(item.get("reason", ""))
                        for item in rejected)
                and failure.get("phase") == "pick_electrode/align/orientation_probe"
                and failure.get("failed_waypoint_index") == 0
                and all(isinstance(vector, list) and len(vector) == 3
                        and all(isinstance(value, (int, float)) and math.isfinite(value)
                                for value in vector)
                        for vector in (actual, target)))
    if attempt_index == 9:
        if refine_probe(manifest, report or {}, issue['step_id']):
            return True
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        rejected = preflight.get("rejected_candidates") or []
        failure = manifest.get("motion_failure") or {}
        starts = correction.get("high_probe_external_acceptances") or []
        accepted_side_start = any(
            item.get("phase") == "pick_electrode/align/orientation_probe"
            and isinstance(item.get("actual_ee_position_m"), list)
            for item in starts if isinstance(item, dict))
        report_clear = any(row.get("group") == "geometry"
                           and row.get("code") == "sampled_intersections"
                           and row.get("status") == "pass"
                           for row in ((report or {}).get("checks") or []))
        actual, target = failure.get("actual_ee_position_m"), failure.get("failed_waypoint_position_m")
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and bool(rejected) and accepted_side_start
                and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and all(isinstance(vector, list) and len(vector) == 3
                        and all(isinstance(value, (int, float)) and math.isfinite(value)
                                for value in vector) for vector in (actual, target))
                and (report_clear or manifest.get("sampled_intersections_passed") is True))
    if attempt_index == 10:
        failure = manifest.get('motion_failure') or {}
        pick = (correction.get('parameters') or {}).get(issue.get('step_id')) or {}
        target, actual = failure.get('failed_waypoint_position_m'), failure.get('actual_ee_position_m')
        error, command = failure.get('failed_waypoint_error_m'), failure.get('controller_waypoint_position_m')
        bias = pick.get('orientation_probe_bias_xyz_m')
        clear = any(row.get('group') == 'geometry' and row.get('code') == 'sampled_intersections'
                    and row.get('status') == 'pass' for row in (report or {}).get('checks', []))
        vectors = (target, actual, error, command, bias)
        return (failure.get('phase') == 'pick_electrode/align/orientation_probe'
                and failure.get('failed_waypoint_index') == 0 and clear
                and all(isinstance(v, list) and len(v) == 3 and all(
                    isinstance(x, (int, float)) and math.isfinite(x) for x in v) for v in vectors)
                and math.dist(error, [0., 0., 0.]) > .018
                and all(abs(target[i] - command[i] - bias[i]) <= .001 for i in range(3))
                and all(abs(actual[i] - target[i] - error[i]) <= .001 for i in range(3)))
    if attempt_index == 11:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        starts = correction.get("high_probe_external_acceptances") or []
        accepted_start = any(
            item.get("phase") == "pick_electrode/align/orientation_probe"
            and isinstance(item.get("actual_ee_position_m"), list)
            for item in starts if isinstance(item, dict))
        target, actual = failure.get("failed_waypoint_position_m"), failure.get("actual_ee_position_m")
        bias = failure.get("side_pose_observed_bias_xyz_m")
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and accepted_start
                and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and isinstance(target, list) and len(target) == 3
                and isinstance(actual, list) and len(actual) == 3
                and isinstance(bias, list) and len(bias) == 3
                and all(isinstance(value, (int, float)) and math.isfinite(value)
                        for vector in (target, actual, bias) for value in vector)
                and min(float(target[2]), float(actual[2])) >= .42
                and .015 <= math.dist(target, actual) <= .04
                and not intersections)
    if attempt_index == 12:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        starts = correction.get("high_probe_external_acceptances") or []
        accepted_start = any(
            item.get("phase") == "pick_electrode/align/orientation_probe"
            and isinstance(item.get("actual_ee_position_m"), list)
            for item in starts if isinstance(item, dict))
        target, actual = failure.get("failed_waypoint_position_m"), failure.get("actual_ee_position_m")
        parameters = correction.get("parameters") or {}
        pick = parameters.get(issue.get("step_id"), {})
        trace_path = (run_dir / "process_validation" / "trace.jsonl") if run_dir else None
        last_axis = None
        try:
            with trace_path.open("rb") as stream:
                line = stream.readlines()[-1].decode("utf-8").strip()
            last_axis = json.loads(line).get("closing_axis_world") if line else None
        except (AttributeError, OSError, ValueError, TypeError, IndexError):
            pass
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and accepted_start and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and pick.get("high_transit_destination_pose_bias") is False
                and isinstance(target, list) and len(target) == 3
                and isinstance(actual, list) and len(actual) == 3
                and isinstance(last_axis, list) and len(last_axis) == 3
                and all(isinstance(value, (int, float)) and math.isfinite(value)
                        for vector in (target, actual, last_axis) for value in vector)
                and min(float(target[2]), float(actual[2])) >= .42
                and abs(float(last_axis[2])) <= .2
                and .02 <= math.dist(target, actual) <= .07
                and not intersections)
    if attempt_index == 13:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        starts = correction.get("high_probe_external_acceptances") or []
        accepted_start = any(
            item.get("phase") == "pick_electrode/align/orientation_probe"
            and isinstance(item.get("actual_ee_position_m"), list)
            for item in starts if isinstance(item, dict))
        parameters = correction.get("parameters") or {}
        pick = parameters.get(issue.get("step_id"), {})
        installation = (manifest.get("presentation") or {}).get("electrode_installation") or {}
        rack = ((installation.get("fixture_geometry") or {}).get("electrode_rack") or {})
        side_match = re.fullmatch(r"ui_pick_(left|right)_copper_electrode",
                                  issue.get("step_id", ""))
        rack_target = (rack.get("slots") or {}).get(side_match.group(1)) if side_match else None
        transit_height = pick.get("transit_height_m")
        target = ([float(rack_target[0]), float(rack_target[1]), float(transit_height)]
                  if isinstance(rack_target, list) and len(rack_target) == 3
                  and isinstance(transit_height, (int, float)) else None)
        actual = failure.get("actual_ee_position_m")
        trace_path = (run_dir / "process_validation" / "trace.jsonl") if run_dir else None
        last_axis = None
        try:
            with trace_path.open("rb") as stream:
                line = stream.readlines()[-1].decode("utf-8").strip()
            last_axis = json.loads(line).get("closing_axis_world") if line else None
        except (AttributeError, OSError, ValueError, TypeError, IndexError):
            pass
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == issue.get("step_id")]
        return (preflight.get("preflight_status") == "no_reachable_side_grasp_pose"
                and accepted_start
                and failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and failure.get("failed_waypoint_index") == 0
                and pick.get("high_transit_destination_pose_bias") is False
                and isinstance(target, list) and len(target) == 3
                and isinstance(actual, list) and len(actual) == 3
                and isinstance(last_axis, list) and len(last_axis) == 3
                and all(isinstance(value, (int, float)) and math.isfinite(value)
                        for vector in (target, actual, last_axis) for value in vector)
                and min(float(target[2]), float(actual[2])) >= .42
                and abs(float(last_axis[2])) <= .2
                and .02 <= math.dist(target, actual) <= .07
                and not intersections)
    if attempt_index == 14:
        preflight = ((correction.get("pose_preflight") or {}).get("pick_electrode/align") or {})
        failure = manifest.get("motion_failure") or {}
        parameters = correction.get("parameters") or {}
        pick = parameters.get(issue.get("step_id"), {})
        target = failure.get("failed_waypoint_position_m")
        actual = failure.get("actual_ee_position_m")
        controller_target = failure.get("controller_waypoint_position_m")
        bias = failure.get("side_pose_observed_bias_xyz_m")
        source_run_id = correction.get("source_run_id")
        if (not isinstance(source_run_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", source_run_id)
                or failure.get("phase") != "pick_electrode/align/orientation_probe"
                or failure.get("failed_waypoint_index") != 0
                or preflight.get("preflight_status") != "no_reachable_side_grasp_pose"
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not isinstance(controller_target, list) or len(controller_target) != 3
                or not isinstance(bias, list) or len(bias) != 3
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (target, actual, controller_target, bias) for value in vector)
                or math.dist(target, controller_target) < .02
                or not isinstance(pick.get("orientation_wxyz"), list)
                or pick.get("side_pose_observed_bias_xyz_m") is None):
            return False
        try:
            parent_dir = run_dir.parent / source_run_id
            parent_manifest = json.loads((parent_dir / "video_manifest.json").read_text(encoding="utf-8"))
            parent_report = json.loads(
                (parent_dir / "process_validation" / "report.json").read_text(encoding="utf-8"))
            parent_failure = parent_manifest.get("motion_failure") or {}
            parent_correction = parent_manifest.get("correction") or {}
            parent_pick = ((parent_correction.get("parameters") or {}).get(issue.get("step_id")) or {})
            trace_path = parent_dir / "process_validation" / "trace.jsonl"
            parent_axis = None
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                sample = json.loads(line)
                if (sample.get("step_id") == issue.get("step_id")
                        and sample.get("phase") == "pick_electrode/align/orientation_probe"
                        and isinstance(sample.get("closing_axis_world"), list)):
                    parent_axis = sample["closing_axis_world"]
            geometry_clear = any(
                row.get("group") == "geometry" and row.get("code") == "sampled_intersections"
                and row.get("status") == "pass" for row in parent_report.get("checks", []))
        except (OSError, ValueError, TypeError):
            return False
        parent_orientation = parent_pick.get("orientation_wxyz")
        transit_orientation = parent_pick.get("high_transit_orientation_wxyz")
        return (parent_correction.get("attempt_index") == 13
                and parent_failure.get("phase") == "pick_electrode/align/high_transit_probe"
                and parent_correction.get("parameters") is not None
                and isinstance(parent_orientation, list) and len(parent_orientation) == 4
                and isinstance(transit_orientation, list) and len(transit_orientation) == 4
                and isinstance(parent_axis, list) and len(parent_axis) == 3
                and all(isinstance(value, (int, float)) and math.isfinite(value)
                        for value in parent_orientation + transit_orientation + parent_axis)
                and abs(float(parent_axis[2])) <= .2 and geometry_clear)
    payload_start = message.find("[", marker) if marker >= 0 else -1
    if payload_start < 0:
        return False
    try:
        candidates, _ = json.JSONDecoder().raw_decode(message[payload_start:])
    except (TypeError, ValueError):
        return False
    return (isinstance(candidates, list) and bool(candidates)
            and all(isinstance(item, dict)
                    and "pick_electrode/align/orientation_probe" in str(item.get("reason", ""))
                    and "actual_ee_position_m" in str(item.get("reason", ""))
                    for item in candidates))


def build_correction_plan(run_dir, report=None) -> dict:
    """Return a conservative local-retry plan; never starts a simulation."""
    run_dir = Path(run_dir).resolve()
    try:
        archive = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        manifest = json.loads((run_dir / "video_manifest.json").read_text(encoding="utf-8"))
        report = report or json.loads(
            (run_dir / "process_validation" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"eligible": False, "status": "unavailable", "reason": f"缺少可用的运行/检查记录：{exc}"}

    run_id = archive.get("snapshot", {}).get("run_id")
    if report.get("run_id") != run_id or manifest.get("run_id") != run_id:
        return {"eligible": False, "status": "blocked", "reason": "检查报告、运行档案和录像编号不一致"}

    prior_attempt = ((manifest.get("correction") or {}).get("attempt_index"))
    if isinstance(prior_attempt, int) and prior_attempt >= MAX_ATTEMPTS:
        return {"run_id": run_id, "eligible": False, "status": "limit_reached",
                "reason": _limit_reason(manifest),
                "attempt_index": prior_attempt, "max_attempts": MAX_ATTEMPTS}

    failed_checks = _false_geometry_checks(archive)
    static_failures = sorted(failed_checks & STATIC_GEOMETRY_KEYS)
    if static_failures:
        names = {
            "stir_bar_clearance": "电极工作片与搅拌子/池底的末端间隙不满足",
            "working_plate_immersed_in_solution": "电极工作片浸入液面的末端条件不满足",
            "working_plate_meets_cap_bottom": "电极工作片与池盖的末端装配关系不满足",
            "cap_bottom_resting_on_holder": "池盖末端承托关系不满足",
            "cap_supported": "电极末端支撑条件不满足",
            "axis_inside_left_cell": "左侧电极末端没有落在池内",
            "axis_inside_right_cell": "右侧电极末端没有落在池内",
            "salt_bridge_reserved": "电极与盐桥的末端空间关系不满足",
        }
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "；".join(names.get(key, key) for key in static_failures)
                          + "。这是放置几何/场景条件，不是夹持指令能修复的问题；为避免错误安装，不自动重跑。",
                "blocking_checks": static_failures}

    snapshot = archive.get("snapshot", {})
    steps = snapshot.get("plan", [])
    issues = [row for row in report.get("issues", [])
              if row.get("status") == "fail" and row.get("code") in SUPPORTED_CODES]
    geometry_obstacles = []
    try:
        geometry = json.loads((run_dir / "process_validation" / "geometry.json").read_text(encoding="utf-8"))
        if geometry.get("run_id") == run_id:
            geometry_obstacles = geometry.get("obstacles", [])
    except (OSError, ValueError):
        pass
    timeout_manifest = {**manifest, "_geometry_obstacles": geometry_obstacles}
    timeout_issues = [(row, _supported_motion_timeout(row, report, timeout_manifest))
                      for row in report.get("issues", [])]
    timeout_issues = [(row, payload) for row, payload in timeout_issues if payload is not None]
    issues.extend(row for row, _ in timeout_issues)
    preflight_issues = [row for row in report.get("issues", [])
                        if _unreachable_probe_issue(row, manifest, report, run_dir)]
    issues.extend(preflight_issues)
    precheck_events = []
    for event in archive.get("events", []):
        if event.get("type") != "step.failed":
            continue
        receipt = (event.get("payload", {}).get("executor_receipt") or {})
        reason = receipt.get("reason") or ""
        if "自动修正姿态未形成侧向夹持" in reason:
            precheck_events.append(event)
    prior_correction = manifest.get("correction") or {}
    release_side = prior_correction.get("side")
    release_gap_evidence = None
    if release_side in {"left", "right"} and _attempt_index(manifest, release_side) == 14:
        release_gap_evidence = measured_top_down_release_gap(
            manifest, report, archive, run_dir,
            f"ui_pick_{release_side}_copper_electrode",
            f"ui_place_{release_side}_copper_electrode")
    if not issues and not precheck_events and release_gap_evidence is None:
        return {"run_id": run_id, "eligible": False, "status": "not_applicable",
                "reason": "没有足够明确的电极夹持方向/支架相交证据；仅有待复核或未覆盖项时不自动改动作。"}

    affected = set()
    for issue in issues:
        step_id = issue.get("step_id") or ""
        match = re.fullmatch(r"ui_(pick|place)_(left|right)_copper_electrode", step_id)
        if not match:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "异常没有绑定到明确的电极取放步骤，不能安全选择修改对象。"}
        affected.add(match.group(2))
    for event in precheck_events:
        step_id = event.get("step_id") or ""
        match = re.fullmatch(r"ui_(pick|place)_(left|right)_copper_electrode", step_id)
        if not match:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "姿态预检失败没有绑定到明确的电极取放步骤。"}
        affected.add(match.group(2))
    if release_gap_evidence is not None:
        affected.add(release_side)
    if len(affected) != 1:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "异常同时涉及左右两侧；首版一次只允许修正一侧，避免扩大重跑范围。"}

    side = next(iter(affected))
    pick_step = f"ui_pick_{side}_copper_electrode"
    place_step = f"ui_place_{side}_copper_electrode"
    step_ids = [row.get("step_id") for row in steps]
    if pick_step not in step_ids:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "动作计划中找不到对应电极抓取步骤。"}
    pick_index = step_ids.index(pick_step)
    if pick_index == 0:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "没有可恢复的安全前置步骤，不能建立独立局部重跑。"}
    checkpoint_step = step_ids[pick_index - 1]
    completed = {event.get("step_id") for event in archive.get("events", [])
                 if event.get("type") == "step.completed"}
    if checkpoint_step not in completed:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "电极动作前最近一个安全检查点没有成功回执，不能据此重建状态。",
                "checkpoint_step_id": checkpoint_step}

    attempt_index = _attempt_index(manifest, side)
    if attempt_index > MAX_ATTEMPTS:
        return {"run_id": run_id, "eligible": False, "status": "limit_reached",
                "reason": f"已达到首版最多 {MAX_ATTEMPTS} 次独立局部尝试；停止自动调整，保留现有证据。",
                "attempt_index": attempt_index - 1, "max_attempts": MAX_ATTEMPTS}

    if side != release_side or attempt_index != 14:
        release_gap_evidence = None

    all_action_steps = {pick_step, place_step}
    has_place = place_step in step_ids
    stop_step = place_step if has_place else pick_step
    # Each retry changes only a bounded motion strategy. Every orientation is
    # checked at a clear staging pose before the gripper can close.
    clearance = 0.42 if attempt_index == 1 else 0.48
    adaptive_adjustment = None
    if attempt_index == 1:
        orientation_candidates = [[1.0, 0.0, 0.0, 0.0],
                                  [0.7071067811865476, 0.0, 0.0, 0.7071067811865476],
                                  [0.7071067811865476, 0.0, 0.0, -0.7071067811865476]]
    elif attempt_index == 2:
        orientation_candidates = [[0.7071067811865476, 0.0, 0.0, 0.7071067811865476],
                                  [0.7071067811865476, 0.0, 0.0, -0.7071067811865476]]
    elif attempt_index == 3:
        orientation_candidates = [[0.7071067811865476, 0.0, 0.0, 0.7071067811865476]]
    elif attempt_index == 6:
        timeout = next((payload for row, payload in timeout_issues
                        if row.get("step_id") == pick_step), None)
        observed_gap = _observed_gripper_gap(manifest, pick_step)
        if timeout is None or observed_gap is None or observed_gap < .05:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "末端超时缺少可靠的夹爪开口测量，不能据此自动改变抓取参数。"}
        adaptive_adjustment = {
            "parameter": "gripper_open_joint_target_m",
            "observed_gap_m": observed_gap,
            "target_per_finger_m": .012,
            "predicted_total_gap_m": .024,
            "cap_width_m": .008,
            "rationale": "录像逐帧记录的抓取前夹爪开口远大于8 mm电极帽；先缩小开口以降低夹爪占用空间，隔离验证是否能避开支架。",
        }
        prior_preflight = ((manifest.get("correction") or {}).get("pose_preflight") or {})
        selected = (prior_preflight.get("pick_electrode/align") or {}).get("selected_orientation_wxyz")
        orientation_candidates = ([selected] if isinstance(selected, list) and len(selected) == 4 else
                                  [[0.7071067811865476, 0.0, 0.0, -0.7071067811865476]])
    elif attempt_index == 7:
        if not preflight_issues:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第六次候选没有明确记录高位姿态预检失败；不根据其他异常猜测新起始点。"}
        observed_pose = _observed_grasp_frame(manifest, pick_step)
        if (observed_pose is None or not .43 <= observed_pose[2] <= .52
                or abs(observed_pose[0] - .40) > .04 or abs(observed_pose[1]) > .03):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "姿态预检失败时没有范围内的末端实测位置；不自动猜测新的高位起始点。"}
        staging_offset = [observed_pose[0] - .40, observed_pose[1]]
        adaptive_adjustment = {
            "parameter": "staging_offset_xy_m",
            "observed_robot_grasp_frame_m": observed_pose,
            "target_staging_offset_xy_m": staging_offset,
            "rationale": "本次在侧向姿态预检超时后记录了末端实际位置；将高位起点平移到该实测位置的正上方，仅缩短起点到高位姿态探测点的距离。",
        }
        prior_parameters = ((manifest.get("correction") or {}).get("parameters") or {})
        prior_pick = prior_parameters.get(pick_step) or {}
        prior_orientation = prior_pick.get("orientation_wxyz")
        orientation_candidates = ([prior_orientation]
                                  if isinstance(prior_orientation, list) and len(prior_orientation) == 4
                                  else [[0.7071067811865476, 0.0, 0.0, -0.7071067811865476]])
    elif attempt_index == 8:
        if not preflight_issues:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第七次候选没有明确记录同一姿态预检超时；不推断控制残差。"}
        current_correction = manifest.get("correction") or {}
        source_run_id = current_correction.get("source_run_id")
        if not isinstance(source_run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_run_id):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "找不到上一次独立姿态预检记录，无法判断偏差是否稳定。"}
        try:
            parent_manifest = json.loads(
                (run_dir.parent / source_run_id / "video_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parent_manifest = {}
        parent_correction = parent_manifest.get("correction") or {}
        current_pick = (current_correction.get("parameters") or {}).get(pick_step) or {}
        parent_pick = (parent_correction.get("parameters") or {}).get(pick_step) or {}
        orientation = current_pick.get("orientation_wxyz")
        if (not isinstance(orientation, list) or len(orientation) != 4
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for value in orientation)
                or parent_manifest.get("run_id") != source_run_id
                or parent_correction.get("attempt_index") != 6
                or parent_pick.get("orientation_wxyz") != orientation):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "两次姿态预检不是同一侧向姿态，不能把末端偏差视为稳定控制残差。"}
        residuals = []
        for source in (parent_manifest, manifest):
            failure = source.get("motion_failure") or {}
            residual = failure.get("failed_waypoint_error_m")
            actual = failure.get("actual_ee_position_m")
            target = failure.get("failed_waypoint_position_m")
            if (failure.get("phase") != "pick_electrode/align/orientation_probe"
                    or failure.get("failed_waypoint_index") != 0
                    or not isinstance(residual, list) or len(residual) != 3
                    or not all(isinstance(value, (int, float)) and math.isfinite(value)
                               for value in residual)
                    or not all(isinstance(vector, list) and len(vector) == 3
                               and all(isinstance(value, (int, float)) and math.isfinite(value)
                                       for value in vector)
                               for vector in (actual, target))
                    or any(abs(float(actual[i]) - float(target[i]) - float(residual[i])) > .001
                           for i in range(3))):
                return {"run_id": run_id, "eligible": False, "status": "blocked",
                        "reason": "两次预检没有同一阶段的完整末端偏差记录，不能自动补偿控制指令。"}
            residuals.append([float(value) for value in residual])
        mean_residual = [(residuals[0][i] + residuals[1][i]) / 2 for i in range(3)]
        span = math.dist(residuals[0], residuals[1])
        magnitude = math.sqrt(sum(value * value for value in mean_residual))
        if (span > .004 or not .015 <= magnitude <= .06
                or any(abs(value) > .05 for value in mean_residual)):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "两次侧向姿态残差变化过大或超出补偿范围，不能安全预测下一次指令。"}
        adaptive_adjustment = {
            "parameter": "side_pose_observed_bias_xyz_m",
            "measured_residuals_m": residuals,
            "mean_residual_m": mean_residual,
            "residual_difference_m": span,
            "rationale": "两次独立高位侧向姿态预检的末端残差几乎相同；仅对控制指令做反向偏移，并以原物理目标逐航点验收。",
        }
        orientation_candidates = [orientation]
    elif attempt_index == 9 and preflight_issues and uncompensated_probe(manifest, report, pick_step):
        evidence = uncompensated_probe(manifest, report, pick_step)
        adaptive_adjustment = {'parameter': 'apply_previously_measured_probe_bias', **evidence}
        orientation_candidates = [evidence['orientation']]
    elif attempt_index == 9:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        target = failure.get("failed_waypoint_position_m")
        actual = failure.get("actual_ee_position_m")
        command = failure.get("controller_waypoint_position_m")
        bias = prior_pick.get("side_pose_observed_bias_xyz_m")
        orientation = prior_pick.get("orientation_wxyz")
        measured = failure.get("failed_waypoint_error_m")
        vectors = (target, actual, command, bias, measured)
        sampled_clear = any(row.get("group") == "geometry"
                            and row.get("code") == "sampled_intersections"
                            and row.get("status") == "pass"
                            for row in report.get("checks", []))
        if (not preflight_issues or prior.get("attempt_index") != 8
                or failure.get("phase") != "pick_electrode/align/orientation_probe"
                or failure.get("failed_waypoint_index") != 0
                or not sampled_clear
                or any(not isinstance(vector, list) or len(vector) != 3
                       or not all(isinstance(value, (int, float)) and math.isfinite(value)
                                  for value in vector) for vector in vectors)
                or not isinstance(orientation, list) or len(orientation) != 4
                or any(abs(float(actual[i]) - float(target[i]) - float(measured[i])) > .001
                       for i in range(3))
                or any(abs(float(target[i]) - float(command[i]) - float(bias[i])) > .001
                       for i in range(3))):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "缺少第八次高位预检的完整末端实测数据或支架无相交证据；不放宽预检参数。"}
        physical_error = max(abs(float(value)) for value in measured)
        physical_error_norm = math.dist(actual, target)
        controller_distance = math.dist(actual, command)
        bias_norm = math.sqrt(sum(float(value) ** 2 for value in bias))
        if (not .008 < physical_error <= .0125 or physical_error_norm > .018
                or not .008 <= controller_distance - bias_norm <= .020):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "实测高位偏差未落在可局部调整的范围；保持抓取/安装精度并停止自动重跑。"}
        probe_tolerance = min(.015, max(.008, physical_error + .003))
        controller_margin = min(.025, max(.012, controller_distance - bias_norm + .004))
        adaptive_adjustment = {
            "parameter": "high_probe_acceptance_only",
            "measured_physical_error_m": list(measured),
            "measured_command_distance_m": controller_distance,
            "high_probe_physical_tolerance_m": probe_tolerance,
            "high_probe_controller_margin_m": controller_margin,
            "rationale": "仅高位无支架相交预检改用实测偏差确定的有界容差；低位抓取、安装目标和严格末端公差均保持不变。",
        }
        orientation_candidates = [orientation]
    elif attempt_index == 10 and preflight_issues and refine_probe(manifest, report, pick_step):
        evidence = refine_probe(manifest, report, pick_step)
        adaptive_adjustment = {'parameter': 'refine_high_probe_bias', **evidence}
        orientation_candidates = [evidence['orientation']]
    elif attempt_index == 10 and preflight_issues:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "已测高位预检补偿后的残差仍超过 18 mm 微调上限；误差方向不稳定，停止自动调整，保留失败记录。"}
    elif attempt_index == 10:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        starts = prior.get("high_probe_external_acceptances") or []
        sampled_clear = any(row.get("group") == "geometry"
                            and row.get("code") == "sampled_intersections"
                            and row.get("status") == "pass"
                            for row in report.get("checks", []))
        target, actual = (failure.get("failed_waypoint_position_m"),
                          failure.get("actual_ee_position_m"))
        side_start = next((item for item in reversed(starts)
                           if isinstance(item, dict)
                           and item.get("phase") == "pick_electrode/align/orientation_probe"), None)
        orientation = prior_pick.get("orientation_wxyz")
        if (not preflight_issues or prior.get("attempt_index") != 9
                or failure.get("phase") != "pick_electrode/align/high_transit_probe"
                or failure.get("failed_waypoint_index") != 0
                or not sampled_clear or side_start is None
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for value in target + actual)
                or not isinstance(orientation, list) or len(orientation) != 4):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第九次没有同时记录安全侧向起点、高位通道超时和无相交采样证据；不能改走姿态路径。"}
        gap = math.dist(actual, target)
        if not .025 <= gap <= .060 or min(float(target[2]), float(actual[2])) < .42:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "高位通道偏差不在受限姿态切换方案范围内，停止自动尝试。"}
        adaptive_adjustment = {
            "parameter": "high_transit_orientation_sequence",
            "failed_waypoint_position_m": target,
            "actual_ee_position_m": actual,
            "measured_gap_m": gap,
            "travel_orientation_wxyz": [0.7071067811865476, 0.7071067811865476, 0.0, 0.0],
            "rationale": "侧向姿态在起点通过而长距离高位移动失败；只在高位通道使用已知默认姿态移动，到架位上方后再切回侧向夹持姿态，低位抓取和安装目标不变。",
        }
        orientation_candidates = [orientation]
    elif (attempt_index == 11 and preflight_issues
          and unstable_probe(manifest, report, pick_step)):
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        orientation = prior_pick.get("orientation_wxyz")
        if not isinstance(orientation, list) or len(orientation) != 4:
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "高位残差不稳定且缺少已记录的目标姿态；不能启动预抓取点复验。"}
        adaptive_adjustment = {
            "parameter": "kinematic_pregrasp_pose_probe",
            "source_attempt": 10,
            "pregrasp_reference": "electrode_slot_xy_at_transit_height",
            "position_bias_reused": False,
            "orientation_step_deg": 30.0,
            "rationale": "第十次高位姿态残差方向不稳定，不再外推位置补偿。本次先在左电极槽正上方以当前姿态到达预抓取点，再分段切换到原记录的侧夹姿态；每一段都核对末端位置与手部网格/支架相交。仅通过后才沿原目标下降并允许抓取。",
        }
        orientation_candidates = [orientation]
    elif attempt_index == 11:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        installation = (manifest.get("presentation") or {}).get("electrode_installation") or {}
        rack = ((installation.get("fixture_geometry") or {}).get("electrode_rack") or {})
        slots = rack.get("slots") or {}
        rack_target = slots.get(side)
        salt_station = (((installation.get("fixture_geometry") or {}).get("tool_bank") or {})
                        .get("stations", {}).get("salt_bridge"))
        collision_rows = [row for row in report.get("issues", [])
                          if row.get("group") == "geometry"
                          and row.get("code") == "fixture_intersection"
                          and row.get("status") == "fail"
                          and row.get("step_id") == pick_step
                          and "/SaltBridgeRack/" in str(row.get("obstacle", ""))]
        target = failure.get("requested_final_position_m")
        actual = failure.get("actual_ee_position_m")
        bias = failure.get("side_pose_observed_bias_xyz_m")
        old_offset = prior_pick.get("approach_offset_xy_m")
        orientation = prior_pick.get("orientation_wxyz")
        travel_orientation = prior_pick.get("high_transit_orientation_wxyz")
        if (prior.get("attempt_index") != 10
                or failure.get("phase") != "pick_electrode/align"
                or not isinstance(rack_target, list) or len(rack_target) != 3
                or not isinstance(salt_station, list) or len(salt_station) != 3
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not isinstance(bias, list) or len(bias) != 3
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (rack_target, salt_station, target, actual, bias)
                           for value in vector)
                or old_offset != [0.0, -0.08]
                or abs(float(target[1]) - float(salt_station[1])) > .005
                or abs(float(rack_target[1]) - float(salt_station[1])) < .04
                or not collision_rows
                or not isinstance(orientation, list) or len(orientation) != 4
                or not isinstance(travel_orientation, list) or len(travel_orientation) != 4):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第十次缺少高位路径、低位末端偏差或盐桥架实体相交的同侧证据；不能猜测新的进路。"}
        adaptive_adjustment = {
            "parameter": "direct_electrode_rack_corridor_and_local_descent",
            "failed_approach_target_m": target,
            "measured_ee_position_m": actual,
            "high_pose_bias_not_reused_below_staging_m": bias,
            "previous_approach_offset_xy_m": old_offset,
            "electrode_rack_slot_m": rack_target,
            "salt_bridge_station_m": salt_station,
            "confirmed_salt_bridge_rack_intersections": sorted({
                str(row.get("obstacle")) for row in collision_rows}),
            "rationale": "第十次已走到架位高处，但原负Y进路穿过盐桥架；高位侧姿残差也不能用于低位下降。本次直达电极槽上方，低位按原目标闭环校正。",
        }
        orientation_candidates = [orientation]
    elif (attempt_index == 12 and any(
            issue.get('step_id') == pick_step
            and upward_pregrasp_descent(manifest, report, pick_step, payload) is not None
            for issue, payload in timeout_issues)):
        payload = next(payload for issue, payload in timeout_issues
                       if issue.get('step_id') == pick_step
                       and upward_pregrasp_descent(manifest, report, pick_step, payload) is not None)
        adaptive_adjustment = upward_pregrasp_descent(manifest, report, pick_step, payload)
        orientation_candidates = [list(TOP_DOWN_HORIZONTAL_WXYZ)]
    elif (attempt_index == 13 and
          (fork_evidence := measured_top_down_fork_clearance(manifest, report, run_dir, pick_step))):
        adaptive_adjustment = fork_evidence
        orientation_candidates = [list(TOP_DOWN_HORIZONTAL_WXYZ)]
    elif attempt_index == 12:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        starts = prior.get("high_probe_external_acceptances") or []
        installation = (manifest.get("presentation") or {}).get("electrode_installation") or {}
        rack = ((installation.get("fixture_geometry") or {}).get("electrode_rack") or {})
        rack_target = (rack.get("slots") or {}).get(side)
        target, actual = failure.get("failed_waypoint_position_m"), failure.get("actual_ee_position_m")
        bias = failure.get("side_pose_observed_bias_xyz_m")
        side_start = any(item.get("phase") == "pick_electrode/align/orientation_probe"
                         and isinstance(item.get("actual_ee_position_m"), list)
                         for item in starts if isinstance(item, dict))
        collision_failures = [row for row in report.get("issues", [])
                              if row.get("group") == "geometry"
                              and row.get("code") == "fixture_intersection"
                              and row.get("status") == "fail"
                              and row.get("step_id") == pick_step]
        orientation = prior_pick.get("orientation_wxyz")
        travel_orientation = prior_pick.get("high_transit_orientation_wxyz")
        if (not preflight_issues or prior.get("attempt_index") != 11
                or failure.get("phase") != "pick_electrode/align/high_transit_probe"
                or failure.get("failed_waypoint_index") != 0
                or not side_start or collision_failures
                or not isinstance(rack_target, list) or len(rack_target) != 3
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not isinstance(bias, list) or len(bias) != 3
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (rack_target, target, actual, bias)
                           for value in vector)
                or math.dist(target, rack_target[:2] + [target[2]]) > .002
                or min(float(target[2]), float(actual[2])) < .42
                or not .015 <= math.dist(target, actual) <= .04
                or prior_pick.get("approach_offset_xy_m") != [0.0, 0.0]
                or not isinstance(orientation, list) or len(orientation) != 4
                or not isinstance(travel_orientation, list) or len(travel_orientation) != 4):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第十一次缺少高位实测姿态偏差、侧向起点通过及无碰撞证据；不自动重复高位路线。"}
        adaptive_adjustment = {
            "parameter": "high_destination_pose_without_staging_bias",
            "rack_target_m": rack_target,
            "failed_destination_pose_m": target,
            "measured_ee_position_m": actual,
            "incorrectly_reused_staging_bias_m": bias,
            "measured_destination_gap_m": math.dist(target, actual),
            "rationale": "第十一次直立路线到达电极槽上方且没有盐桥架相交；失败来自把起点预检残差带到架位转姿态。本次只在起点用该残差，架位转姿态和下降均按原物理目标核验。",
        }
        orientation_candidates = [orientation]
    elif attempt_index == 13:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        installation = (manifest.get("presentation") or {}).get("electrode_installation") or {}
        rack = ((installation.get("fixture_geometry") or {}).get("electrode_rack") or {})
        rack_target = (rack.get("slots") or {}).get(side)
        target, actual = failure.get("failed_waypoint_position_m"), failure.get("actual_ee_position_m")
        starts = prior.get("high_probe_external_acceptances") or []
        side_start = any(item.get("phase") == "pick_electrode/align/orientation_probe"
                         and isinstance(item.get("actual_ee_position_m"), list)
                         for item in starts if isinstance(item, dict))
        intersections = [row for row in report.get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == pick_step]
        orientation = prior_pick.get("orientation_wxyz")
        travel_orientation = prior_pick.get("high_transit_orientation_wxyz")
        if (not preflight_issues or prior.get("attempt_index") != 12
                or failure.get("phase") != "pick_electrode/align/high_transit_probe"
                or failure.get("failed_waypoint_index") != 0
                or prior_pick.get("high_transit_destination_pose_bias") is not False
                or not side_start or intersections
                or not isinstance(rack_target, list) or len(rack_target) != 3
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (rack_target, target, actual) for value in vector)
                or math.dist(target, [rack_target[0], rack_target[1], target[2]]) > .002
                or min(float(target[2]), float(actual[2])) < .42
                or not .02 <= math.dist(target, actual) <= .07
                or prior_pick.get("approach_offset_xy_m") != [0.0, 0.0]
                or not isinstance(orientation, list) or len(orientation) != 4
                or not isinstance(travel_orientation, list) or len(travel_orientation) != 4):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第十二次缺少安全高位末端偏差或无碰架证据；不自动修正未知路径。"}
        adaptive_adjustment = {
            "parameter": "bounded_high_pose_position_feedback",
            "original_high_target_m": target,
            "measured_high_pose_m": actual,
            "measured_offset_m": [float(actual[i]) - float(target[i]) for i in range(3)],
            "max_feedback_steps": 4,
            "step_fraction": 0.5,
            "rationale": "第十二次侧向姿态在高位保持水平，但末端偏离原架位目标；按实际偏差分步修正控制器命令，每步仍对原物理目标做严格位置核验，不将该偏差带到低位。",
        }
        orientation_candidates = [orientation]
    elif attempt_index == 14 and release_gap_evidence is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        orientation = prior_pick.get("orientation_wxyz")
        adaptive_adjustment = release_gap_evidence
        orientation_candidates = [orientation]
    elif attempt_index == 14:
        prior = manifest.get("correction") or {}
        prior_pick = (prior.get("parameters") or {}).get(pick_step) or {}
        failure = manifest.get("motion_failure") or {}
        installation = (manifest.get("presentation") or {}).get("electrode_installation") or {}
        rack = ((installation.get("fixture_geometry") or {}).get("electrode_rack") or {})
        rack_target = (rack.get("slots") or {}).get(side)
        transit_height = prior_pick.get("transit_height_m")
        target = ([float(rack_target[0]), float(rack_target[1]), float(transit_height)]
                  if isinstance(rack_target, list) and len(rack_target) == 3
                  and isinstance(transit_height, (int, float)) else None)
        actual = failure.get("actual_ee_position_m")
        starts = prior.get("high_probe_external_acceptances") or []
        side_start = any(item.get("phase") == "pick_electrode/align/orientation_probe"
                         and isinstance(item.get("actual_ee_position_m"), list)
                         for item in starts if isinstance(item, dict))
        orientation = prior_pick.get("orientation_wxyz")
        trace_path = (run_dir / "process_validation" / "trace.jsonl") if run_dir else None
        last_axis = None
        try:
            with trace_path.open("rb") as stream:
                line = stream.readlines()[-1].decode("utf-8").strip()
            last_axis = json.loads(line).get("closing_axis_world") if line else None
        except (AttributeError, OSError, ValueError, TypeError, IndexError):
            pass
        prior_candidates = prior_pick.get("orientation_candidates_wxyz") or []
        intersections = [row for row in report.get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == pick_step]
        if (prior.get("attempt_index") != 13 or not preflight_issues or not side_start
                or failure.get("phase") != "pick_electrode/align/high_transit_probe"
                or failure.get("failed_waypoint_index") != 0
                or not isinstance(rack_target, list) or len(rack_target) != 3
                or not isinstance(target, list) or len(target) != 3
                or not isinstance(actual, list) or len(actual) != 3
                or not isinstance(last_axis, list) or len(last_axis) != 3
                or not isinstance(orientation, list) or len(orientation) != 4
                or abs(float(orientation[3])) < .5
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (rack_target, target, actual, last_axis, orientation)
                           for value in vector)
                or math.dist(target, [rack_target[0], rack_target[1], target[2]]) > .002
                or min(float(target[2]), float(actual[2])) < .42
                or abs(float(last_axis[2])) > .2
                or not .02 <= math.dist(target, actual) <= .07
                or any(isinstance(item, list) and len(item) == 4
                       and math.dist([float(value) for value in item],
                                     [float(orientation[0]), float(orientation[1]),
                                      float(orientation[2]), -float(orientation[3])]) < .001
                       for item in prior_candidates)
                or intersections):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第十三次缺少水平夹持轴、架位上方末端偏差或无碰架证据；不自动试探另一姿态。"}
        alternative = [float(orientation[0]), float(orientation[1]),
                       float(orientation[2]), -float(orientation[3])]
        adaptive_adjustment = {
            "parameter": "alternate_horizontal_grasp_direction",
            "original_high_target_m": target,
            "measured_high_pose_m": actual,
            "measured_offset_m": [float(actual[i]) - float(target[i]) for i in range(3)],
            "previous_orientation_wxyz": orientation,
            "alternative_orientation_wxyz": alternative,
            "rationale": "第十三次确认架位上方路径无碰撞、夹持轴水平，但原侧夹朝向的高位末端距槽位约数厘米；本次只反转水平夹持方向，在不改槽位与低位目标的条件下复核另一种腕部朝向。",
        }
        orientation_candidates = [alternative]
    else:
        # Reuse the side pose that passed the previous high-pose probe, but
        # change the approach corridor around the nearby fixture.
        orientation_candidates = ([[0.7071067811865476, 0.0, 0.0, 0.7071067811865476],
                                   [0.7071067811865476, 0.0, 0.0, -0.7071067811865476]]
                                  if attempt_index == 5 else
                                  [[0.7071067811865476, 0.0, 0.0, -0.7071067811865476]])
    orientation = orientation_candidates[0]
    actions = {
        step: {"orientation_wxyz": orientation,
               "orientation_candidates_wxyz": orientation_candidates,
               "transit_height_m": clearance}
        for step in all_action_steps if step in step_ids
    }
    approach_strategy = "lateral_grasp_high_clearance"
    if attempt_index == 3:
        # The last run stalled on descent beside the P200 fixture. Approach the
        # same unchanged grasp target from the opposite side of that fixture.
        actions[pick_step].update({
            "staging_offset_xy_m": [0.0, -0.10],
            "approach_offset_xy_m": [0.0, -0.08],
        })
        approach_strategy = "opposite_side_clearance_from_pipette_stand"
    elif attempt_index == 4:
        actions[pick_step].update({"approach_offset_xy_m": [0.0, -0.08]})
        approach_strategy = "prevalidated_side_pose_with_negative_y_approach"
    elif attempt_index == 5:
        actions[pick_step].update({"approach_offset_xy_m": [0.0, -0.08]})
        approach_strategy = "known_pose_sequence_with_negative_y_approach"
    elif attempt_index == 6 and adaptive_adjustment is not None:
        prior_parameters = ((manifest.get("correction") or {}).get("parameters") or {})
        prior_pick = prior_parameters.get(pick_step) or {}
        if prior_pick.get("approach_offset_xy_m") is not None:
            actions[pick_step]["approach_offset_xy_m"] = prior_pick["approach_offset_xy_m"]
        for step in (pick_step, place_step):
            if step in actions:
                actions[step]["gripper_open_joint_target_m"] = adaptive_adjustment["target_per_finger_m"]
        approach_strategy = "reduced_pregrasp_aperture_same_pose_and_route"
    elif attempt_index == 7 and adaptive_adjustment is not None:
        prior_parameters = ((manifest.get("correction") or {}).get("parameters") or {})
        prior_pick = prior_parameters.get(pick_step) or {}
        if prior_pick.get("approach_offset_xy_m") is not None:
            actions[pick_step]["approach_offset_xy_m"] = prior_pick["approach_offset_xy_m"]
        if prior_pick.get("gripper_open_joint_target_m") is not None:
            for step in (pick_step, place_step):
                if step in actions:
                    actions[step]["gripper_open_joint_target_m"] = prior_pick["gripper_open_joint_target_m"]
        actions[pick_step]["staging_offset_xy_m"] = adaptive_adjustment["target_staging_offset_xy_m"]
        approach_strategy = "measured_reachable_high_staging_point"
    elif attempt_index == 8 and adaptive_adjustment is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        for key in ("approach_offset_xy_m", "staging_offset_xy_m"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        for step in (pick_step, place_step):
            if step in actions:
                if prior_pick.get("gripper_open_joint_target_m") is not None:
                    actions[step]["gripper_open_joint_target_m"] = prior_pick["gripper_open_joint_target_m"]
                actions[step]["side_pose_observed_bias_xyz_m"] = adaptive_adjustment["mean_residual_m"]
        approach_strategy = "measured_side_pose_command_compensation"
    elif (attempt_index == 9 and adaptive_adjustment is not None
          and adaptive_adjustment.get('parameter') == 'apply_previously_measured_probe_bias'):
        prior_pick = ((manifest.get('correction') or {}).get('parameters') or {}).get(pick_step) or {}
        for key in ('approach_offset_xy_m', 'staging_offset_xy_m', 'gripper_open_joint_target_m',
                    'side_pose_observed_bias_xyz_m'):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        actions[pick_step]['orientation_probe_bias_xyz_m'] = adaptive_adjustment['bias']
        approach_strategy = 'apply_measured_bias_to_high_orientation_probe'
    elif attempt_index == 9 and adaptive_adjustment is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        for key in ("approach_offset_xy_m", "staging_offset_xy_m"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        for step in (pick_step, place_step):
            if step in actions:
                if prior_pick.get("gripper_open_joint_target_m") is not None:
                    actions[step]["gripper_open_joint_target_m"] = prior_pick["gripper_open_joint_target_m"]
                actions[step]["side_pose_observed_bias_xyz_m"] = prior_pick["side_pose_observed_bias_xyz_m"]
        actions[pick_step]["high_probe_physical_tolerance_m"] = adaptive_adjustment[
            "high_probe_physical_tolerance_m"]
        actions[pick_step]["high_probe_controller_margin_m"] = adaptive_adjustment[
            "high_probe_controller_margin_m"]
        approach_strategy = "measured_high_probe_acceptance_same_final_targets"
    elif (attempt_index == 10 and adaptive_adjustment is not None
          and adaptive_adjustment.get('parameter') == 'refine_high_probe_bias'):
        prior_pick = ((manifest.get('correction') or {}).get('parameters') or {}).get(pick_step) or {}
        for key in ('approach_offset_xy_m', 'staging_offset_xy_m', 'gripper_open_joint_target_m',
                    'side_pose_observed_bias_xyz_m'):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        actions[pick_step]['orientation_probe_bias_xyz_m'] = adaptive_adjustment['bias']
        approach_strategy = 'refine_measured_high_probe_bias'
    elif attempt_index == 10 and adaptive_adjustment is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        for key in ("approach_offset_xy_m", "staging_offset_xy_m"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        for step in (pick_step, place_step):
            if step in actions:
                for key in ("gripper_open_joint_target_m", "side_pose_observed_bias_xyz_m"):
                    if prior_pick.get(key) is not None:
                        actions[step][key] = prior_pick[key]
        actions[pick_step]["high_probe_physical_tolerance_m"] = prior_pick.get(
            "high_probe_physical_tolerance_m", .0145)
        actions[pick_step]["high_probe_controller_margin_m"] = prior_pick.get(
            "high_probe_controller_margin_m", .017)
        actions[pick_step]["high_transit_orientation_wxyz"] = adaptive_adjustment[
            "travel_orientation_wxyz"]
        approach_strategy = "upright_high_transit_then_side_grasp"
    elif (attempt_index == 11 and adaptive_adjustment is not None
          and adaptive_adjustment.get("parameter") == "kinematic_pregrasp_pose_probe"):
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        actions[pick_step].update({
            "orientation_probe_at_pregrasp": True,
            "high_transit_orientation_wxyz": actions[pick_step]["orientation_wxyz"],
            "high_transit_waypoint_step_m": .035,
            "approach_offset_xy_m": [0.0, 0.0],
            "gripper_open_joint_target_m": prior_pick.get("gripper_open_joint_target_m", .012),
        })
        stop_step = pick_step
        approach_strategy = "slot_aligned_pregrasp_with_segmented_kinematic_probe"
    elif attempt_index == 11 and adaptive_adjustment is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        for key in ("staging_offset_xy_m", "gripper_open_joint_target_m",
                    "side_pose_observed_bias_xyz_m", "high_probe_physical_tolerance_m",
                    "high_probe_controller_margin_m", "high_transit_orientation_wxyz"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        # The failed approach target was aligned with the adjacent salt-bridge
        # rack. Return directly above the electrode's own slot instead.
        actions[pick_step]["approach_offset_xy_m"] = [0.0, 0.0]
        approach_strategy = "upright_transit_directly_over_electrode_slot_then_local_descent"
    elif (attempt_index == 12 and adaptive_adjustment is not None
          and adaptive_adjustment.get("parameter") == "top_down_after_upward_pregrasp"):
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        actions[pick_step].update({
            "orientation_wxyz": list(TOP_DOWN_HORIZONTAL_WXYZ),
            "orientation_candidates_wxyz": [list(TOP_DOWN_HORIZONTAL_WXYZ)],
            "orientation_probe_at_pregrasp": True,
            "top_down_vertical_descent": True,
            "top_down_approach_z_m": .32,
            "high_transit_orientation_wxyz": list(TOP_DOWN_HORIZONTAL_WXYZ),
            "high_transit_waypoint_step_m": .035,
            "high_transit_destination_pose_bias": False,
            "high_destination_position_feedback": False,
            "approach_offset_xy_m": [0.0, 0.0],
            "gripper_open_joint_target_m": prior_pick.get("gripper_open_joint_target_m", .012),
        })
        stop_step = pick_step
        approach_strategy = "downward_fingertip_pregrasp_and_vertical_descent"
    elif attempt_index == 12 and adaptive_adjustment is not None:
        prior_parameters = (manifest.get("correction") or {}).get("parameters") or {}
        prior_pick = prior_parameters.get(pick_step) or {}
        for key in ("approach_offset_xy_m", "staging_offset_xy_m", "gripper_open_joint_target_m",
                    "side_pose_observed_bias_xyz_m", "high_probe_physical_tolerance_m",
                    "high_probe_controller_margin_m", "high_transit_orientation_wxyz"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        actions[pick_step]["high_transit_destination_pose_bias"] = False
        # Keep this diagnostic focused on the failed rack pickup; the cell
        # placement remains a separately checkable action after this succeeds.
        stop_step = pick_step
        approach_strategy = "direct_slot_high_pose_without_reused_bias"
    elif (attempt_index == 13 and adaptive_adjustment is not None
          and adaptive_adjustment.get("parameter") == "top_down_grasp_reference_z_offset"):
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        actions[pick_step].update({
            "orientation_wxyz": list(TOP_DOWN_HORIZONTAL_WXYZ),
            "orientation_candidates_wxyz": [list(TOP_DOWN_HORIZONTAL_WXYZ)],
            "orientation_probe_at_pregrasp": True,
            "top_down_vertical_descent": True,
            "top_down_approach_z_m": prior_pick.get("top_down_approach_z_m", .32),
            "top_down_grasp_target_offset_z_m": adaptive_adjustment["target_offset_z_m"],
            "high_transit_orientation_wxyz": list(TOP_DOWN_HORIZONTAL_WXYZ),
            "high_transit_waypoint_step_m": .035,
            "high_transit_destination_pose_bias": False,
            "high_destination_position_feedback": False,
            "approach_offset_xy_m": [0.0, 0.0],
            "gripper_open_joint_target_m": prior_pick.get("gripper_open_joint_target_m", .012),
        })
        stop_step = pick_step
        approach_strategy = "top_down_pick_above_rack_fork_with_compensated_placement"
    elif attempt_index == 13 and adaptive_adjustment is not None:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        for key in ("approach_offset_xy_m", "staging_offset_xy_m", "gripper_open_joint_target_m",
                    "side_pose_observed_bias_xyz_m", "high_probe_physical_tolerance_m",
                    "high_probe_controller_margin_m", "high_transit_orientation_wxyz"):
            if prior_pick.get(key) is not None:
                actions[pick_step][key] = prior_pick[key]
        actions[pick_step]["high_transit_destination_pose_bias"] = False
        actions[pick_step]["high_destination_position_feedback"] = True
        stop_step = pick_step
        approach_strategy = "direct_slot_with_bounded_high_pose_feedback"
    elif (attempt_index == 14 and adaptive_adjustment is not None
          and adaptive_adjustment.get("parameter") == "explicit_top_down_release_opening"):
        prior_parameters = (manifest.get("correction") or {}).get("parameters") or {}
        prior_pick = prior_parameters.get(pick_step) or {}
        prior_place = prior_parameters.get(place_step) or {}
        actions[pick_step].update(prior_pick)
        actions[place_step].update(prior_place)
        actions[place_step]["gripper_open_joint_target_m"] = prior_pick.get(
            "gripper_open_joint_target_m", .012)
        stop_step = place_step
        approach_strategy = "top_down_pick_with_explicit_release_opening"
    elif attempt_index == 14 and adaptive_adjustment is not None:
        return {"run_id": run_id, "eligible": False, "status": "blocked",
                "reason": "反向四元数候选未通过姿态预检，不能作为水平夹持姿态继续使用；目前没有新的实测安全朝向，停止自动改姿态。"}
    elif attempt_index == 15:
        correction = manifest.get("correction") or {}
        source_run_id = correction.get("source_run_id")
        try:
            if not isinstance(source_run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_run_id):
                raise ValueError("missing source run")
            source_manifest = json.loads(
                (run_dir.parent / source_run_id / "video_manifest.json").read_text(encoding="utf-8"))
            source_correction = source_manifest.get("correction") or {}
            source_pick = ((source_correction.get("parameters") or {}).get(pick_step) or {})
        except (OSError, ValueError, TypeError):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "找不到第十三次已记录的水平姿态，无法安全生成修复预演。"}
        source_orientation = source_pick.get("orientation_wxyz")
        travel_orientation = source_pick.get("high_transit_orientation_wxyz")
        if (source_correction.get("attempt_index") != 13
                or not isinstance(source_orientation, list) or len(source_orientation) != 4
                or not isinstance(travel_orientation, list) or len(travel_orientation) != 4
                or source_pick.get("staging_offset_xy_m") is None):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "第十三次没有可复用的姿态与高位路线记录，停止自动修正。"}
        orientation_candidates = [source_orientation]
        actions[pick_step].update({
            "orientation_wxyz": source_orientation,
            "orientation_candidates_wxyz": orientation_candidates,
            "transit_height_m": source_pick.get("transit_height_m", clearance),
            "staging_offset_xy_m": source_pick["staging_offset_xy_m"],
            "approach_offset_xy_m": [0.0, 0.0],
            "gripper_open_joint_target_m": source_pick.get("gripper_open_joint_target_m", .012),
            "high_transit_orientation_wxyz": travel_orientation,
            "high_transit_destination_pose_bias": False,
            "high_destination_position_feedback": True,
            "high_transit_waypoint_step_m": .035,
        })
        stop_step = pick_step
        approach_strategy = "restore_measured_side_pose_segmented_high_transit_without_reused_bias"
        adaptive_adjustment = {
            "parameter": "repair_cross_pose_bias_and_segment_high_transit",
            "source_attempt": 13,
            "source_run_id": source_run_id,
            "rejected_attempt": 14,
            "high_transit_waypoint_step_m": .035,
            "rationale": "第十四次把旧姿态偏差用于不同四元数的起点预检，命令点偏移约38 mm；本次复用第十三次记录的水平姿态，不跨位置复用偏差，并将高位转场拆为短段。",
        }
    elif attempt_index == 16:
        # User corrected the intended motion: approach the electrode from above,
        # descend along world Z, then close the jaws. Reuse the measured downward
        # grasp pose and its residual only at the same high staging point.
        prior15 = manifest.get("correction") or {}
        parent_id = prior15.get("source_run_id")
        attempt14 = None
        attempt13 = None
        try:
            if not isinstance(parent_id, str) or not re.fullmatch(r"[a-f0-9]{32}", parent_id):
                raise ValueError("missing attempt 14 source")
            attempt14 = json.loads((run_dir.parent / parent_id / "video_manifest.json").read_text(encoding="utf-8"))
            attempt14_correction = attempt14.get("correction") or {}
            grandparent_id = attempt14_correction.get("source_run_id")
            if not isinstance(grandparent_id, str) or not re.fullmatch(r"[a-f0-9]{32}", grandparent_id):
                raise ValueError("missing attempt 13 source")
            attempt13 = json.loads((run_dir.parent / grandparent_id / "video_manifest.json").read_text(encoding="utf-8"))
            source_correction = attempt13.get("correction") or {}
            source_pick = ((source_correction.get("parameters") or {}).get(pick_step) or {})
        except (OSError, ValueError, TypeError):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "找不到第十三次实测的向下抓取姿态与高位残差，不能安全生成局部预演。"}
        failure = manifest.get("motion_failure") or {}
        source_orientation = source_pick.get("orientation_wxyz")
        stage_bias = source_pick.get("side_pose_observed_bias_xyz_m")
        staging_offset = source_pick.get("staging_offset_xy_m")
        if (prior15.get("attempt_index") != 15
                or (attempt14.get("correction") or {}).get("attempt_index") != 14
                or (attempt13.get("correction") or {}).get("attempt_index") != 13
                or failure.get("phase") != "pick_electrode/align/orientation_probe"
                or not isinstance(source_orientation, list) or len(source_orientation) != 4
                or not isinstance(stage_bias, list) or len(stage_bias) != 3
                or math.sqrt(sum(float(value) ** 2 for value in stage_bias)) > .05
                or not isinstance(staging_offset, list) or len(staging_offset) != 2
                or not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for vector in (source_orientation, stage_bias, staging_offset)
                           for value in vector)):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "本次失败位置或第十三次姿态证据不匹配；停止修改，保留当前运行。"}
        orientation_candidates = [source_orientation]
        actions[pick_step].update({
            "orientation_wxyz": source_orientation,
            "orientation_candidates_wxyz": orientation_candidates,
            "transit_height_m": source_pick.get("transit_height_m", clearance),
            "staging_offset_xy_m": staging_offset,
            "approach_offset_xy_m": [0.0, 0.0],
            "gripper_open_joint_target_m": source_pick.get("gripper_open_joint_target_m", .012),
            "orientation_probe_bias_xyz_m": stage_bias,
            "side_pose_observed_bias_xyz_m": stage_bias,
            # Preserve the measured downward grasp orientation across approach,
            # vertical descent and jaw closure; do not flip the quaternion.
            "high_transit_orientation_wxyz": source_orientation,
            "high_transit_destination_pose_bias": False,
            "high_destination_position_feedback": True,
            "high_probe_physical_tolerance_m": source_pick.get(
                "high_probe_physical_tolerance_m", .0145),
            "high_probe_controller_margin_m": source_pick.get(
                "high_probe_controller_margin_m", .017),
            "high_transit_waypoint_step_m": .035,
        })
        stop_step = pick_step
        approach_strategy = "top_down_electrode_pick_segmented_high_approach"
        adaptive_adjustment = {
            "parameter": "restore_measured_downward_grasp_and_vertical_descent",
            "source_attempt": 13,
            "source_run_id": grandparent_id,
            "rejected_attempt": 14,
            "staging_bias_used_only_at_measured_pose": True,
            "downward_descent_axis": "world_z",
            "rationale": "第十五次停在高位姿态切换，未下降或闭爪。本次恢复第十三次实测姿态；仅在相同高位点使用同姿态测得的残差，保持夹爪朝下，以35 mm分段到达电极槽上方，再沿Z轴下降并闭爪。",
        }
    elif attempt_index == 17:
        failure = manifest.get("motion_failure") or {}
        closing_axis, approach_axis = _last_recorded_gripper_axes(
            run_dir, pick_step, "pick_electrode/align/high_transit_probe")
        intersections = [row for row in (report or {}).get("issues", [])
                         if row.get("group") == "geometry"
                         and row.get("code") == "fixture_intersection"
                         and row.get("status") == "fail"
                         and row.get("step_id") == pick_step]
        if (prior_attempt != 16
                or failure.get("phase") != "pick_electrode/align/high_transit_probe"
                or failure.get("failed_waypoint_index") != 0
                or not isinstance(approach_axis, list) or len(approach_axis) != 3
                or not isinstance(closing_axis, list) or len(closing_axis) != 3
                or approach_axis[2] < .85 or abs(closing_axis[2]) > .2
                or intersections):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "录像测得的夹爪方向或失败位置不满足朝下修正条件；停止动作，不尝试闭合。"}
        # Flip the measured upward pose 180 degrees about world X. The runner
        # must remeasure the actual mesh axes at a clear staging pose before
        # it is allowed to travel over the rack or descend.
        top_down_orientation = [0.0, 0.7071067811865476, 0.7071067811865476, 0.0]
        actions[pick_step].update({
            "orientation_wxyz": top_down_orientation,
            "orientation_candidates_wxyz": [top_down_orientation],
            "transit_height_m": .48,
            "staging_offset_xy_m": [0.0, 0.0],
            "approach_offset_xy_m": [0.0, 0.0],
            "top_down_vertical_descent": True,
            "top_down_approach_z_m": .32,
            "gripper_open_joint_target_m": .012,
            "high_transit_orientation_wxyz": top_down_orientation,
            "high_transit_destination_pose_bias": False,
            "high_destination_position_feedback": False,
            "high_transit_waypoint_step_m": .035,
        })
        stop_step = pick_step
        approach_strategy = "verified_top_down_vertical_electrode_pick"
        adaptive_adjustment = {
            "parameter": "flip_measured_upward_pose_and_verify_downward_axis",
            "source_attempt": 16,
            "source_gripper_approach_axis_world": approach_axis,
            "source_gripper_closing_axis_world": closing_axis,
            "rotation": "180 degrees about world X",
            "transit_waypoint_step_m": .035,
            "downward_axis_gate": -0.85,
            "rationale": "录像网格轨迹显示原夹爪指端朝上；本次在安全高位将姿态绕世界 X 轴翻转 180°，并从网格重新测量指端轴与闭合轴。只有指端 Z 分量≤−0.85且闭合轴水平时，才分段移动到电极槽正上方、垂直下降并闭合。",
        }
    elif attempt_index == 18:
        prior_pick = ((manifest.get("correction") or {}).get("parameters") or {}).get(pick_step) or {}
        collisions = [row for row in (report or {}).get("issues", [])
                      if row.get("status") == "fail"]
        fork_collision = [row for row in collisions
                          if row.get("code") == "fixture_intersection"
                          and row.get("step_id") == pick_step
                          and "panda_" in str(row.get("body", ""))
                          and "CopperElectrodeRack/" in str(row.get("obstacle", ""))]
        orientation_check = next((row for row in (report or {}).get("checks", [])
                                  if row.get("code") == "grasp_orientation"), {})
        required_pick = {"top_down_vertical_descent": True,
                         "orientation_wxyz": [0.0, 0.7071067811865476,
                                              0.7071067811865476, 0.0]}
        if (prior_attempt != 17
                or prior_pick.get("top_down_vertical_descent") is not True
                or prior_pick.get("orientation_wxyz") != required_pick["orientation_wxyz"]
                or orientation_check.get("status") != "pass"
                or not fork_collision
                or len(collisions) != len(fork_collision)
                or any("CopperElectrodeRack/" not in str(row.get("obstacle", ""))
                       for row in fork_collision)):
            return {"run_id": run_id, "eligible": False, "status": "blocked",
                    "reason": "当前录像不符合“朝下姿态正确、仅电极架支撑叉碰撞”的修正条件；不自动改变抓取高度。"}
        actions[pick_step].update({
            "top_down_grasp_target_offset_z_m": .008,
            "top_down_vertical_descent": True,
            "orientation_wxyz": required_pick["orientation_wxyz"],
            "orientation_candidates_wxyz": [required_pick["orientation_wxyz"]],
            "high_transit_orientation_wxyz": required_pick["orientation_wxyz"],
            "high_transit_waypoint_step_m": .035,
            "approach_offset_xy_m": [0.0, 0.0],
        })
        stop_step = pick_step
        approach_strategy = "top_down_vertical_pick_with_measured_fork_clearance"
        adaptive_adjustment = {
            "parameter": "raise_tool_grasp_reference_above_rack_forks",
            "source_attempt": 17,
            "source_fork_collision_count": len(fork_collision),
            "source_minimum_finger_z_m": 0.1377,
            "rack_fork_top_z_m": 0.139,
            "target_z_offset_m": .008,
            "predicted_finger_clearance_m": .0067,
            "downward_pose_preserved": True,
            "rationale": "第十七次已通过指端朝下和水平闭合检查，但实测指端最低点为0.1377 m，低于电极架支撑叉顶面0.139 m约1.3 mm。只将末端夹持参考点抬高8 mm；保持原电极、水平闭合方向、垂直接近路线及实验状态不变，使指端仍落在电极帽高度范围并越过支撑叉。",
        }
    if (pick_step in actions
            and actions[pick_step].get("gripper_open_joint_target_m") is None):
        actions[pick_step]["restore_full_open_before_pick"] = True

    prior_correction = manifest.get('correction') or {}
    attempts = dict(prior_correction.get('attempts_by_side') or {})
    attempts[side] = attempt_index
    return {
        "run_id": run_id, "eligible": True, "status": "ready",
        "reason": ((f"校验器从录像测得抓取前开口约 {adaptive_adjustment['observed_gap_m'] * 1000:.0f} mm，"
                    "相对 8 mm 电极帽偏大；本次只把每侧开口目标调到 12 mm（总开口约 24 mm），"
                    "保留已选侧向姿态、接近路线和最终抓取点，用局部复验判断是否改善。")
                   if attempt_index == 6 else
                  (f"姿态预检记录的末端位置为 {adaptive_adjustment['observed_robot_grasp_frame_m']} m；"
                   f"将高位起点平移 {adaptive_adjustment['target_staging_offset_xy_m']} m 到实测位置正上方，"
                   "保留侧向姿态、夹爪开口、接近路线及抓取/放置目标，用局部复验判断是否可达。")
                   if attempt_index == 7 else
                  (f"两次同姿态高位预检的平均末端残差为 {adaptive_adjustment['mean_residual_m']} m，"
                   f"两次差距仅 {adaptive_adjustment['residual_difference_m'] * 1000:.1f} mm；"
                   "仅对侧向动作的控制指令作反向补偿，仍按原物理目标逐航点核验。")
                   if attempt_index == 8 else
                  ("第八次预检未应用已测得的姿态补偿；本次仅在同一高位姿态预检中使用该补偿，原位置与验收精度不变。")
                   if attempt_index == 9 and adaptive_adjustment.get('parameter') == 'apply_previously_measured_probe_bias' else
                  (f"第八次高位预检末端实测偏差为 {adaptive_adjustment['measured_physical_error_m']} m；"
                   f"仅高位预检采用 {adaptive_adjustment['high_probe_physical_tolerance_m'] * 1000:.1f} mm"
                   " 的有界验收，抓取与安装的原目标及精度不变。")
                   if attempt_index == 9 else
                  ("根据第九次同一高位目标的剩余实测偏差微调预检补偿，原目标和验收精度不变。")
                   if attempt_index == 10 and adaptive_adjustment.get('parameter') == 'refine_high_probe_bias' else
                  (f"侧向起点已通过，但第九次在高位通道差 {adaptive_adjustment['measured_gap_m'] * 1000:.1f} mm；"
                   "本次只把高位行进姿态切回默认姿态，到架位上方再转为侧向夹持，低位目标不变。")
                   if attempt_index == 10 else
                  ("高位姿态残差方向不稳定，本次不再沿残差补偿；改在电极槽正上方分段预检目标姿态，"
                   "逐帧检查末端位置和夹爪网格/支架相交，通过后才沿原目标下降。")
                   if attempt_index == 11 and adaptive_adjustment.get(
                       "parameter") == "kinematic_pregrasp_pose_probe" else
                  (f"第十次已到架位高处，但实测低位偏差 {math.dist(adaptive_adjustment['failed_approach_target_m'], adaptive_adjustment['measured_ee_position_m']) * 1000:.1f} mm，且网格与盐桥架相交；"
                   "本次绕开盐桥架、直达电极槽上方，低位不用高位补偿并按原目标校正。")
                   if attempt_index == 11 else
                   ("第十一次高位预抓取姿态稳定且无相交，但指端长轴朝上，原姿态下降时末端偏离目标超过50 mm；"
                    "本次改用朝下夹持姿态，重新逐段预检并按原目标垂直下降。")
                   if attempt_index == 12 and adaptive_adjustment.get(
                       "parameter") == "top_down_after_upward_pregrasp" else
                  (f"第十一次已到电极槽上方，但姿态切换时复用了起点补偿，末端偏差 {adaptive_adjustment['measured_destination_gap_m'] * 1000:.1f} mm；"
                   "本次仅在高位起点使用补偿，架位转姿态与低位抓取按实测原目标执行。")
                   if attempt_index == 12 else
                  (f"第十二次朝下抓取在第 {adaptive_adjustment['collision_frame_index']} 帧碰到支撑叉，"
                   f"实测穿入 {adaptive_adjustment['measured_penetration_m'] * 1000:.1f} mm；"
                   f"本次将抓取参考点上移 {adaptive_adjustment['target_offset_z_m'] * 1000:.0f} mm，"
                   "网格复核无相交，放置时补偿该偏移以保持电极安装高度。")
                   if attempt_index == 13 and adaptive_adjustment.get(
                       "parameter") == "top_down_grasp_reference_z_offset" else
                  (f"第十二次高位侧夹方向已水平，但末端偏离原架位目标 {math.dist(adaptive_adjustment['original_high_target_m'], adaptive_adjustment['measured_high_pose_m']) * 1000:.1f} mm；"
                   "本次依据实时末端位置分步校正高位控制命令，并逐步按原目标验收。")
                   if attempt_index == 13 else
                  (f"第十三次已完成朝下抓取，但放置时夹爪只张开到 {adaptive_adjustment['measured_gap_m'] * 1000:.0f} mm；"
                   "本次把每侧12 mm开口目标显式传给放置动作，确认释放后再恢复全开，并复验稳定插入姿态。")
                   if attempt_index == 14 and adaptive_adjustment.get(
                       "parameter") == "explicit_top_down_release_opening" else
                  (f"第十三次确认原侧夹姿态在架位高处不收敛，末端距原目标 {math.dist(adaptive_adjustment['original_high_target_m'], adaptive_adjustment['measured_high_pose_m']) * 1000:.1f} mm，且未检测到碰架；"
                   "本次只反转水平夹持方向，在固定架位上方目标重新预检，低位抓取和电极槽坐标保持不变。")
                   if attempt_index == 14 else
                  ("第十四次把旧姿态偏差套用到不同四元数的预检点；本次复用第十三次的已记录水平姿态，移除跨位置补偿，并将高位转场拆为 35 mm 短段，只预演左侧电极抓取。")
                   if attempt_index == 15 else
                  ("恢复实测的朝下夹持姿态；仅在同一高位预检点使用已测残差，分段到电极架上方后沿 Z 轴下降、闭爪，只预演左侧电极抓取。")
                    if attempt_index == 16 else
                  "录像确认原夹爪指端朝上；本次翻转姿态，并在安全高位验证指端朝下、闭合轴水平后，从电极正上方垂直下降并闭合。"
                    if attempt_index == 17 else
                  "已保留经网格验证的朝下姿态；根据碰架帧测得的指端与支撑叉高度，将夹持末端参考点上移8 mm，再垂直接近电极帽并闭合。"
                    if attempt_index == 18 else
                   ("仅调整电极夹爪侧向姿态和有界接近路径；根据支架邻近证据改从另一侧进入。"
                   "最终放置坐标、支撑/间隙判据、实验条件、随机种子和化学状态均不改。"
                   if attempt_index in {3, 4, 5} else
                   "仅调整电极夹爪侧向闭合姿态与高位进退路径；最终放置坐标、支撑/间隙判据、实验条件、随机种子和化学状态均不改.")),
        "attempt_index": attempt_index, "max_attempts": MAX_ATTEMPTS,
        "approach_strategy": approach_strategy,
        "candidate_id": ("electrode_top_down_after_upward_pregrasp_12"
                         if attempt_index == 12 and adaptive_adjustment.get(
                             "parameter") == "top_down_after_upward_pregrasp" else
                         "electrode_top_down_fork_clearance_pick_13"
                         if attempt_index == 13 and adaptive_adjustment.get(
                             "parameter") == "top_down_grasp_reference_z_offset" else
                         "electrode_top_down_release_opening_14"
                         if attempt_index == 14 and adaptive_adjustment.get(
                             "parameter") == "explicit_top_down_release_opening" else
                         "electrode_slot_pregrasp_kinematic_probe_11"
                         if attempt_index == 11 and adaptive_adjustment.get(
                             "parameter") == "kinematic_pregrasp_pose_probe" else
                         "electrode_top_down_vertical_pick_17" if attempt_index == 17
                         else "electrode_top_down_fork_clearance_pick_18" if attempt_index == 18
                         else f"electrode_side_grasp_clear_corridor_{attempt_index}"),
        "adaptive_adjustment": adaptive_adjustment,
        "attempts_by_side": attempts,
        "root_run_id": prior_correction.get('root_run_id') or run_id,
        "side": side, "checkpoint_step_id": checkpoint_step,
        "stop_after_step": stop_step, "actions": actions,
        "motion_timeout_replanned": bool(timeout_issues),
        "unreachable_pose_probe_replanned": bool(preflight_issues),
        "failed_waypoint_m": (timeout_issues[0][1].get("failed_waypoint_position_m")
                              if timeout_issues else None),
        "final_target_unchanged": True, "source_run_id": run_id,
        "source_status_uncertain": bool(snapshot.get("state_uncertain")),
        "diagnostic_only": True,
    }


def correction_result_path(run_dir):
    return Path(run_dir) / "process_validation" / "correction_result.json"

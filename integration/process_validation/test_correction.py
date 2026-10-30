"""Focused tests for evidence-driven electrode action correction."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integration"))
from process_validation.correction import build_correction_plan, MAX_ATTEMPTS


RUN_ID = "a" * 32
PICK = "ui_pick_left_copper_electrode"
PLACE = "ui_place_left_copper_electrode"
ORIENTATION = [0.7071067811865476, 0.0, 0.0, -0.7071067811865476]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def fixture(root, include_gap=True):
    (root / "process_validation").mkdir()
    target = [0.288, 0.0, 0.145]
    actual = [0.2785, 0.1289, 0.2717]
    timeout = {
        "phase": "pick_electrode/align",
        "failed_waypoint_index": 2,
        "failed_waypoint_position_m": target,
        "planned_waypoints_m": [[0.288, -0.08, 0.32], [0.288, 0.0, 0.32], target],
        "actual_ee_position_m": actual,
        "requested_final_position_m": target,
    }
    archive = {
        "snapshot": {
            "run_id": RUN_ID,
            "plan": [{"step_id": "ui_mix_pair"}, {"step_id": PICK}, {"step_id": PLACE}],
            "state_uncertain": True,
        },
        "events": [{"type": "step.completed", "step_id": "ui_mix_pair"}],
    }
    report = {
        "run_id": RUN_ID,
        "issues": [{
            "status": "fail", "code": "execution_failed", "step_id": PICK,
            "message": "机械臂动作超时: " + json.dumps(timeout),
        }],
    }
    manifest = {
        "run_id": RUN_ID,
        "correction": {
            "attempt_index": 5, "side": "left", "attempts_by_side": {"left": 5},
            "parameters": {PICK: {"approach_offset_xy_m": [0.0, -0.08]}},
            "pose_preflight": {"pick_electrode/align": {"selected_orientation_wxyz": ORIENTATION}},
        },
        "frames": ([{"step_id": PICK, "gripper_gap_m": 0.08}] if include_gap else []),
    }
    # A P200 support fork lies between the commanded target and stalled EE
    # location. It supplies a route clue, not proof of contact.
    corners = [
        [0.2855, 0.07, 0.147], [0.2855, 0.09, 0.147],
        [0.2855, 0.07, 0.149], [0.2855, 0.09, 0.149],
        [0.2885, 0.07, 0.147], [0.2885, 0.09, 0.147],
        [0.2885, 0.07, 0.149], [0.2885, 0.09, 0.149],
    ]
    geometry = {"run_id": RUN_ID, "obstacles": [{
        "path": "/World/ElectrochemTransferProxy/PipetteStand_P200/ForkR",
        "corners": corners,
    }]}
    save(root / "run.json", archive)
    save(root / "process_validation" / "report.json", report)
    save(root / "video_manifest.json", manifest)
    save(root / "process_validation" / "geometry.json", geometry)


class AdaptiveCorrectionTests(unittest.TestCase):
    def test_wide_open_timeout_selects_aperture_only_adjustment(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            fixture(run_dir)
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan["eligible"], plan["reason"])
        self.assertEqual(plan["attempt_index"], 6)
        self.assertEqual(plan["approach_strategy"], "reduced_pregrasp_aperture_same_pose_and_route")
        self.assertEqual(plan["actions"][PICK]["gripper_open_joint_target_m"], 0.012)
        self.assertEqual(plan["actions"][PLACE]["gripper_open_joint_target_m"], 0.012)
        self.assertEqual(plan["actions"][PICK]["approach_offset_xy_m"], [0.0, -0.08])
        self.assertTrue(plan["final_target_unchanged"])

    def test_timeout_without_measured_aperture_is_not_changed_blindly(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            fixture(run_dir, include_gap=False)
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("夹爪开口测量", plan["reason"])

    def _make_attempt_six_preflight_failure(self, run_dir, include_pose=True):
        fixture(run_dir)
        manifest_path = run_dir / "video_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pick_parameters = {
            "orientation_wxyz": ORIENTATION,
            "orientation_candidates_wxyz": [ORIENTATION],
            "transit_height_m": 0.48,
            "approach_offset_xy_m": [0.0, -0.08],
            "gripper_open_joint_target_m": 0.012,
        }
        manifest["correction"].update({
            "attempt_index": 6,
            "attempts_by_side": {"left": 6},
            "parameters": {PICK: pick_parameters, PLACE: {
                "orientation_wxyz": ORIENTATION,
                "orientation_candidates_wxyz": [ORIENTATION],
                "transit_height_m": 0.48,
                "gripper_open_joint_target_m": 0.012,
            }},
            "pose_preflight": {"pick_electrode/align": {
                "staging_position_m": [0.4, 0.0, 0.48],
                "selected_orientation_wxyz": None,
                "rejected_candidates": [{
                    "orientation_wxyz": ORIENTATION,
                    "reason": "机械臂动作超时: {\"phase\": \"pick_electrode/align/orientation_probe\", "
                              "\"actual_ee_position_m\": [0.427464, -0.0128, 0.45916]",
                }],
                "preflight_status": "no_reachable_side_grasp_pose",
            }},
        })
        manifest["frames"] = ([{
            "step_id": PICK, "phase": "pick_electrode/align/orientation_probe",
            "robot_grasp_frame_m": [0.427464, -0.0128, 0.45916],
        }] if include_pose else [])
        manifest["motion_failure"] = {
            "phase": "pick_electrode/align/orientation_probe",
            "failed_waypoint_index": 0,
            "failed_waypoint_position_m": [0.4, 0.0, 0.48],
            "actual_ee_position_m": [0.427464, -0.0128, 0.45916],
            "failed_waypoint_error_m": [0.027464, -0.0128, -0.02084],
        }
        save(manifest_path, manifest)
        save(run_dir / "process_validation" / "report.json", {
            "run_id": RUN_ID,
            "issues": [{"status": "fail", "code": "execution_failed", "step_id": PICK,
                        "message": "安全高位姿态预检未找到可达的侧向夹持角度: [...]"}],
        })

    def test_unreachable_preflight_uses_last_measured_pose_for_next_staging_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            self._make_attempt_six_preflight_failure(run_dir)
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan["eligible"], plan["reason"])
        self.assertEqual(plan["attempt_index"], 7)
        self.assertEqual(plan["approach_strategy"], "measured_reachable_high_staging_point")
        self.assertAlmostEqual(plan["actions"][PICK]["staging_offset_xy_m"][0], 0.027464)
        self.assertAlmostEqual(plan["actions"][PICK]["staging_offset_xy_m"][1], -0.0128)
        self.assertEqual(plan["actions"][PICK]["gripper_open_joint_target_m"], 0.012)
        self.assertEqual(plan["actions"][PICK]["approach_offset_xy_m"], [0.0, -0.08])
        self.assertTrue(plan["final_target_unchanged"])

    def test_unreachable_preflight_without_observed_pose_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            self._make_attempt_six_preflight_failure(run_dir, include_pose=False)
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("实测位置", plan["reason"])

    def _make_two_preflight_residuals(self, root, second_residual=None):
        run_dir = root / RUN_ID
        run_dir.mkdir()
        self._make_attempt_six_preflight_failure(run_dir)
        source_id = "b" * 32
        parent_dir = root / source_id
        parent_dir.mkdir()
        first_residual = [0.02746, -0.0128, -0.02084]
        second_residual = second_residual or [0.02894, -0.01307, -0.02186]
        first_target = [0.4, 0.0, 0.48]
        second_target = [0.42746, -0.0128, 0.48]
        parent_manifest = {
            "run_id": source_id,
            "correction": {"attempt_index": 6, "parameters": {
                PICK: {"orientation_wxyz": ORIENTATION}}},
            "motion_failure": {"phase": "pick_electrode/align/orientation_probe",
                               "failed_waypoint_index": 0,
                               "failed_waypoint_position_m": first_target,
                               "actual_ee_position_m": [first_target[i] + first_residual[i]
                                                        for i in range(3)],
                               "failed_waypoint_error_m": first_residual},
        }
        save(parent_dir / "video_manifest.json", parent_manifest)
        manifest_path = run_dir / "video_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["correction"].update({
            "attempt_index": 7, "attempts_by_side": {"left": 7},
            "source_run_id": source_id,
        })
        manifest["correction"]["parameters"][PICK]["staging_offset_xy_m"] = [0.02746, -0.0128]
        manifest["motion_failure"] = {
            "phase": "pick_electrode/align/orientation_probe",
            "failed_waypoint_index": 0,
            "failed_waypoint_position_m": second_target,
            "actual_ee_position_m": [second_target[i] + second_residual[i]
                                     for i in range(3)],
            "failed_waypoint_error_m": second_residual,
        }
        save(manifest_path, manifest)
        return run_dir

    def test_stable_side_pose_residual_produces_bounded_command_compensation(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_two_preflight_residuals(Path(temporary))
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan["eligible"], plan["reason"])
        self.assertEqual(plan["attempt_index"], 8)
        self.assertEqual(plan["approach_strategy"], "measured_side_pose_command_compensation")
        for actual, expected in zip(plan["actions"][PICK]["side_pose_observed_bias_xyz_m"],
                                    [0.0282, -0.012935, -0.02135]):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertEqual(plan["actions"][PICK]["gripper_open_joint_target_m"], 0.012)
        self.assertEqual(plan["actions"][PICK]["staging_offset_xy_m"], [0.02746, -0.0128])
        self.assertTrue(plan["final_target_unchanged"])

    def test_unstable_side_pose_residual_refuses_command_compensation(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_two_preflight_residuals(
                Path(temporary), second_residual=[-0.015, 0.01, -0.02])
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("变化过大", plan["reason"])

    def _make_attempt_eight_high_probe_failure(self, root, clear=True):
        run_dir = self._make_two_preflight_residuals(root)
        manifest_path = run_dir / "video_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["correction"].update({
            "attempt_index": 8, "attempts_by_side": {"left": 8},
        })
        bias = [0.028202, -0.012934, -0.021348]
        manifest["correction"]["parameters"][PICK]["side_pose_observed_bias_xyz_m"] = bias
        manifest["correction"]["parameters"][PLACE]["side_pose_observed_bias_xyz_m"] = bias
        target = [0.427464, -0.012800, 0.480000]
        actual = [0.438859, -0.013049, 0.472640]
        manifest["motion_failure"] = {
            "phase": "pick_electrode/align/orientation_probe",
            "failed_waypoint_index": 0,
            "failed_waypoint_position_m": target,
            "actual_ee_position_m": actual,
            "failed_waypoint_error_m": [actual[i] - target[i] for i in range(3)],
            "controller_waypoint_position_m": [target[i] - bias[i] for i in range(3)],
        }
        save(manifest_path, manifest)
        save(run_dir / "process_validation" / "report.json", {
            "run_id": RUN_ID,
            "checks": [{"group": "geometry", "code": "sampled_intersections",
                        "status": "pass" if clear else "fail"}],
            "issues": [{"status": "fail", "code": "execution_failed", "step_id": PICK,
                        "message": "安全高位姿态预检未找到可达的侧向夹持角度: [...]"}],
        })
        return run_dir

    def test_near_high_probe_may_adjust_high_probe_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_attempt_eight_high_probe_failure(Path(temporary))
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan["eligible"], plan["reason"])
        self.assertEqual(plan["attempt_index"], 9)
        self.assertEqual(plan["approach_strategy"],
                         "measured_high_probe_acceptance_same_final_targets")
        self.assertLessEqual(plan["actions"][PICK]["high_probe_physical_tolerance_m"], .015)
        self.assertGreater(plan["actions"][PICK]["high_probe_physical_tolerance_m"], .011)
        self.assertLessEqual(plan["actions"][PICK]["high_probe_controller_margin_m"], .025)
        self.assertNotIn("high_probe_physical_tolerance_m", plan["actions"][PLACE])
        self.assertTrue(plan["final_target_unchanged"])

    def test_high_probe_adjustment_requires_clear_sampled_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_attempt_eight_high_probe_failure(Path(temporary), clear=False)
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "blocked")

    def test_unstable_attempt_ten_uses_slot_aligned_pregrasp_without_bias(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_attempt_eight_high_probe_failure(Path(temporary))
            manifest_path = run_dir / "video_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bias = [0.02746, -0.0128, -0.02084]
            target = [0.4, 0.0, 0.48]
            actual = [0.460546, -0.01336, 0.450347]
            error = [actual[i] - target[i] for i in range(3)]
            command = [target[i] - bias[i] for i in range(3)]
            manifest["correction"].update({"attempt_index": 10,
                                           "attempts_by_side": {"left": 10}})
            manifest["correction"]["parameters"][PICK].update({
                "orientation_probe_bias_xyz_m": bias,
                "side_pose_observed_bias_xyz_m": bias,
                "orientation_wxyz": ORIENTATION,
                "gripper_open_joint_target_m": .012,
            })
            manifest["motion_failure"] = {
                "phase": "pick_electrode/align/orientation_probe",
                "failed_waypoint_index": 0,
                "failed_waypoint_position_m": target,
                "actual_ee_position_m": actual,
                "failed_waypoint_error_m": error,
                "controller_waypoint_position_m": command,
            }
            save(manifest_path, manifest)
            report = json.loads((run_dir / "process_validation/report.json").read_text(encoding="utf-8"))
            report["issues"] = [{"status": "fail", "code": "execution_failed", "step_id": PICK,
                                 "message": "安全高位姿态预检未找到可达的侧向夹持角度: measured"}]
            report["checks"] = [{"group": "geometry", "code": "sampled_intersections", "status": "pass"}]
            save(run_dir / "process_validation/report.json", report)
            plan = build_correction_plan(run_dir)

        self.assertTrue(plan["eligible"], plan["reason"])
        self.assertEqual(plan["attempt_index"], 11)
        self.assertTrue(plan["actions"][PICK]["orientation_probe_at_pregrasp"])
        self.assertNotIn("orientation_probe_bias_xyz_m", plan["actions"][PICK])
        self.assertNotIn("side_pose_observed_bias_xyz_m", plan["actions"][PICK])
        self.assertEqual(plan["stop_after_step"], PICK)
        self.assertTrue(plan["final_target_unchanged"])

    def test_repeated_high_transit_failure_stops_with_specific_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_attempt_eight_high_probe_failure(Path(temporary))
            manifest_path = run_dir / "video_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["correction"]["attempt_index"] = MAX_ATTEMPTS
            manifest["motion_failure"].update({
                "phase": "pick_electrode/align/high_transit_probe",
                "failed_waypoint_position_m": [.288, -.08, .48],
                "actual_ee_position_m": [.319257, -.100314, .458071],
            })
            save(manifest_path, manifest)
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "limit_reached")
        self.assertIn("高位通道", plan["reason"])
        self.assertIn("重新规划", plan["reason"])

    def test_unregistered_high_transit_failure_below_cap_does_not_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_attempt_eight_high_probe_failure(Path(temporary))
            manifest_path = run_dir / "video_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["correction"]["attempt_index"] = 9
            manifest["motion_failure"].update({
                "phase": "pick_electrode/align/high_transit_probe",
                "failed_waypoint_position_m": [.288, -.08, .48],
                "actual_ee_position_m": [.319257, -.100314, .458071],
            })
            save(manifest_path, manifest)
            plan = build_correction_plan(run_dir)

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()

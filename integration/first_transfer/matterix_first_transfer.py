"""Franka motion + semantic pipetting proxy for the first transfer process.

Requires the verified Matterix/Isaac cloud runtime. Tool attachment and fluid
transfer are semantic, visual proxies; no contact attachment or CFD is claimed.
Each successful receipt follows observed robot motion and a verified USD ledger.
"""

import argparse
import json
import math
import os
import sys
from importlib.metadata import version as package_version
from pathlib import Path

from preview_frames import optional_publisher, synchronized_state
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.scene_trace import SceneTrace
from process_validation.service import start_automatic_audit
from process_validation.continuation import build_continuation_plan, execution_fingerprint
from process_validation.repairs.profile import validate_repair_profile
from transfer_session import TransferSession
from resume_checkpoint import restore_confirmed_prefix
from robot_checkpoint import (make_robot_checkpoint, select_robot_checkpoint,
                               verify_restored_robot)
from motion_constraints import (load_policy,PhysicalConstraintError,MotionMonitor,
                                CorrectionBudget,HeldOrientation,prepare_waypoints,
                                require_self_collision)
from runtime_scene_constraints import RuntimeSceneConstraints
from joint_aware_ik import action_type as joint_aware_action_type, plan_live_route
from lead_route_search import search_approach
from video_presentation import Presentation, project_point
from scene_presentation import soften_dark_robot_materials
from labware_presentation import (read_asset_geometry, decorate_vessel, update_contours,
                                  optical_material)
from liquid_presentation import (create_liquid_material, update_liquid_material,
                                 create_liquid_visual, update_liquid_visual,
                                 configure_liquid_lighting)
from tip_disposal_visuals import (DisposalVisuals, WASTE_XY, WASTE_RIM_Z,
                                  WASTE_INNER_HALF, falling_center)
from desktop_fixtures import create_tip_rack
from microcell_labware import CELLS, create_microcells
from action_camera import focus_key
from mixer_presentation import (add_ika_asset, convert_glb_to_usd,
                                create_stirring_visuals, update_stirring_visuals,
                                mixing_camera_spec)
from electrode_visuals import (COPPER_RACK_XY, COPPER_RACK_CAP_CENTER_Z_M,
                               COPPER_RACK_SLOT_X_OFFSET_M,
                               PIPETTE_STAND_XY, PIPETTE_STAND_XY_BY_ID, PIPETTE_DOCK_EE_Z_M,
                               ELECTRODE_CAP_M, ELECTRODE_PLATE_M,
                               CELL_AXIS_X_OFFSET_M, ELECTRODE_INSERTION_M,
                               create_electrode_supports, create_electrode,
                               set_electrode_center, electrode_stack_geometry)
from pipette_visuals import (PIPETTE_BODY_RADIUS_M, PIPETTE_BODY_HEIGHT_M,
                             PIPETTE_BODY_CENTER_Z_M, PIPETTE_GRIP_RADIUS_M,
                             PIPETTE_GRIP_HEIGHT_M, PIPETTE_GRIP_CENTER_Z_M,
                             PIPETTE_LOWER_COLLAR_RADIUS_M,
                             PIPETTE_LOWER_COLLAR_HEIGHT_M,
                             PIPETTE_LOWER_COLLAR_CENTER_Z_M,
                             PIPETTE_NOZZLE_RADIUS_M, PIPETTE_NOZZLE_HEIGHT_M,
                             PIPETTE_NOZZLE_CENTER_Z_M,
                             PIPETTE_TIP_CENTER_Z_M, PIPETTE_TIP_LENGTH_M,
                             PIPETTE_TIP_RADIUS_M, PIPETTE_TIP_APEX_OFFSET_M,
                             PIPETTE_NOZZLE_BOTTOM_OFFSET_M,
                             pipette_grip_aligned, pipette_visual_profile)
from salt_bridge_visuals import (create_salt_bridge, set_bridge_center, bridge_geometry,
                                 BRIDGE_RACK_CENTER_M)
from bench_layout import VERSION as BENCH_LAYOUT_VERSION
from measurement_visuals import (create_measurement_visuals, set_lead_center, lead_geometry,
                                 cable_frame_evidence)
from measurement_session import LEAD_MAP
from matterix_ik_target_compensation import (compensated_target,
                                               residual_for_runtime)
from high_probe_gate import high_probe_physically_reached
from repair_execution import (ee_route_waypoints, expand_route, pose_axis_evidence,
                              open_gap_verified, resumed_gripper_open_target, latest_approach_axis)

parser = argparse.ArgumentParser()
parser.add_argument("--live-preview", action="store_true", help="Publish bounded JPEG/confirmed-state packets alongside the full cloud MP4")
parser.add_argument("--engine-root", type=Path, required=True)
parser.add_argument("--conditions", type=Path, required=True)
parser.add_argument("--output-root", type=Path, default=Path("/mnt/data/electrochem_twin/runs"))
parser.add_argument("--matterix-root", default=os.environ.get(
    "MATTERIX_PATH", "/home/ubuntu/Documents/Kit/apps/Matterix"))
parser.add_argument("--ika-asset", type=Path, default=Path(os.environ.get(
    "CHEMLAB_IKA_GLB",
    str(Path(__file__).resolve().parents[1] / "reference" / "ChemLab-Assets" /
        "assets/expansion_batch05/t4_measure/ika_plate_rctdigital/model.glb"))))
parser.add_argument("--ika-usd-cache", type=Path, default=Path(
    "/mnt/data/electrochem_twin/assets/cache/ika_plate_rctdigital/model.usd"))
parser.add_argument("--electrode-asset", type=Path, default=Path(os.environ.get(
    "CHEMLAB_ELECTRASYN_GLB",
    str(Path(__file__).resolve().parents[1] / "reference" / "ChemLab-Assets" /
        "assets/expansion_batch03/t5_automation/ika_electrasyn2/model.glb"))))
parser.add_argument("--electrode-usd-cache", type=Path, default=Path(
    "/mnt/data/electrochem_twin/assets/cache/ika_electrasyn2/model.usd"))
parser.add_argument("--seed", type=int, default=7)
parser.add_argument("--run-id", default=None)
parser.add_argument("--resume-archive", type=Path, default=None,
                    help="Rebuild the chemical ledger from a prior run archive without replaying its physical actions")
parser.add_argument("--resume-after-step", default=None,
                    help="Last successful step to restore before running a focused diagnostic segment")
parser.add_argument("--stop-after-step", default=None,
                    help="Stop this diagnostic run after the named step")
parser.add_argument("--stop-after-actions", type=int, default=None,
                    help="Stop a fresh diagnostic run after this many successful actions")
parser.add_argument("--correction-profile", type=Path, default=None,
                    help="Bounded pose/path overrides for an isolated validation-triggered electrode retry")
parser.add_argument("--continuation-profile", type=Path, default=None,
                    help="Checked correction checkpoint from which to execute the remaining workflow")
parser.add_argument("--include-left-mix", action="store_true",
                    help="Include the legacy left-solution IKA mixing action")
parser.add_argument("--include-paired-mix", action="store_true",
                    help="Prepare both solutions and mix both cells in one simultaneous action")
parser.add_argument("--include-left-electrode", action="store_true",
                    help="After paired mixing, park the pipette and install one copper electrode in the left cell")
parser.add_argument("--include-right-electrode", action="store_true",
                    help="After the left electrode, install a second copper electrode in the right cell")
parser.add_argument('--include-salt-bridge', action='store_true',
                    help='After both electrodes, pick and seat the prefilled microcell salt bridge; no voltage measurement')
parser.add_argument('--include-measurement', action='store_true',
                    help='Extend both-electrode/bridge assembly with lead handling and ideal virtual OCP')
parser.add_argument('--measurement-asset', type=Path, default=Path(__file__).resolve().parents[1] /
                    'reference/ChemLab-Assets/assets/basic40_batch02/t4_measure/gamry_ref620/model.glb')
parser.add_argument('--measurement-usd-cache', type=Path,
                    default=Path('/mnt/data/electrochem_twin/assets/cache/gamry_ref620/model.usd'))
parser.add_argument("--video-width", type=int, default=2560)
parser.add_argument("--video-height", type=int, default=1440)
parser.add_argument("--video-crf", type=int, default=16,
                    help="H.264 quality target; lower values retain more image detail")
parser.add_argument("--video-preset", choices=("medium", "slow", "slower"), default="slow",
                    help="FFmpeg x264 compression preset; slower presets trade encoding time for smaller files at similar quality")
parser.add_argument("--video-font", default=None)
parser.add_argument('--preview-only', action='store_true',
                    help='Render frozen visual previews without executing any actions')
parser.add_argument('--preview-snapshot', type=Path, default=None,
                    help='Existing run archive supplying the preview quantities')
parser.add_argument('--mixing-camera-audit', action='store_true',
                    help='Frozen actual-renderer camera candidates, no action receipts or video')
args = parser.parse_args()
if args.preview_only and args.preview_snapshot is None:
    parser.error('--preview-only needs an existing --preview-snapshot')
if bool(args.resume_archive) != bool(args.resume_after_step):
    parser.error('--resume-archive and --resume-after-step must be provided together')
if args.resume_archive and not args.stop_after_step and not args.continuation_profile:
    parser.error('恢复档案只用于有限步骤的局部诊断，必须指定 --stop-after-step')
if args.continuation_profile and (not args.resume_archive or args.stop_after_step
        or args.stop_after_actions is not None or args.correction_profile or args.preview_only):
    parser.error('续跑必须使用独立检查点，不能与局部终点、预览或另一修正方案混用')
if args.stop_after_step and not args.resume_archive:
    parser.error('--stop-after-step is only valid for a focused diagnostic run')
if args.stop_after_actions is not None:
    if args.stop_after_actions < 1:
        parser.error('--stop-after-actions must be a positive integer')
    if args.resume_archive or args.stop_after_step:
        parser.error('--stop-after-actions cannot be combined with archive-resume options')
if args.video_width < 1280 or args.video_height < 720 or args.video_width % 16 or args.video_height % 2:
    parser.error('Video size must be at least 1280x720; width multiple of 16, height even')
if not 0 <= args.video_crf <= 51:
    parser.error('--video-crf must be between 0 and 51')
os.environ["MATTERIX_PATH"] = args.matterix_root
resume_archive = json.loads(args.resume_archive.read_text(encoding="utf-8")) if args.resume_archive else None
continuation_profile = None
if args.continuation_profile:
    continuation_profile = json.loads(args.continuation_profile.read_text(encoding="utf-8"))
    current_plan = build_continuation_plan(args.resume_archive.parent)
    if (not current_plan.get('eligible') or
            continuation_profile.get('checkpoint_signature') != current_plan.get('checkpoint_signature') or
            continuation_profile.get('expected_revision') != current_plan.get('expected_revision') or
            args.resume_after_step != current_plan.get('checkpoint_step_id') or
            continuation_profile.get('correction_profile') != current_plan.get('correction_profile')):
        parser.error(current_plan.get('reason') if not current_plan.get('eligible')
                     else '续跑检查点已变化，请重新读取检查结果')
correction_profile = (json.loads(args.correction_profile.read_text(encoding="utf-8"))
                      if args.correction_profile else
                      continuation_profile.get('correction_profile') if continuation_profile else None)
if correction_profile:
    if not resume_archive or not (args.stop_after_step or continuation_profile):
        parser.error("--correction-profile is only valid for a resumed, bounded diagnostic segment")
    source_id = resume_archive.get("snapshot", {}).get("run_id")
    try:
        validate_repair_profile(correction_profile, source_id)
        if not continuation_profile:
            from process_validation.repairs.dependency_plan import validate_dependency_profile
            validate_dependency_profile(correction_profile, resume_archive, args.resume_after_step, args.stop_after_step)
    except ValueError as exc:
        parser.error(str(exc))
conditions = (resume_archive.get("snapshot", {}).get("conditions") if resume_archive
              else json.loads(args.conditions.read_text(encoding="utf-8")))
if not isinstance(conditions, dict):
    parser.error('恢复档案中没有有效的实验条件')
session_seed = resume_archive.get("seed", args.seed) if resume_archive else args.seed
session = TransferSession(args.engine_root, conditions,
                          seed=session_seed, executor="matterix", run_id=args.run_id,
                          include_discard_tip=True, include_left_water=True,
                          include_left_mix=args.include_left_mix,
                          include_paired_mix=args.include_paired_mix,
                          include_left_electrode=args.include_left_electrode,
                          include_right_electrode=args.include_right_electrode,
                          include_salt_bridge=args.include_salt_bridge,
                          include_measurement=args.include_measurement)
# The confirmed archive includes the geometry-derived working capacity in its
# state snapshot. Restore that same deterministic geometry before rebuilding
# the accepted prefix, otherwise continuation rejects an otherwise identical
# chemical state solely because the fresh session still has an unset capacity.
for cell_id, cell in CELLS.items():
    cell.at(None, float(session.conditions[cell_id.replace('_half_cell', '_volume_ml')])*1000)
    session.state.containers[cell_id].capacity_l = cell.working_capacity_ul*1e-6
if resume_archive:
    # Reconstruct only the accepted chemical ledger. Prior robot actions and
    # video frames are never re-executed during a focused segment diagnostic.
    try:
        restore_confirmed_prefix(session, resume_archive, args.resume_after_step,
                                 require_terminal=bool(continuation_profile),
                                 candidate_id=(correction_profile or {}).get("candidate_id"),
                                 branch_type=("continuation" if continuation_profile else
                                              "local_correction" if correction_profile else
                                              "local_diagnostic"),
                                 repair_contract=(correction_profile or {}).get("repair_contract"))
    except ValueError as exc:
        parser.error(str(exc))
    remaining_step_ids = [action.step_id for action in session.actions[session.index:]]
    if args.stop_after_step and args.stop_after_step not in remaining_step_ids:
        parser.error("指定的局部诊断终点不在恢复位置之后的剩余动作中")
    if not remaining_step_ids:
        parser.error('检查点之后已经没有待执行步骤')
    session.scope = ("continued_virtual_concentration_cell" if continuation_profile
                     else "targeted_segment_diagnostic")
elif args.stop_after_actions is not None:
    if args.stop_after_actions >= len(session.actions):
        parser.error('--stop-after-actions must stop before the full plan completes')
    session.scope = "initial_state_prefix_diagnostic"
run_dir = args.output_root / session.run_id
run_dir.mkdir(parents=True, exist_ok=False)

def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)

# Publish the initial confirmed ledger before the expensive simulator startup.
if not args.preview_only:
    atomic_json(run_dir / "run.json", session.archive())

from simulator_runtime import launch_headless_simulator, selected_gpu_id, isolate_runtime_recorders
app = launch_headless_simulator()

import gymnasium as gym
import imageio.v2 as imageio
import numpy as np
import torch
from PIL import Image, ImageDraw
from pxr import Gf, Sdf, UsdGeom, UsdShade
import omni.kit.app
import omni.replicator.core as rep
import omni.usd
import matterix_tasks
from matterix_assets.labware.beakers import BEAKER_500ML_INST_CFG
from matterix_sm import StateMachine
from matterix_sm.primitive_actions import MoveToPoseCfg
from matterix_sm.robot_action_spaces import FRANKA_IK_ACTION_SPACE
from pregrasp_planner import orientation_waypoints, pregrasp_pose, orientation_error_deg
from isaaclab_tasks.utils.parse_cfg import parse_env_cfg

preview = None
process_trace = None
preview_terminal = "failed"
env = rgb = product = writer = None
close_rgb = close_product = None
frames = steps = 0
command = None
changed = False
dispense_progress = None
current_label = "initializing"
completed_command = None
stirring_started_s = None
current_mix_duration_s = 10.0
pipette_parked = True
held_pipette_id = None
held_electrode_side = None
held_electrode_grasp_offset_z_m = 0.0
held_bridge = False
held_bridge_grasp_offset_z_m = 0.0
active_route_orientation_wxyz = None
runtime_policy = load_policy()
runtime_scene = None
runtime_motion_context = None
motion_monitor = MotionMonitor(runtime_policy)
held_pose_constraint = HeldOrientation(runtime_policy)
salt_bridge_connected = bool(session.state.cell_setup.get('salt_bridge_connected'))
bridge_visual = None
measurement_visual = None
held_lead_side = None
lead_installed = {side: bool(session.state.cell_setup.get(side+'_leads_connected'))
                  for side in ('left', 'right')}
electrode_installed = {"left": False, "right": False}
electrode_visuals = {}
gripper_command = 1.0
pipette_grasp_gap_m = None
if resume_archive:
    restored_events = [event for event in session.events if event.get('type') == 'step.completed']
    if restored_events:
        last_restored = restored_events[-1]
        completed_command = {**last_restored['payload'], 'action': last_restored['action'],
                             'step_id': last_restored['step_id']}
    restored_steps = {event.get('step_id') for event in session.events
                      if event.get('type') == 'step.completed'}
    pipette_parked = session.state.cell_setup.get('pipette_parked', True)
    held_pipette_id = session.state.cell_setup.get('active_pipette_id')
    if ('ui_pick_left_copper_electrode' in restored_steps
            and 'ui_place_left_copper_electrode' not in restored_steps):
        held_electrode_side = 'left'
    elif ('ui_pick_right_copper_electrode' in restored_steps
          and 'ui_place_right_copper_electrode' not in restored_steps):
        held_electrode_side = 'right'
    electrode_installed = {
        'left': 'ui_place_left_copper_electrode' in restored_steps,
        'right': 'ui_place_right_copper_electrode' in restored_steps}
    gripper_command = (-1.0 if held_electrode_side is not None or held_pipette_id is not None
                       else 1.0)
    held_bridge = session.state.cell_setup.get('held_component') == 'salt_bridge'
    if session.state.cell_setup.get('held_component') == 'measurement_lead':
        held_lead_side = session.state.cell_setup['held_component_target'].split('_')[0]
        gripper_command = -1.0
    if held_bridge:
        gripper_command = -1.0
output = run_dir / "first_transfer.mp4"
partial = run_dir / "first_transfer.partial.mp4"
manifest = {"run_id": session.run_id, "model": "semantic_pipetting_stirring_and_paired_electrode_visual_adapter_v5",
            "tool_attachment": "gripper-aligned_kinematic_visual_attachment",
            "fluid_model": "semantic_volume_ledger_with_visual_surfaces",
            "physical_fluid_validation": False, "frames": [], "status": "starting"}
manifest['execution_fingerprint'] = execution_fingerprint()
manifest['recording_scope'] = ('remaining_workflow_segment' if continuation_profile else
                               'local_correction_segment' if correction_profile else
                               'diagnostic_segment' if args.resume_archive or args.stop_after_actions else
                               'full_workflow')
manifest['full_workflow_video'] = not bool(args.resume_archive or args.stop_after_actions)
if correction_profile:
    manifest["correction"] = {
        "source_run_id": correction_profile["source_run_id"],
        "attempt_index": correction_profile.get("attempt_index"),
        "candidate_id": correction_profile.get("candidate_id"),
        "strategy_family": correction_profile.get("strategy_family"),
        "fault_step_ids": correction_profile.get("fault_step_ids", []),
        "side": correction_profile.get("side"),
        "root_run_id": correction_profile.get("root_run_id") or correction_profile['source_run_id'],
        "attempts_by_side": correction_profile.get("attempts_by_side", {}),
        "repair_contract": correction_profile.get("repair_contract"),
        "pipeline_stages": correction_profile.get("pipeline_stages", []),
        "dependency_plan": correction_profile.get('dependency_plan'),
        "proposal_fingerprint": correction_profile.get('proposal_fingerprint'),
        "scope": correction_profile.get("scope", "evidence_bounded_local_repair_segment"),
        "final_target_unchanged": True,
        "chemical_conditions_unchanged": True,
        "error_state_reused_from_confirmed_prefix": True,
        "parameters": correction_profile.get("actions"),
        "routes": correction_profile.get("routes", {}),
    }
    for stage in manifest["correction"]["pipeline_stages"]:
        if stage.get("stage") == "execute":
            stage["status"] = "running"
if continuation_profile:
    manifest['continuation'] = {
        'source_run_id': continuation_profile['source_run_id'],
        'root_run_id': continuation_profile.get('root_run_id'),
        'repair_contract': continuation_profile.get('correction_profile', {}).get('repair_contract'),
        'restored_after_step': args.resume_after_step,
        'source_revision': continuation_profile['expected_revision'],
        'checkpoint_signature': continuation_profile['checkpoint_signature'],
        'prior_actions_replayed': False, 'executed_step_ids': [],
        'chemical_ledger_reconstructed_from_successful_receipts': True,
        'full_workflow_video': False}
manifest['presentation'] = {'version': 'measurement_leads_and_ideal_ocp_v21' if args.include_measurement else 'tidy_bench_tool_bank_v20',
                            'bench_layout_version': BENCH_LAYOUT_VERSION,
                            'resolution': [args.video_width, args.video_height],
                            'views': ['persistent_overview', 'synchronized_independent_macro_camera'],
                            'confirmed_quantity_source': 'accepted_executor_receipt',
                            'liquid_geometry': 'illustrative_not_calibrated'}
runtime_isaaclab_version = package_version('isaaclab')
runtime_residual_m = residual_for_runtime(runtime_isaaclab_version)
manifest['presentation']['runtime'] = {
    'isaacsim': package_version('isaacsim'), 'isaaclab': runtime_isaaclab_version,
    'matterix_root': args.matterix_root}
manifest['presentation']['simulator_pose_compensation'] = {
    'runtime_scope': 'pinned_matterix_isaaclab_2_2_only',
    'active': any(runtime_residual_m),
    'applied_residual_m': list(runtime_residual_m),
    'isaaclab_version': runtime_isaaclab_version,
    'applied_to': 'commanded_microcell_dispense_pose_only',
    'physical_robot_calibration': False,
    'source_failed_run_id': '29c594422aff4248a07a96f35dd7e91c'}
if resume_archive and not continuation_profile:
    manifest['diagnostic'] = {
        'scope': 'targeted_segment_only',
        'source_run_id': resume_archive.get('snapshot', {}).get('run_id'),
        'restored_after_step': args.resume_after_step,
        'stop_after_step': args.stop_after_step,
        'prior_actions_replayed': False,
        'chemical_ledger_reconstructed_from_successful_receipts': True,
        'executed_step_ids': []}
elif args.stop_after_actions is not None:
    manifest['diagnostic'] = {
        'scope': 'initial_state_prefix_only',
        'source_run_id': None,
        'starting_state': 'fresh_initial_state',
        'maximum_successful_actions': args.stop_after_actions,
        'prior_actions_replayed': False,
        'executed_step_ids': []}


def persist():
    if runtime_scene is not None:
        manifest['physical_constraints']['runtime_checks']=runtime_scene.check_count
        manifest['physical_constraints']['planned_path_checks']=runtime_scene.planned_path_checks
        manifest['physical_constraints']['path_mesh_refinements']=runtime_scene.path_mesh_refinements
        manifest['physical_constraints']['path_box_false_positives_cleared']=runtime_scene.path_box_false_positives_cleared
    atomic_json(run_dir / "run.json", session.archive())
    atomic_json(run_dir / "video_manifest.json", manifest)
    if session.state.cell_setup.get('voltage_measurement_completed'):
        atomic_json(run_dir / 'measurement_result.json', {
            'run_id':session.run_id, 'source_checkpoint_run_id':(resume_archive or {}).get('snapshot',{}).get('run_id'),
            'conditions':session.conditions,'seed':session.seed,
            'simulation_time_s':session.simulation_time_s,'state_revision':session.revision,
            'voltage':session.snapshot()['voltage'],'state':session.snapshot()['state'],
            'error_model_realized':session.snapshot()['error_model_realized'],
            'conservation':session.snapshot()['conservation'],
            'data_origin':'simulated','physical_measurement_performed':False})


try:
    cfg = parse_env_cfg("Matterix-Test-Beaker-Lift-Franka-v1", device=f"cuda:{selected_gpu_id()}", num_envs=1)
    isolate_runtime_recorders(cfg, run_dir)
    robot_cfg=cfg.articulated_assets.get('robot')
    articulation_props=getattr(getattr(robot_cfg,'spawn',None),'articulation_props',None)
    require_self_collision(getattr(articulation_props,'enabled_self_collisions',None))
    robot_cfg.action_terms['arm_action'].class_type=joint_aware_action_type()
    # The stock-only clip fit inside Matterix's 30 s default episode horizon;
    # this continuous stock+water workflow does not. Prevent time-limit resets
    # from breaking a physically/ledger-continuous run.
    cfg.episode_length_s = 600.0
    cfg.events.randomize_beaker_position = None
    if session.includes_left_electrode:
        # Binary-gripper -1 maps to a symmetric 10 mm cap opening. The grasp
        # remains kinematic; this is clearance geometry, not force validation.
        cfg.articulated_assets["robot"].action_terms["gripper_action"].close_command_expr = {
            "panda_finger_.*": .005}
    # Asset-based enlarged presentation: operation poses follow these same bounds.
    vessel_scale = 1.4
    cfg.objects["beaker"] = BEAKER_500ML_INST_CFG(pos=(0.55, 0.14, 0.05), scale=(vessel_scale,) * 3)
    # Keep the dedicated water vessel within the arm's verified working area;
    # this position is separate from the stock beaker, receiving beaker and rack.
    water_source_pose = (0.40, 0.14, 0.05)
    cfg.objects["water_source"] = BEAKER_500ML_INST_CFG(pos=water_source_pose, scale=(vessel_scale,) * 3)
    vessel_shape = read_asset_geometry(cfg.objects['beaker'].usd_path, vessel_scale)
    env = gym.make("Matterix-Test-Beaker-Lift-Franka-v1", cfg=cfg).unwrapped
    if correction_profile:
        pick_spec = correction_profile.get("actions", {}).get(
            f"ui_pick_{correction_profile.get('side', 'left')}_copper_electrode", {})
        open_joint_target = pick_spec.get("gripper_open_joint_target_m")
        if open_joint_target is not None:
            gripper_term = env.action_manager.get_term("Actions_robot_gripper_action")
            open_command = getattr(gripper_term, "_open_command", None)
            if open_command is None or open_command.numel() != 2:
                raise RuntimeError("当前夹爪动作项不支持经过核验的逐指开口目标，停止修正")
            open_command[:] = float(open_joint_target)
    obs, _ = env.reset()
    if resume_archive:
        source_manifest_path = args.resume_archive.with_name('video_manifest.json')
        source_manifest = json.loads(source_manifest_path.read_text(encoding='utf-8'))
        if held_electrode_side is not None:
            source_pick = source_manifest.get('presentation', {}).get(
                'electrode_installation', {}).get('picks', {}).get(held_electrode_side, {})
            held_electrode_grasp_offset_z_m = float(source_pick.get('grasp_target_offset_z_m', 0.0))
        if held_bridge:
            source_pick = source_manifest.get('presentation', {}).get('salt_bridge', {}).get('pick', {})
            held_bridge_grasp_offset_z_m = float(source_pick.get('grasp_reference_offset_z_m', 0.0))
        restore_metadata = manifest['continuation'] if continuation_profile else manifest['diagnostic']
        robot = env.scene['robot']
        checkpoint = select_robot_checkpoint(
            resume_archive, source_manifest, args.resume_after_step,
            manifest['presentation']['runtime'], list(robot.joint_names))
        q = torch.tensor([checkpoint['robot']['joint_positions']], device=env.device,
                         dtype=robot.data.joint_pos.dtype)
        robot.write_joint_state_to_sim(q, torch.zeros_like(q))
        robot.set_joint_position_target(q)
        env.scene.write_data_to_sim()
        env.sim.forward()
        env.scene.update(float(env.step_dt))
        obs = env.observation_manager.compute()
        fingers = obs['articulations']['robot__gripper_pos'][0].detach().cpu().flatten().tolist()
        actual_robot = {
            'joint_positions': robot.data.joint_pos[0].detach().cpu().tolist(),
            'end_effector_position_m': obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist(),
            'end_effector_orientation_wxyz': obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist(),
            'gripper_gap_m': sum(abs(float(value)) for value in fingers),
        }
        verification = verify_restored_robot(checkpoint, actual_robot)
        restore_metadata['robot_joint_pose_restored_from_checkpoint'] = True
        restore_metadata['robot_joint_pose_restored_from_source_end'] = checkpoint['source'] == 'source_end_state'
        restore_metadata['robot_checkpoint_restore'] = {
            **checkpoint, 'actual_robot': actual_robot, 'verification': verification}
    stage = omni.usd.get_context().get_stage()
    dt = float(env.step_dt)
    if resume_archive and session.simulation_time_s is not None:
        # The new Matterix scene starts its physics clock at zero, while the
        # restored ledger retains the source run's logical simulation time.
        steps = int(round(session.simulation_time_s / dt))
    fps = 1 / dt
    root = UsdGeom.Xform.Define(stage, "/World/ElectrochemTransferProxy")
    root.GetPrim().CreateAttribute("run_id", Sdf.ValueTypeNames.String).Set(session.run_id)
    root.GetPrim().CreateAttribute("model_scope", Sdf.ValueTypeNames.String).Set("semantic_proxy_no_contact_physics_or_CFD")
    # Fixed receiving cells use explicit cavity dimensions, shared by the
    # liquid depth, tool poses, and reserved electrode/salt-bridge interfaces.
    micro_glass = optical_material(stage, str(root.GetPath())+'/MicrocellGlass',
                                   color=(1., 1., 1.), depth=.02, thin_walled=False)
    manifest['presentation']['microcells'] = create_microcells(stage, root.GetPath(), micro_glass)
    ika_usd_path = convert_glb_to_usd(app, args.ika_asset, args.ika_usd_cache)
    manifest['presentation']['ika_magnetic_stirrer'] = add_ika_asset(
        stage, str(root.GetPath()), ika_usd_path, plate_center=(.57,-.16))
    manifest['presentation']['ika_magnetic_stirrer'].update({
        'catalog_asset_id': 'CHEM-008',
        'asset_license': 'CC BY-NC 4.0; attribution retained in asset source folder',
        'source_glb_sha256': '1424d89d59f0b37788f75f262a87130dfdfdd245bd227dad25789ccf03bbda04',
        'not_validated': ['5 mm bar behavior from upstream IKA asset',
                          'magnetic coupling through this microcell floor/adapter gap',
                          'device RPM or mixing performance']})
    stirring_visuals = {}
    for cell_id, cell in CELLS.items():
        cell_geometry = cell.at(
            None, session.state.containers[cell_id].volume_l*1_000_000)
        stirring_visuals[cell_id] = create_stirring_visuals(
            stage, str(root.GetPath()), cell_id, cell.xy,
            cell_geometry['floor_z'])
    manifest['presentation']['micro_stir_bar'] = {
        'dimensions_m': [0.005, 0.002], 'custom_visual_proxy': True,
        'in_pool_inner_diameter_m': .012,
        'preplaced_cells': list(stirring_visuals),
        'positioned_at_each_cell_bottom': True,
        'magnetic_coupling_validated': False, 'upstream_behavior_validated': False}
    manifest['presentation']['mixing_visual'] = {
        'method': 'both_preplaced_bars_rotate_together_with_volume_preserving_animated_free_surfaces',
        'fluid_solver': None, 'animation_rate_is_calibrated_rpm': False,
        'efficiency_authority': 'existing electrochemistry mixing error model only',
        'paired_efficiency_sampling': 'shared_run_latent_plus_independent_per_cell_deviation',
        'paired_efficiency_correlation': session.errors.paired_mixing_correlation,
        'paired_efficiency_correlation_status': 'provisional_uncalibrated_assumption',
        'chemical_state_committed_after_successful_executor_receipt': True,
        'liquid_volume_change_ul': 0.0,
        'detail_view': 'long_lens_elevated_rear_oblique_avoiding_forks',
        'camera_retained_through_stop_and_confirmation': True,
        'bar_animation': '10s_ramp_up_steady_brake_not_calibrated_rpm'}
    if session.includes_left_electrode:
        if not args.electrode_asset.is_file():
            raise FileNotFoundError(f"缺少已复核的 CHEM-016 电极几何：{args.electrode_asset}")
        electrode_usd = convert_glb_to_usd(app, args.electrode_asset, args.electrode_usd_cache)
        cells_by_side = {'left': CELLS['left_half_cell'],
                         'right': CELLS['right_half_cell']}
        fixtures = create_electrode_supports(
            stage, str(root.GetPath()), cells_by_side,
            CELLS['left_half_cell'].support_z)
        electrode_visuals = {
            side: create_electrode(stage, str(root.GetPath()), electrode_usd,
                                   cell, side=side)
            for side, cell in cells_by_side.items()
            if side == 'left' or session.includes_right_electrode}
        electrode_geometry_metadata = {
            side: {key: value for key, value in visual.items()
                   if key != 'root_translate_op'}
            for side, visual in electrode_visuals.items()}
        manifest['presentation']['electrode_installation'] = {
            'catalog_asset_id': 'CHEM-016', 'model': 'IKA ElectraSyn 2.0 package',
            'asset_license': 'CC BY-NC 4.0; attribution retained in asset source folder',
            'source_glb_sha256': '586CE722148D88A7C97C4DC7D74D7F4EB6457B3B6C2C49ED33E1A14A9B33BDB9',
            'asset_revision': 'ac95c1bcb717a71e3036993ef9102ed20fa028c0',
            'reused_components': {'electrode_cap': 'CHEM-016 geometry, reduced to 8 mm diameter x 12 mm high',
                                  'working_electrode': 'CHEM-016 graphite plate geometry, resized to 0.4 x 3 x 42 mm and recolored copper',
                                  'cap_grip': 'CHEM-016 grasp knob geometry, resized as visual cap detail'},
            'source_electrode_material': 'graphite; changed copper appearance is an adaptation, not source copper geometry',
            'original_package_not_imported': True,
            'fixture_geometry': fixtures, 'electrode_geometry': electrode_geometry_metadata,
            'plate_dimensions_m': list(ELECTRODE_PLATE_M), 'cap_dimensions_m': list(ELECTRODE_CAP_M),
            'axes_xy_m': {side: [cell.xy[0] + CELL_AXIS_X_OFFSET_M, cell.xy[1]]
                          for side, cell in cells_by_side.items()},
            'insertion_span_from_tip_to_cap_m': ELECTRODE_INSERTION_M,
            'immersion_length_set_by_actual_liquid_height': True,
            'installed_sides_planned': ['left', 'right'] if session.includes_right_electrode else ['left'],
            'placements': {},
            'source_asset_behavior_invoked': False,
            'contact_grasp_validated': False, 'copper_electrode_hardware_validated': False,
            'electrochemical_connection_or_voltage_enabled': False}
    if session.includes_measurement:
        measurement_usd = convert_glb_to_usd(app, args.measurement_asset, args.measurement_usd_cache)
        measurement_visual = create_measurement_visuals(stage, str(root.GetPath()), measurement_usd,
                                                         CELLS, electrode_visuals)
        manifest['presentation']['measurement'] = measurement_visual['metadata']
        for side, clip in measurement_visual['clips'].items():
            if lead_installed[side]:
                set_lead_center(clip, clip['target_center_m'], closed=True)
    if session.includes_salt_bridge:
        bridge_visual = create_salt_bridge(stage, str(root.GetPath()), electrode_usd, CELLS)
        set_bridge_center(bridge_visual, bridge_visual['target_center_m'] if salt_bridge_connected
                          else bridge_visual['rack_center_m'])
        manifest['presentation']['salt_bridge'] = {
            key: value for key, value in bridge_visual.items() if key != 'translate_op'}
        manifest['presentation']['salt_bridge'].update(
            electrolyte=session.conditions['salt_bridge_type'],
            electrolyte_concentration_m=session.conditions['salt_bridge_concentration_m'],
            cell_liquid_added_ul=0., ion_transport_simulated=False)
    manifest['presentation']['robot_materials'] = soften_dark_robot_materials(stage, str(root.GetPath()))
    vessel_assets = {'copper_stock': 'beaker', 'water': 'water_source'}
    vessel_contours = {}
    for name, asset_name in vessel_assets.items():
        prim_path = env.scene[asset_name].root_physx_view.prim_paths[0]
        vessel_contours[name] = decorate_vessel(stage, str(root.GetPath()) + '/' + name, prim_path)
    manifest['presentation']['vessels'] = {
        'asset_path': cfg.objects['beaker'].usd_path, 'scale': vessel_scale,
        'asset_bounds_min_m': vessel_shape.lower, 'asset_bounds_max_m': vessel_shape.upper,
        'liquid_radius_source': 'inscribed_asset_bounds', 'pose_source': 'rigid_object_root_pos_w',
        'graduations': 'no_added_contours_or_marks', 'nominal_capacity_validated': False,
        'material': 'RTX_MDL_OmniGlass_transmission'}
    manifest['presentation']['liquid_rendering'] = 'watertight_refractive_MDL_volume_not_CFD'
    manifest['presentation']['water_source'] = {'asset_path': cfg.objects['water_source'].usd_path,
        'pose_m': list(water_source_pose), 'visual_fill_scaled': False,
        'material': 'clear_water_illustration_not_calibrated'}
    manifest['presentation']['episode_horizon_s'] = cfg.episode_length_s
    manifest['presentation']['stock_fill_display'] = {
        'user_requested_initial_fraction': .45,
        'scope': 'source_only_visual_height_enlargement',
        'ledger_inventory_unchanged': True, 'receiving_vessel_volume_scale': 1.0}
    vessel_geometry = {}

    def vessel_geometry_at(name, volume_ul):
        if name in CELLS:
            return CELLS[name].at(None, volume_ul,
                                  electrode_inserted=(electrode_installed['left'] if name == 'left_half_cell'
                                                      else electrode_installed['right']),
                                  bridge_inserted=salt_bridge_connected)
        position = env.scene[vessel_assets[name]].data.root_pos_w[0].detach().cpu().tolist()
        return vessel_shape.at(position, volume_ul)

    def cylinder(name, pos, radius, height, color, parent=None):
        parent_path = parent.GetPath() if parent is not None else root.GetPath()
        obj = UsdGeom.Cylinder.Define(stage, parent_path.AppendChild(name))
        obj.CreateRadiusAttr(radius)
        obj.CreateHeightAttr(height)
        obj.CreateAxisAttr("Z")
        obj.CreateDisplayColorAttr([Gf.Vec3f(*color)])
        obj.AddTranslateOp().Set(Gf.Vec3d(*pos))
        return obj

    if resume_archive and session.state.tip.loaded:
        first_pipette_action = next((action for action in reversed(session.actions[:session.index])
                                     if action.kind == 'load_tip'), None)
    else:
        first_pipette_action = next((action for action in session.actions[session.index:]
                                     if action.kind == 'load_tip'), None)
    current_pipette_id = (first_pipette_action.pipette_id if first_pipette_action
                          and first_pipette_action.pipette_id else 'gilson_pipetman_p1000')
    current_pipette_profile = pipette_visual_profile(current_pipette_id)
    current_tip_length_m = float(current_pipette_profile['tip_length_m'])
    current_tip_radius_m = PIPETTE_TIP_RADIUS_M
    tool_apex_offset_m = -PIPETTE_NOZZLE_BOTTOM_OFFSET_M + current_tip_length_m

    def make_pipette_visual(pipette_id):
        profile = pipette_visual_profile(pipette_id)
        short_id = 'P200' if pipette_id.endswith('_p200') else 'P1000'
        pipette_root = UsdGeom.Xform.Define(stage, root.GetPath().AppendChild(f'PipetteVisual_{short_id}'))
        root_translate = UsdGeom.Xformable(pipette_root).AddTranslateOp()
        (stand_x, stand_y) = PIPETTE_STAND_XY_BY_ID[pipette_id]
        root_translate.Set(Gf.Vec3d(stand_x, stand_y, PIPETTE_DOCK_EE_Z_M))
        body = cylinder("tool_body", (0.0, 0.0, profile['body_center_z_m']),
                        profile['body_radius_m'], profile['body_height_m'],
                        profile['body_color_rgb'], parent=pipette_root)
        cylinder('tool_grip_collar', (0.0, 0.0, PIPETTE_GRIP_CENTER_Z_M),
                 PIPETTE_GRIP_RADIUS_M, PIPETTE_GRIP_HEIGHT_M,
                 (.25, .28, .32), parent=pipette_root)
        cylinder('tool_collar', (0.0, 0.0, PIPETTE_LOWER_COLLAR_CENTER_Z_M),
                 PIPETTE_LOWER_COLLAR_RADIUS_M, PIPETTE_LOWER_COLLAR_HEIGHT_M,
                 (.8, .83, .87), parent=pipette_root)
        cylinder('tool_nozzle', (0.0, 0.0, PIPETTE_NOZZLE_CENTER_Z_M),
                 PIPETTE_NOZZLE_RADIUS_M, PIPETTE_NOZZLE_HEIGHT_M,
                 (.72, .75, .78), parent=pipette_root)
        carrier = UsdGeom.Xform.Define(stage, pipette_root.GetPath().AppendChild('tip_carrier'))
        carrier_translate = carrier.AddTranslateOp()
        carrier_translate.Set(Gf.Vec3d(0.0, 0.0,
            PIPETTE_NOZZLE_BOTTOM_OFFSET_M-float(profile['tip_length_m'])/2))
        tip = UsdGeom.Cone.Define(stage, carrier.GetPath().AppendChild("tip"))
        tip.CreateRadiusAttr(PIPETTE_TIP_RADIUS_M)
        tip.CreateHeightAttr(float(profile['tip_length_m']))
        tip.CreateAxisAttr("Z")
        tip.CreateDisplayColorAttr([Gf.Vec3f(0.85, 0.9, 0.95)])
        tip.AddRotateXOp().Set(180.0)
        tip.CreateVisibilityAttr("invisible")
        chamber = cylinder("tip_liquid", (0.0, 0.0, 0.0), 0.002, 0.0001,
                           (0.0, 0.65, 0.95), parent=pipette_root)
        chamber_translate = UsdGeom.Xformable(chamber).GetOrderedXformOps()[0]
        chamber.CreateVisibilityAttr("invisible")
        pipette_root.GetPrim().CreateAttribute('pipette_id', Sdf.ValueTypeNames.String).Set(pipette_id)
        pipette_root.GetPrim().CreateAttribute('tip_id', Sdf.ValueTypeNames.String).Set(profile['tip_id'])
        pipette_root.GetPrim().CreateAttribute('tip_attached', Sdf.ValueTypeNames.Bool).Set(False)
        return {'root': pipette_root, 'translate': root_translate, 'profile': profile,
                'body': body, 'tip': tip, 'carrier_translate': carrier_translate,
                'chamber': chamber, 'chamber_translate': chamber_translate,
                'attached': pipette_root.GetPrim().GetAttribute('tip_attached')}

    pipette_visuals = {pid: make_pipette_visual(pid) for pid in
                       ('gilson_pipetman_p200', 'gilson_pipetman_p1000')}
    liquid_material = create_liquid_material(stage, str(root.GetPath())+'/CuSO4Liquid',
        session.config['containers']['copper_stock']['concentrations_m'].get('Cu', 0.0),
        reference_distance_m=.04)
    water_material = create_liquid_material(stage, str(root.GetPath())+'/ClearWaterLiquid',
                                            0., reference_distance_m=.04)
    target_material = create_liquid_material(stage, str(root.GetPath())+'/ReceivingLiquid', 0.)
    right_target_material = create_liquid_material(stage, str(root.GetPath())+'/RightReceivingLiquid', 0.)
    stock_liquid = create_liquid_visual(stage, str(root.GetPath())+'/stock_liquid', liquid_material)
    water_liquid = create_liquid_visual(stage, str(root.GetPath())+'/water_liquid', water_material)
    target_liquid = create_liquid_visual(stage, str(root.GetPath())+'/target_liquid', target_material)
    right_target_liquid = create_liquid_visual(stage, str(root.GetPath())+'/right_target_liquid', right_target_material)
    active_liquid_material = liquid_material
    for tool_visual in pipette_visuals.values():
        UsdShade.MaterialBindingAPI.Apply(tool_visual['chamber'].GetPrim()).Bind(active_liquid_material)
    drops = []
    for index in range(3):
        drop = UsdGeom.Sphere.Define(stage, root.GetPath().AppendChild(f'dispense_drop_{index}'))
        drop.CreateRadiusAttr(.0013)
        drop.AddTranslateOp()
        drop.CreateVisibilityAttr('invisible')
        UsdShade.MaterialBindingAPI.Apply(drop.GetPrim()).Bind(active_liquid_material)
        drops.append(drop)
    # Matterix bench surface is z=0 m; derive the fixture contact stack in its
    # own presentation module so the base and tips sit on their support planes.
    tip_racks = {}
    rack_tip_prims_by_pipette = {}
    for pipette_id in ('gilson_pipetman_p200', 'gilson_pipetman_p1000'):
        rack = create_tip_rack(stage, root.GetPath(), 0.0, Gf, UsdGeom, pipette_id)
        rack_tip_prims_by_pipette[pipette_id] = rack.pop('tip_prims')
        used_count = sum(action.kind == 'load_tip' and action.pipette_id == pipette_id
                         for action in session.actions[:session.index])
        for prim in rack_tip_prims_by_pipette[pipette_id][:used_count]:
            prim.GetVisibilityAttr().Set('invisible')
        tip_racks[pipette_id] = rack
    tip_rack = tip_racks[current_pipette_id]
    manifest['presentation']['tip_racks'] = tip_racks
    manifest['presentation']['pipette_selection_model'] = {
        'source': 'same planned action pipette_id as continuous-range error model',
        'tool_exchange_representation': 'two independent pipette proxies with robot park/pick kinematic actions at separate stands',
        'tip_consumption_accounting': 'separate 96-slot inventory per recommended tip ID',
        'geometry_limit': 'pipette bodies, stands, and rack envelopes are project visual proxies, not manufacturer CAD or force-validated grasping'}
    manifest['presentation']['pipette_usage'] = []
    disposal = DisposalVisuals(stage, root.GetPath())
    manifest['tip_disposal'] = {'model': 'kinematic_release_and_fall_not_contact_physics',
                               'waste_xy_m': WASTE_XY, 'residual_source': 'existing_tip_ledger',
                               'asset': 'custom_open_box_no_suitable_catalogue_asset_confirmed'}
    def select_pipette_visual(pipette_id):
        global current_pipette_id, current_pipette_profile
        global current_tip_length_m, tool_apex_offset_m, tip_rack
        global pipette_root, pipette_root_translate, tip, chamber, chamber_translate, attached
        profile = pipette_visual_profile(pipette_id)
        current_pipette_id = pipette_id
        current_pipette_profile = profile
        current_tip_length_m = float(profile['tip_length_m'])
        tool_apex_offset_m = -PIPETTE_NOZZLE_BOTTOM_OFFSET_M + current_tip_length_m
        tip_rack = tip_racks[pipette_id]
        tool_visual = pipette_visuals[pipette_id]
        pipette_root = tool_visual['root']
        pipette_root_translate = tool_visual['translate']
        tip = tool_visual['tip']
        chamber = tool_visual['chamber']
        chamber_translate = tool_visual['chamber_translate']
        attached = tool_visual['attached']
        tool_visual['tip'].GetRadiusAttr().Set(current_tip_radius_m)
        disposal.set_tip_model(current_tip_length_m, current_tip_radius_m)
        manifest['presentation']['active_pipette_profile'] = {
            key: profile[key] for key in ('pipette_id', 'model_name', 'tip_id',
                'volume_range_ul', 'tip_length_m', 'geometry_status')}

    select_pipette_visual(current_pipette_id)
    volume_attrs = {
        name: root.GetPrim().CreateAttribute("volume_ul_" + name, Sdf.ValueTypeNames.Double)
        for name in ("copper_stock", "water", "tip", "left_half_cell", "right_half_cell", "waste")
    }
    root_tip_attached = root.GetPrim().CreateAttribute("tip_attached", Sdf.ValueTypeNames.Bool)
    root_tip_attached.Set(False)
    volume_attrs["copper_stock"].Set(session.state.containers["copper_stock"].volume_l * 1_000_000)
    volume_attrs["water"].Set(session.state.containers["water"].volume_l * 1_000_000)
    volume_attrs["left_half_cell"].Set(0.0)
    volume_attrs["right_half_cell"].Set(0.0)
    volume_attrs["tip"].Set(0.0)
    volume_attrs["waste"].Set(0.0)
    if resume_archive:
        for name, store in session.state.containers.items():
            volume_attrs[name].Set(store.volume_l * 1_000_000)
        volume_attrs['tip'].Set(session.state.tip.liquid.volume_l * 1_000_000)
        volume_attrs['waste'].Set(session.state.waste.volume_l * 1_000_000)
        attached.Set(bool(session.state.tip.loaded))
        root_tip_attached.Set(bool(session.state.tip.loaded))
    display_volumes = {name: attr.Get() for name, attr in volume_attrs.items()}
    manifest['presentation']['pipette_grasp'] = {
        'anchor': 'robot__grasping_frame_world_pos',
        'independent_pipette_models': list(pipette_visuals),
        'stand_xy_by_pipette_id_m': {key: list(value) for key, value in PIPETTE_STAND_XY_BY_ID.items()},
        'grip_collar_diameter_m': PIPETTE_GRIP_RADIUS_M * 2,
        'configured_franka_closed_gap_m': .010,
        'attachment_model': 'kinematic_visual_alignment_only_no_contact_force_validation',
        'gripper_closed_while_pipette_held': True,
        'park_support_plane_derived_from_lower_collar': True}
    # Keep the live USD XformOp handle out of JSON; persist only its plain metadata above.
    for side, electrode_visual in electrode_visuals.items():
        if electrode_installed[side]:
            set_electrode_center(electrode_visual, electrode_visual[f'{side}_cell_center_m'])
        else:
            set_electrode_center(electrode_visual, electrode_visual['rack_center_m'])
    display_left_cu_mol = session.state.containers['left_half_cell'].amounts_mol.get('Cu', 0.0)
    display_cell_cu_mol = {
        cell_id: session.state.containers[cell_id].amounts_mol.get('Cu', 0.0)
        for cell_id in CELLS}

    rep.functional.create.dome_light(intensity=700, parent="/World", name="TransferLight")
    manifest['presentation']['liquid_optics'] = configure_liquid_lighting(stage, root.GetPath())
    # Restore the original wide, elevated view. A low secondary camera could
    # put the tabletop/robot in front of the nozzle; details now use this frame.
    # Look from negative Y so increasing-X maps to screen-right: the physical
    # left cell at x=.55 is then also visibly the left cell.
    overview_eye, overview_target = (.45, -1.35, 1.10), (.45, .03, .22)
    camera = rep.functional.create.camera(position=overview_eye, look_at=overview_target,
                                          parent="/World", name="TransferCamera")
    scene_width, scene_height = args.video_width * 3 // 4, args.video_height - 210
    product = rep.create.render_product(camera.GetPath(), (scene_width, scene_height))
    rgb = rep.AnnotatorRegistry.get_annotator("rgb", device="cpu")
    rgb.attach([product])
    # Native macro render, not an enlarged crop of the distant overview.
    # It observes the same stage/update and never advances simulation itself.
    close_camera = UsdGeom.Camera.Define(stage, root.GetPath().AppendChild('MacroCamera'))
    close_camera.CreateFocalLengthAttr(50)
    close_camera.CreateHorizontalApertureAttr(36)
    close_camera.CreateVerticalApertureAttr(36)
    close_camera.CreateClippingRangeAttr(Gf.Vec2f(.001, 20.0))
    close_transform = close_camera.AddTransformOp()
    close_product = rep.create.render_product(str(close_camera.GetPath()), (1024, 1024))
    close_rgb = rep.AnnotatorRegistry.get_annotator('rgb', device='cpu')
    close_rgb.attach([close_product])
    manifest['presentation']['macro_resolution'] = [1024, 1024]
    manifest['presentation']['continuity'] = {'scene_resets': 0,
        'checkpoints': {},
        'layout_revision_applied_before_recording': BENCH_LAYOUT_VERSION,
        'cell_ids': list(CELLS), 'next_actions': 'prepare_left_then_right_then_simultaneous_mixing_then_park_pipette_then_install_left_then_right_copper_electrodes',
        'electrodes_and_bridge_installed': False}
    presentation = Presentation(args.video_width, args.video_height, args.video_font)
    manifest['presentation']['language'] = 'zh' if presentation.chinese else 'en_fallback'
    presentation_plan = session.snapshot()['plan']
    manifest['presentation']['sidebar'] = {
        'layout': 'current_action_workflow_detail_confirmed_cells',
        'workflow_source': 'session_action_plan_and_successful_receipts',
        'numeric_source': 'confirmed_session_ledger_not_animation',
        'interactive_controls_in_video': False}

    def update_visuals():
        mix_elapsed = steps*dt-stirring_started_s if stirring_started_s is not None else 0.0
        mixing_active = current_label == 'mix/rotation'
        ee = obs["articulations"]["robot__ee_world_pos"][0].detach().cpu().tolist()
        grasp_frame = obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
        for pipette_id, visual in pipette_visuals.items():
            tool_pose = (tuple(grasp_frame) if held_pipette_id == pipette_id else
                         (*PIPETTE_STAND_XY_BY_ID[pipette_id], PIPETTE_DOCK_EE_Z_M))
            visual['translate'].Set(Gf.Vec3d(*tool_pose))
        if held_electrode_side in electrode_visuals:
            set_electrode_center(electrode_visuals[held_electrode_side],
                                 (ee[0], ee[1], ee[2] - held_electrode_grasp_offset_z_m))
        if held_bridge and bridge_visual is not None:
            set_bridge_center(bridge_visual, (ee[0], ee[1], ee[2] - held_bridge_grasp_offset_z_m))
        if held_lead_side is not None:
            set_lead_center(measurement_visual['clips'][held_lead_side], grasp_frame)
        tip.GetVisibilityAttr().Set("inherited" if attached.Get() else "invisible")
        held = float(display_volumes["tip"])
        height = max(0.0001, min(0.04, held * 1e-9 / (math.pi * 0.002 ** 2)))
        chamber.GetHeightAttr().Set(height)
        chamber_translate.Set(Gf.Vec3d(0.0, 0.0, -0.083 - height / 2))
        chamber.GetVisibilityAttr().Set("inherited" if held > 0 and attached.Get() else "invisible")
        for name, visual in (("copper_stock", stock_liquid), ("water", water_liquid),
                             ("left_half_cell", target_liquid),
                             ("right_half_cell", right_target_liquid)):
            value = display_volumes[name]
            geometry = vessel_geometry_at(name, value)
            vessel_geometry[name] = geometry
            update_contours(vessel_contours.get(name), geometry)
            # No minimum fake fill: an empty vessel stays visibly empty.
            visual_depth = geometry['liquid_depth']
            if name == 'copper_stock':
                initial_ul = session.config['containers']['copper_stock']['volume_ml'] * 1000
                visual_depth = (geometry['rim_z'] - geometry['floor_z']) * .45 * value / initial_ul
            visual_geometry = dict(geometry)
            visual_geometry['liquid_depth'] = visual_depth
            visual_geometry['surface_z'] = geometry['floor_z'] + visual_depth
            cell_surface_active = mixing_active and (
                session.includes_paired_mix or name == 'left_half_cell')
            update_liquid_visual(visual, visual_geometry, visible=value > 0,
                                 mixing_phase_s=mix_elapsed if name in CELLS and cell_surface_active else None,
                                 mixing_strength=.55*min(1.,mix_elapsed/.8,max(0.,(current_mix_duration_s-mix_elapsed)/1.5)))
        # Liquid is visibly translucent blue; color is a qualitative bulk-average
        # cue only, and the ledger retains the unmixed parcels.
        # Qualitative appearance only, not calibrated absorbance; never changes
        # concentration, volume, or the chemistry state.
        for cell_id, material in (('left_half_cell', target_material),
                                  ('right_half_cell', right_target_material)):
            volume_ul = display_volumes[cell_id]
            cu_m = display_cell_cu_mol[cell_id] / (volume_ul*1e-6) if volume_ul > 0 else 0
            update_liquid_material(material, cu_m)
        for cell_id, visuals in stirring_visuals.items():
            cell_mix_active = mixing_active and (
                session.includes_paired_mix or cell_id == 'left_half_cell')
            update_stirring_visuals(visuals, mix_elapsed, cell_mix_active)
        camera_target = focus_key(current_label, command or {}, completed_command)
        if camera_target == 'measurement_lead_rack':
            side = ((command or completed_command or {}).get('target') or 'left_half_cell').split('_')[0]
            center = measurement_visual['clips'][side]['position']
            focus = Gf.Vec3d(center[0], center[1], center[2]-.012)
            distance, vertical, x_offset = .14, .09, 0.
        elif camera_target == 'measurement_connection':
            focus = Gf.Vec3d(.5675, -.171, CELLS['left_half_cell'].support_z+.062)
            distance, vertical, x_offset = .155, .090, 0.
        elif camera_target == 'measurement_result':
            focus = Gf.Vec3d(.570, -.160, CELLS['left_half_cell'].support_z+.038)
            distance, vertical, x_offset = .165, .110, 0.
        elif camera_target == 'salt_bridge_rack':
            focus = Gf.Vec3d(BRIDGE_RACK_CENTER_M[0], BRIDGE_RACK_CENTER_M[1], BRIDGE_RACK_CENTER_M[2] - .032)
            distance, vertical, x_offset = .16, .10, .015
        elif camera_target == 'salt_bridge_installation':
            center = bridge_visual['target_center_m']
            focus = Gf.Vec3d(center[0], center[1], center[2] - .035)
            distance, vertical, x_offset = .13, .075, 0.
        elif camera_target == 'tip_rack':
            focus = Gf.Vec3d(tip_rack['xy'][0], tip_rack['xy'][1],
                              tip_rack['rack_top_z_m'] + .025)
            distance, vertical, x_offset = .22, .11, .035
        elif str(camera_target).startswith('pipette_stand_'):
            pipette_id = str(camera_target).replace('pipette_stand_', '')
            stand_xy = PIPETTE_STAND_XY_BY_ID.get(pipette_id, PIPETTE_STAND_XY)
            focus = Gf.Vec3d(*stand_xy, PIPETTE_DOCK_EE_Z_M - .055)
            distance, vertical, x_offset = .20, .12, .025
        elif camera_target == 'pipette_stand':
            focus = Gf.Vec3d(*PIPETTE_STAND_XY, PIPETTE_DOCK_EE_Z_M - .055)
            distance, vertical, x_offset = .20, .12, .025
        elif camera_target == 'electrode_rack':
            focus = Gf.Vec3d(*COPPER_RACK_XY, COPPER_RACK_CAP_CENTER_Z_M)
            distance, vertical, x_offset = .22, .13, .025
        elif camera_target == 'left_electrode_installation':
            focus = Gf.Vec3d(CELLS['left_half_cell'].xy[0] + CELL_AXIS_X_OFFSET_M,
                             CELLS['left_half_cell'].xy[1],
                             CELLS['left_half_cell'].support_z + .040)
            distance, vertical, x_offset = .10, .075, 0.0
        elif camera_target == 'right_electrode_installation':
            focus = Gf.Vec3d(CELLS['right_half_cell'].xy[0] + CELL_AXIS_X_OFFSET_M,
                             CELLS['right_half_cell'].xy[1],
                             CELLS['right_half_cell'].support_z + .040)
            distance, vertical, x_offset = .10, .075, 0.0
        elif camera_target == 'waste':
            focus = Gf.Vec3d(WASTE_XY[0], WASTE_XY[1], WASTE_RIM_Z * .55)
            distance, vertical, x_offset = .22, .12, .035
        elif camera_target == 'both_half_cells':
            focal_point, distance, vertical, x_offset = mixing_camera_spec(
                {name: vessel_geometry[name] for name in CELLS})
            focus = Gf.Vec3d(*focal_point)
        elif camera_target in CELLS and ((command or completed_command or {}).get('action') == 'mix'):
            focal_point, distance, vertical, x_offset = mixing_camera_spec(
                {camera_target: vessel_geometry[camera_target]})
            focus = Gf.Vec3d(*focal_point)
        else:
            detail = vessel_geometry.get(camera_target) or vessel_geometry['left_half_cell']
            focus_z = (detail['floor_z'] + detail['rim_z']) / 2
            focus = Gf.Vec3d(*detail['xy'], focus_z)
            distance = .115 if camera_target in CELLS else .24
            vertical, x_offset = distance * .38, distance * .12
        # Negative-Y view preserves the same left/right mapping as the overview.
        eye = focus+Gf.Vec3d(x_offset, -distance, vertical)
        mixing_detail = camera_target == 'both_half_cells' or (
            camera_target in CELLS and (command or completed_command or {}).get('action') == 'mix')
        close_camera.GetFocalLengthAttr().Set(150. if mixing_detail else 50.)
        up = Gf.Vec3d(0,1,0) if mixing_detail else Gf.Vec3d(0,0,1)
        close_transform.Set(Gf.Matrix4d().SetLookAt(eye, focus, up).GetInverse())
        manifest['presentation']['last_macro_focus'] = camera_target
        # These optical droplets share the same step/progress, not a second
        # material ledger and not independently simulated fluid particles.
        target_name = (command or {}).get('target') or 'left_half_cell'
        target_geometry = vessel_geometry[target_name] if target_name in CELLS else vessel_geometry['left_half_cell']
        end_z = target_geometry['surface_z'] + .0013
        nozzle_z = ee[2] - tool_apex_offset_m
        active = dispense_progress is not None and nozzle_z > end_z
        for index, drop in enumerate(drops):
            drop.GetVisibilityAttr().Set('inherited' if active else 'invisible')
            if active:
                fall = (dispense_progress * 8 + index/3) % 1
                z = nozzle_z - (nozzle_z - end_z) * fall * fall
                UsdGeom.Xformable(drop).GetOrderedXformOps()[0].Set(Gf.Vec3d(ee[0], ee[1], z))

    def capture(record=True):
        global frames
        update_visuals()
        omni.kit.app.get_app().update()
        data = rgb.get_data()
        if isinstance(data, dict):
            data = data["data"]
        frame = np.asarray(data)
        if frame.ndim != 3 or frame.size == 0 or float(frame.std()) <= 2:
            return False
        img = Image.fromarray(frame[:, :, :3].astype(np.uint8).copy())
        close_data = close_rgb.get_data()
        if isinstance(close_data, dict):
            close_data = close_data['data']
        macro = np.asarray(close_data)
        if macro.ndim != 3 or macro.size == 0 or float(macro.std()) <= 2:
            return False
        macro_image = Image.fromarray(macro[:, :, :3].astype(np.uint8).copy())
        cam = UsdGeom.Camera(stage.GetPrimAtPath(camera.GetPath()))
        ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
        anchors = {name: project_point(point, overview_eye, overview_target, scene_width, scene_height,
                                       cam.GetFocalLengthAttr().Get(), cam.GetHorizontalApertureAttr().Get())
                   for name, point in {'copper_stock': (*vessel_geometry['copper_stock']['xy'], vessel_geometry['copper_stock']['rim_z']),
                                       'water': (*vessel_geometry['water']['xy'], vessel_geometry['water']['rim_z']),
                                       'left_half_cell': (*vessel_geometry['left_half_cell']['xy'], vessel_geometry['left_half_cell']['rim_z']),
                                       'right_half_cell': (*vessel_geometry['right_half_cell']['xy'], vessel_geometry['right_half_cell']['rim_z']),
                                       'tip': ((*PIPETTE_STAND_XY_BY_ID[current_pipette_id], PIPETTE_DOCK_EE_Z_M - .055)
                                               if held_pipette_id is None else (ee[0], ee[1], ee[2] - .11)),
                                       'waste': (*WASTE_XY, WASTE_RIM_Z)}.items()}
        img = presentation.compose_overview(img, run_id=session.run_id, phase=current_label,
                          sim_time=steps * dt, volumes=display_volumes,
                          stock_concentration=session.config['containers']['copper_stock']['concentrations_m']['Cu'],
                          completed=completed_command,
                          command=command,
                          water_concentration=session.config['containers']['water']['concentrations_m'].get('Cu', 0.0),
                          planned_ul=command['requested_volume_ul'] if command else None,
                          anchors=anchors, tip_attached=bool(attached.Get()),
                          detail_scene=macro_image,
                          plan=presentation_plan, completed_steps=session.index,
                          voltage=session.snapshot()['voltage'],
                          confirmed_volumes={name: session.state.containers[name].volume_l * 1_000_000
                                             for name in CELLS},
                          cell_concentrations={name: (session.state.containers[name].amounts_mol.get('Cu', 0.0)
                                                      / session.state.containers[name].volume_l
                                                      if session.state.containers[name].volume_l > 0 else None)
                                               for name in CELLS},
                          left_liquid_height_mm=vessel_geometry['left_half_cell']['liquid_depth']*1000)
        if record:
            if preview is not None and preview.due():
                try:
                    preview.publish(img, session.snapshot(), frame_index=frames, simulation_time_s=steps * dt, phase=current_label, command=command)
                except Exception:
                    pass  # Preview is best-effort; it must not alter receipts or recording.
            writer.append_data(np.asarray(img))
            if process_trace is not None:
                try:
                    frame_state = synchronized_state(session.snapshot(), frame_index=frames,
                        simulation_time_s=steps*dt, phase=current_label, command=command)
                    if measurement_visual:
                        frame_state['measurement_cables'] = cable_frame_evidence(measurement_visual['clips'])
                    process_trace.sample(frame_state)
                except Exception as trace_error:
                    print(f"PROCESS_TRACE_ERROR={trace_error!r}", flush=True)
                    if runtime_scene is not None:
                        raise RuntimeError('约束运行缺少同帧轨迹记录，停止动作') from trace_error
            manifest["frames"].append({"frame": frames, "video_time_s": frames / fps,
                                       "simulation_time_s": steps * dt,
                                       "step_id": command["step_id"] if command else None,
                                       "phase": current_label,
                                       'stir_bar_angles_deg': {name: float(v['bar_rotate'].Get()) for name,v in stirring_visuals.items()},
                                       'view': 'persistent_overview_with_detail',
                                       'last_confirmed_step_id': completed_command['step_id'] if completed_command else None,
                                       'camera_focus': focus_key(current_label, command or {}, completed_command),
                                       'held_lead': held_lead_side,
                                       'robot_grasp_frame_m':obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist(),
                                       'lead_centers_m': {s:list(c['position']) for s,c in measurement_visual['clips'].items()} if measurement_visual else {},
                                       'gripper_gap_m': sum(abs(float(v)) for v in obs['articulations']['robot__gripper_pos'][0].detach().cpu().flatten().tolist()),
                                       'confirmed_virtual_meter_mv': session.snapshot()['voltage'].get('virtual_meter_mv')})
            frames += 1
        else:
            img.save(run_dir / "preview.png")
        return True

    for attempt in range(30):
        if capture(False):
            print("CAMERA_OK", flush=True)
            break
    else:
        raise RuntimeError("相机未获得可用画面")
    if args.mixing_camera_audit:
        if args.preview_snapshot is None:
            raise RuntimeError('Mixing camera audit needs a snapshot')
        archive = json.loads(args.preview_snapshot.read_text(encoding='utf-8'))
        for name in CELLS:
            value = archive['snapshot']['state']['containers'][name]
            display_volumes[name] = value['volume_ul']
            display_cell_cu_mol[name] = value['amounts_mol'].get('Cu',0.)
        command = {'action':'mix_pair','target':'left_half_cell','requested_volume_ul':None}
        current_label = 'mix/rotation'
        stirring_started_s = steps*dt-2.
        update_visuals()
        focus = Gf.Vec3d(.57,-.16,CELLS['left_half_cell'].support_z+.008)
        close_camera.GetFocalLengthAttr().Set(100.)
        for name,offset,up in (
                ('north', (0.,.05,.20),(0,1,0)),
                ('north_high', (0.,.03,.22),(0,1,0)),
                ('north_west',(-.03,.03,.22),(0,1,0)),
                ('south',(0.,-.03,.22),(0,0,1))):
            eye = focus+Gf.Vec3d(*offset)
            close_transform.Set(Gf.Matrix4d().SetLookAt(eye,focus,Gf.Vec3d(*up)).GetInverse())
            for _ in range(36):omni.kit.app.get_app().update()
            data=close_rgb.get_data()
            if isinstance(data,dict):data=data['data']
            Image.fromarray(np.asarray(data)[:,:,:3].astype(np.uint8).copy()).save(run_dir/('mix_camera_'+name+'.png'))
        atomic_json(run_dir/'preview_manifest.json',{'status':'preview_ready','actions_executed':0,
                    'video_generated':False,'run_id':session.run_id,'scope':'frozen_camera_audit'})
        print('MIXING_CAMERA_AUDIT_READY='+str(run_dir),flush=True)
        raise SystemExit(0)
    if args.preview_only:
        # Frozen snapshots are explicitly separate from execution runs and never
        # create receipts or edit the archive that supplied these quantities.
        archive = json.loads(args.preview_snapshot.read_text(encoding='utf-8'))
        frozen = archive['snapshot']['state']['containers']
        left = frozen['left_half_cell']
        source = frozen['copper_stock']
        mother_parts = [p for p in left['mixing_components'] if p['source_id']=='copper_stock']
        mother_ul = sum(p['volume_l'] for p in mother_parts)*1e6
        mother_mol = sum(p['amounts_mol'].get('Cu',0.) for p in mother_parts)
        display_volumes['copper_stock'] = source['volume_ul']
        display_volumes['water'] = frozen['water']['volume_ul']
        preview_frames = []
        cases = [('source', source['volume_ul'], source['amounts_mol'].get('Cu',0.), 'copper_stock'),
                 ('receiver_stock', mother_ul, mother_mol, 'left_half_cell'),
                 ('receiver_after_water', left['volume_ul'], left['amounts_mol'].get('Cu',0.), 'left_half_cell')]
        for name, volume_ul, cu_mol, target in cases:
            display_volumes['left_half_cell'] = volume_ul if target=='left_half_cell' else mother_ul
            display_left_cu_mol = cu_mol if target=='left_half_cell' else mother_mol
            display_cell_cu_mol['left_half_cell'] = display_left_cu_mol
            command = {'action': 'dispense', 'target': target,
                       'source': target, 'requested_volume_ul': None}
            current_label = 'preview/dispense/settle'
            update_visuals()
            # Accumulate clean stills at the same runtime renderer/settings.
            for _ in range(48):
                omni.kit.app.get_app().update()
            data = close_rgb.get_data()
            data = data['data'] if isinstance(data,dict) else data
            frame = np.asarray(data)
            if frame.ndim != 3 or frame.size == 0 or float(frame.std())<=2:
                raise RuntimeError('Empty actual-renderer liquid preview')
            Image.fromarray(frame[:,:,:3].astype(np.uint8).copy()).save(run_dir/(name+'.png'))
            preview_frames.append({'file':name+'.png', 'focus':target, 'volume_ul':volume_ul})
        manifest.update({'status':'preview_ready', 'preview_only':True,
                         'snapshot_ref':str(args.preview_snapshot), 'previews':preview_frames,
                         'actions_executed':0, 'video_generated':False})
        atomic_json(run_dir/'preview_manifest.json', manifest)
        print('LIQUID_PREVIEW_READY='+str(run_dir), flush=True)
        raise SystemExit(0)
    preview = optional_publisher(run_dir, session.run_id, enabled=args.live_preview and not args.preview_only)
    writer = imageio.get_writer(partial, fps=fps, codec="libx264",
                               macro_block_size=1,
                               ffmpeg_params=['-crf', str(args.video_crf),
                                              '-preset', args.video_preset,
                                              '-movflags', '+faststart'])
    session.update_video("recording")
    try:
        validation_policy = json.loads((Path(__file__).resolve().parents[1]/'process_validation/policy.json').read_text(encoding='utf-8'))
        process_trace = SceneTrace(stage, run_dir, session.run_id, validation_policy, env.scene['robot'])
        manifest['process_validation'] = {'trace': 'process_validation/trace.jsonl',
                                          'geometry': 'process_validation/geometry.json',
                                          'automatic_post_run_audit': True}
    except Exception as trace_error:
        (run_dir/'process_validation').mkdir(exist_ok=True)
        atomic_json(run_dir/'process_validation/trace_error.json', {'message':str(trace_error)})
        print(f"PROCESS_TRACE_INIT_ERROR={trace_error!r}", flush=True)
    runtime_scene = RuntimeSceneConstraints(stage,str(root.GetPath()),env.scene['robot'],runtime_policy,
        extra_static_roots=[env.scene[name].root_physx_view.prim_paths[0] for name in ('beaker','water_source')])
    runtime_scene.coverage['physx_self_collision_solver_enabled']=True
    runtime_scene.coverage['self_collision_scope']='PhysX configured pairs during simulation steps; no independent continuous inter-step certificate'
    manifest['physical_constraints']={'policy':runtime_policy,'coverage':runtime_scene.coverage,
                                       'complete_physics_certified':False}

    def enforce_constraint(function,*values,**kwargs):
        try:
            return function(*values,**kwargs)
        except PhysicalConstraintError as exc:
            manifest['physical_constraints']['violation']={
                'code':exc.code,'message':str(exc),'phase':current_label,**exc.evidence}
            raise

    enforce_constraint(runtime_scene.check,'initialization')
    manifest.update({"status": "recording", "fps": fps, "simulation_step_dt_s": dt,
                     "video_encoding": {"codec": "libx264", "crf": args.video_crf,
                                        "preset": args.video_preset,
                                        "resolution": [args.video_width, args.video_height],
                                        "fps_source": "1 / simulation_step_dt; unchanged by quality profile"}})

    def move(pos, precision=.015, orientation_wxyz=None, orientation_threshold=.1):
        return MoveToPoseCfg(agent_assets="robot", target_positions=torch.tensor([pos]),
                             target_orientations=torch.tensor([orientation_wxyz or
                                                               [0.70710678, 0.70710678, 0.0, 0.0]]),
                             action_space_info=FRANKA_IK_ACTION_SPACE,
                             position_threshold=precision, orientation_threshold=orientation_threshold,
                             settling_time=.3 if precision <= .005 else .1, timeout=15.0)

    def record_elbow_posture():
        if not command or command.get('step_id') not in ('ui_pick_right_lead','ui_connect_right_lead'):
            return
        term=env.action_manager.get_term('Actions_robot_arm_action')
        reference=getattr(term,'elbow_reference_height_world',None)
        if reference is None:
            return
        height=float(env.scene['robot'].data.body_pos_w[0,term.elbow_body_index,2].item())
        entry=manifest['physical_constraints'].setdefault('elbow_posture',{
            'body':'panda_link4','reference_height_world_m':reference,
            'minimum_runtime_height_world_m':height,
            'rule':'task_specific_soft_preference','fixed_height_or_direction':False})
        entry['minimum_runtime_height_world_m']=min(entry['minimum_runtime_height_world_m'],height)

    def tick(machine):
        global obs, steps
        if not app.is_running():
            raise RuntimeError("仿真应用已退出")
        enforce_constraint(runtime_scene.check,current_label)
        record_elbow_posture()
        with torch.inference_mode():
            action, semantics = machine.step(obs)
            arm_term=env.action_manager.get_term('Actions_robot_arm_action')
            joint_route=(runtime_motion_context or {}).get('joint_route')
            if (runtime_motion_context is not None
                    and runtime_motion_context['machine'] is machine and joint_route):
                waypoint=min(int(machine.current_action_idx[0].item()),len(joint_route)-1)
                arm_term.preferred_joint_positions=joint_route[waypoint]
            else:
                arm_term.preferred_joint_positions=None
            if session.includes_left_electrode:
                if action.shape[-1] != 8:
                    raise RuntimeError(f"Franka IK 动作维度异常，不能安全控制夹爪：{action.shape}")
                action = action.clone()
                action[..., 7] = gripper_command
            obs, _, terminated, truncated, _ = env.step(action.to(env.device), semantic_actions=semantics)
        steps += 1
        if bool((terminated | truncated).any().item()):
            raise RuntimeError("场景在过程内发生重置，停止以免账本与画面脱节")
        if not capture():
            raise RuntimeError("录制中出现空白画面")
        enforce_constraint(runtime_scene.check,current_label)
        record_elbow_posture()
        if runtime_motion_context is not None and runtime_motion_context['machine'] is machine:
            waypoint=min(int(machine.current_action_idx[0].item()),len(runtime_motion_context['poses'])-1)
            enforce_constraint(motion_monitor.observe,(id(machine),waypoint),
                obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist(),
                obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist())
        if process_trace is not None and process_trace.last_preflight_intersections:
            actual = [float(value) for value in obs['articulations'][
                'robot__ee_world_pos'][0].detach().cpu().tolist()]
            collisions = process_trace.last_preflight_intersections
            manifest['motion_failure'] = {
                'phase': current_label,
                'actual_ee_position_m': actual,
                'preflight_collision': collisions,
            }
            raise RuntimeError('预抓取路径发现手部网格与支架相交，停止后续动作: '
                               + json.dumps(collisions, ensure_ascii=False))

    def motion(poses, label, precision=.015, orientation_wxyz=None,
               side_pose_observed_bias_xyz_m=None, physical_tolerance_m=.008,
               controller_bias_margin_m=.012):
        global current_label,runtime_motion_context
        current_label=label
        poses=enforce_constraint(prepare_waypoints,poses)
        if orientation_wxyz is None:
            orientation_wxyz = active_route_orientation_wxyz
        held_key=(('pipette',held_pipette_id) if held_pipette_id is not None else
                  ('electrode',held_electrode_side) if held_electrode_side is not None else
                  ('bridge','salt_bridge') if held_bridge else
                  ('lead',held_lead_side) if held_lead_side is not None else None)
        measured_orientation=obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist()
        orientation_wxyz=enforce_constraint(held_pose_constraint.choose,held_key,
                                            measured_orientation,orientation_wxyz)
        if side_pose_observed_bias_xyz_m is not None and orientation_wxyz is None:
            raise RuntimeError('侧向姿态控制补偿不能用于未指定姿态的动作')
        if side_pose_observed_bias_xyz_m is not None and len(poses) > 1:
            # Complete and verify each physical waypoint before continuing
            # toward the electrode rack or cell.
            last_machine = None
            for index, pose in enumerate(poses):
                last_machine = motion([pose], label,
                                      precision=precision if index == len(poses) - 1 else .015,
                                      orientation_wxyz=orientation_wxyz,
                                      side_pose_observed_bias_xyz_m=side_pose_observed_bias_xyz_m,
                                      physical_tolerance_m=(physical_tolerance_m
                                                            if index == len(poses) - 1 else .008),
                                      controller_bias_margin_m=controller_bias_margin_m)
            return last_machine
        current_label = label
        bias = side_pose_observed_bias_xyz_m
        commanded_poses = ([tuple(float(pose[i]) - float(bias[i]) for i in range(3))
                            for pose in poses] if bias is not None else poses)
        measured_ee=obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
        target_orientation=orientation_wxyz or [0.70710678,0.70710678,0.,0.]
        enforce_constraint(runtime_scene.check_path,measured_ee,commanded_poses,target_orientation,measured_orientation)
        if bias is not None:
            enforce_constraint(runtime_scene.check_path,measured_ee,poses,target_orientation,measured_orientation)
        # Matterix's internal settling threshold is relative to its command.
        # A previously measured, stable side-pose residual can keep that
        # internal action pending even when the physical target is reachable.
        # This threshold only lets the controller finish; every actual pose is
        # checked against the original physical waypoint below.
        controller_precision = (max(float(precision),
                                    math.sqrt(sum(float(value) ** 2 for value in bias))
                                    + float(controller_bias_margin_m))
                                if bias is not None else precision)
        machine = StateMachine(num_envs=1, dt=dt, device=env.device)
        machine.set_action_sequence([move(p, controller_precision if i == len(poses)-1 else .015,
                                          orientation_wxyz, .2 if bias is not None else .1)
                                     for i,p in enumerate(commanded_poses)])
        machine.reset()
        joint_route=None
        if command and command.get('step_id') in ('ui_pick_right_lead','ui_connect_right_lead'):
            try:
                reachability=plan_live_route(env.action_manager.get_term('Actions_robot_arm_action'),
                                            commanded_poses,target_orientation,command.get('step_id',''))
            except ValueError as exc:
                manifest['physical_constraints']['violation']={
                    'code':'joint_route_unreachable','message':str(exc),'phase':label}
                raise PhysicalConstraintError('joint_route_unreachable',str(exc),phase=label) from exc
            joint_route=reachability['joint_targets_rad']
            manifest['physical_constraints'].setdefault('joint_route_preflights',[]).append(
                {'phase':label,**reachability})
        runtime_motion_context={'machine':machine,'poses':poses,'phase':label,'joint_route':joint_route}
        for _ in range(max(1, round(40 / dt))):
            tick(machine)
            if bool(machine.action_sequence_failure.any().item()):
                ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                waypoint_index = int(machine.current_action_idx[0].item())
                failed_waypoint = list(poses[min(waypoint_index, len(poses)-1)])
                xyz_error = [float(ee[i]-failed_waypoint[i]) for i in range(3)]
                evidence = {'phase':label,'failed_waypoint_index':waypoint_index,
                            'failed_waypoint_position_m':failed_waypoint,
                            'planned_waypoints_m':[list(pose) for pose in poses],
                            'actual_ee_position_m':ee,
                            'requested_final_position_m':list(poses[-1]),
                            'failed_waypoint_error_m':xyz_error,
                            'position_threshold_m':precision}
                if bias is not None:
                    evidence['controller_waypoint_position_m'] = list(
                        commanded_poses[min(waypoint_index, len(commanded_poses)-1)])
                    evidence['side_pose_observed_bias_xyz_m'] = list(bias)
                    evidence['physical_tolerance_m'] = physical_tolerance_m
                    if high_probe_physically_reached(
                            phase=label, target_m=failed_waypoint, actual_m=ee,
                            closing_axis_world=gripper_closing_axis_world(),
                            tolerance_m=physical_tolerance_m):
                        # The generic IK action still wants a stricter full
                        # quaternion. At this clear high waypoint, the actual
                        # task-space pose and lateral finger axis are the
                        # relevant gate. Never apply this to the low grasp or
                        # installation waypoints.
                        manifest.setdefault('correction', {}).setdefault(
                            'high_probe_external_acceptances', []).append(evidence)
                        return machine
                manifest['motion_failure'] = evidence
                raise RuntimeError('机械臂动作超时: '+json.dumps(evidence,ensure_ascii=False))
            if bool(machine.action_sequence_success.all().item()):
                if bias is not None:
                    ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                    physical_error = [float(ee[i] - poses[-1][i]) for i in range(3)]
                    if max(abs(value) for value in physical_error) > physical_tolerance_m:
                        evidence = {'phase': label,
                                    'requested_final_position_m': list(poses[-1]),
                                    'controller_final_position_m': list(commanded_poses[-1]),
                                    'actual_ee_position_m': ee,
                                    'physical_error_m': physical_error,
                                    'physical_tolerance_m': physical_tolerance_m,
                                    'side_pose_observed_bias_xyz_m': list(bias)}
                        manifest['motion_failure'] = evidence
                        raise RuntimeError('补偿后末端未达原物理目标: '
                                           + json.dumps(evidence, ensure_ascii=False))
                return machine
        raise RuntimeError("机械臂动作超过过程步数限制")

    def interpolated_xyz_waypoints(start, target, max_step_m):
        start = [float(value) for value in start]
        target = [float(value) for value in target]
        distance = math.sqrt(sum((target[i] - start[i]) ** 2 for i in range(3)))
        segments = max(1, math.ceil(distance / float(max_step_m)))
        return [tuple(start[k] + (target[k] - start[k]) * index / segments
                      for k in range(3))
                for index in range(1, segments + 1)]

    def move_exact(target, label, tolerance=.001, approach_z=None,
                   orientation_wxyz=None, transit_height_m=None,
                   orientation_candidates_wxyz=None,
                   staging_offset_xy_m=None, approach_offset_xy_m=None,
                   side_pose_observed_bias_xyz_m=None,
                   orientation_probe_bias_xyz_m=None,
                   high_probe_physical_tolerance_m=.008,
                   high_probe_controller_margin_m=.012,
                   high_transit_orientation_wxyz=None,
                   high_transit_destination_pose_bias=False,
                   high_destination_position_feedback=False,
                   high_transit_waypoint_step_m=None,
                   top_down_vertical_descent=False,
                   route_waypoints_m=None,
                   orientation_probe_at_pregrasp=False,
                   path_step_m=None):
        """Approach once, then correct locally against the original EE target."""
        desired = [float(v) for v in target]
        requested = desired.copy()
        samples = []
        correction_budget=CorrectionBudget(runtime_policy,tolerance)
        for correction in range(runtime_policy['motion']['maximum_local_corrections']):
            # MoveToPose settles with a repeatable ~3.6 mm EE-frame offset in
            # this scene. Let the controller finish within 5 mm, then use the
            # measured EE pose below to compensate. Acceptance is always against
            # the original target, never the adjusted controller command.
            approach = (requested[0], requested[1],
                        float(approach_z) if approach_z is not None else requested[2] + .08)
            if correction == 0 and transit_height_m is not None:
                current = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                high = max(float(transit_height_m), float(approach[2]) + .01,
                           float(current[2]) + .01)
                if orientation_wxyz is not None:
                    # First reach a known clear staging height with the current
                    # orientation; then rotate in place and verify lateral jaw
                    # closure before translating over the rack or vessel.
                    staging_offset = staging_offset_xy_m or (0.0, 0.0)
                    if orientation_probe_at_pregrasp:
                        staging = pregrasp_pose(requested, high, staging_offset)
                        if process_trace is None:
                            raise RuntimeError('缺少实际网格轨迹，不能执行预抓取路径预检')
                        process_trace.enable_preflight_collision_gate(label)
                    else:
                        staging = (.40 + float(staging_offset[0]),
                                   .00 + float(staging_offset[1]), high)
                    current_orientation = obs['articulations'][
                        'robot__ee_world_quat'][0].detach().cpu().tolist()
                    keep_downward_pose = (top_down_vertical_descent
                                          and orientation_error_deg(current_orientation, orientation_wxyz) <= 2.0
                                          and abs(float(gripper_closing_axis_world()[2])) <= .2
                                          and float(gripper_approach_axis_world()[2]) <= -.85)
                    keep_held_pose = (held_electrode_side is not None
                                      and orientation_error_deg(current_orientation, orientation_wxyz) <= 2.0)
                    machine = motion([staging], label + '/safe_staging',
                                     orientation_wxyz=(orientation_wxyz if keep_held_pose or keep_downward_pose
                                                      else None))
                    candidates = orientation_candidates_wxyz or [orientation_wxyz]
                    rejected = []
                    selected_orientation = None
                    for candidate in candidates:
                        try:
                            same_measured_orientation = (
                                orientation_probe_bias_xyz_m is not None
                                and len(candidate) == len(orientation_wxyz)
                                and math.dist([float(value) for value in candidate],
                                              [float(value) for value in orientation_wxyz]) <= 1e-6)
                            probe_bias = (orientation_probe_bias_xyz_m
                                          if same_measured_orientation else None)
                            probe_tolerance = (high_probe_physical_tolerance_m
                                               if same_measured_orientation else .008)
                            probe_margin = (high_probe_controller_margin_m
                                            if same_measured_orientation else .012)
                            if orientation_probe_at_pregrasp:
                                start_orientation = [float(value) for value in obs['articulations'][
                                    'robot__ee_world_quat'][0].detach().cpu().tolist()]
                                initial_angle = orientation_error_deg(start_orientation, candidate)
                                probe_steps = ([] if initial_angle <= 2.0 else orientation_waypoints(
                                    start_orientation, candidate, max_step_deg=30.0))
                                segment_evidence = []
                                for segment_index, probe_orientation in enumerate(probe_steps, 1):
                                    machine = motion(
                                        [staging], label + '/orientation_probe', precision=.005,
                                        orientation_wxyz=probe_orientation)
                                    actual_probe = [float(value) for value in obs['articulations'][
                                        'robot__ee_world_pos'][0].detach().cpu().tolist()]
                                    position_error = [actual_probe[i] - float(staging[i])
                                                      for i in range(3)]
                                    segment = {
                                        'segment_index': segment_index,
                                        'orientation_wxyz': probe_orientation,
                                        'actual_ee_position_m': actual_probe,
                                        'position_error_m': position_error,
                                    }
                                    segment_evidence.append(segment)
                                    if math.dist(actual_probe, staging) > .008:
                                        raise RuntimeError(
                                            '分段姿态预检末端漂移超过8 mm: '
                                            + json.dumps(segment, ensure_ascii=False))
                                manifest.setdefault('correction', {}).setdefault(
                                    'orientation_probe_segments', []).append({
                                        'phase': label + '/orientation_probe',
                                        'pregrasp_position_m': list(staging),
                                        'maximum_step_deg': 30.0,
                                        'initial_orientation_error_deg': initial_angle,
                                        'reused_existing_orientation': not probe_steps,
                                        'segments': segment_evidence,
                                    })
                            else:
                                measured_orientation = obs['articulations'][
                                    'robot__ee_world_quat'][0].detach().cpu().tolist()
                                measured_angle = orientation_error_deg(measured_orientation, candidate)
                                if measured_angle <= 2.0:
                                    manifest.setdefault('correction', {}).setdefault(
                                        'orientation_pose_reuse', []).append({
                                            'phase': label + '/orientation_probe',
                                            'orientation_error_deg': measured_angle,
                                            'holding_electrode_side': held_electrode_side,
                                            'rotation_segments': 0,
                                        })
                                else:
                                    machine = motion([staging], label + '/orientation_probe',
                                                     orientation_wxyz=candidate,
                                                     # Same measured pose and bounded physical tolerance.
                                                     side_pose_observed_bias_xyz_m=probe_bias,
                                                     physical_tolerance_m=probe_tolerance,
                                                     controller_bias_margin_m=probe_margin)
                        except RuntimeError as exc:
                            rejected.append({'orientation_wxyz': candidate,
                                             'reason': str(exc)[:240]})
                            continue
                        axis = gripper_closing_axis_world()
                        approach_axis = (gripper_approach_axis_world()
                                         if top_down_vertical_descent else None)
                        orientation_valid = (abs(float(axis[2])) <= .2
                                             and (not top_down_vertical_descent
                                                  or float(approach_axis[2]) <= -.85))
                        if orientation_valid:
                            try:
                                route_offset = approach_offset_xy_m or (0.0, 0.0)
                                rack_high = (float(requested[0]) + float(route_offset[0]),
                                             float(requested[1]) + float(route_offset[1]), high)
                                if high_transit_orientation_wxyz is not None:
                                    # The side grasp pose has already passed at the
                                    # clear staging point. Keep the gripper open and
                                    # travel upright at high clearance; only rotate
                                    # into the side grasp pose after reaching the rack.
                                    transit_poses = [rack_high]
                                    if high_transit_waypoint_step_m is not None:
                                        route_start = obs['articulations']['robot__ee_world_pos'][
                                            0].detach().cpu().tolist()
                                        transit_poses = interpolated_xyz_waypoints(
                                            route_start, rack_high, high_transit_waypoint_step_m)
                                    machine = motion(transit_poses,
                                                     label + '/high_transit_probe',
                                                     orientation_wxyz=high_transit_orientation_wxyz)
                                    destination_bias = (side_pose_observed_bias_xyz_m
                                                        if high_transit_destination_pose_bias
                                                        else None)
                                    if high_destination_position_feedback:
                                        physical_target = [float(value) for value in rack_high]
                                        commanded_target = physical_target.copy()
                                        feedback = []
                                        reached = False
                                        for feedback_index in range(4):
                                            controller_error = None
                                            try:
                                                machine = motion(
                                                    [tuple(commanded_target)],
                                                    label + '/high_transit_probe',
                                                    orientation_wxyz=candidate)
                                            except RuntimeError as exc:
                                                controller_error = str(exc)[:240]
                                            actual = [float(value) for value in obs['articulations'][
                                                'robot__ee_world_pos'][0].detach().cpu().tolist()]
                                            axis = [float(value) for value in gripper_closing_axis_world()]
                                            physical_error = [physical_target[i] - actual[i]
                                                              for i in range(3)]
                                            evidence = {
                                                'feedback_index': feedback_index + 1,
                                                'physical_target_m': physical_target,
                                                'controller_target_m': commanded_target.copy(),
                                                'actual_ee_position_m': actual,
                                                'physical_error_m': physical_error,
                                                'closing_axis_world': axis,
                                                'controller_timed_out': controller_error is not None,
                                                'controller_error': controller_error,
                                            }
                                            feedback.append(evidence)
                                            if high_probe_physically_reached(
                                                    phase=label + '/high_transit_probe',
                                                    target_m=physical_target, actual_m=actual,
                                                    closing_axis_world=axis,
                                                    tolerance_m=high_probe_physical_tolerance_m):
                                                manifest.setdefault('correction', {}).setdefault(
                                                    'high_destination_position_feedback', []).extend(feedback)
                                                manifest.setdefault('correction', {}).setdefault(
                                                    'high_probe_external_acceptances', []).append({
                                                        'phase': label + '/high_transit_probe',
                                                        'requested_final_position_m': physical_target,
                                                        'actual_ee_position_m': actual,
                                                        'closing_axis_world': axis,
                                                        'feedback_steps': feedback,
                                                    })
                                                manifest.pop('motion_failure', None)
                                                reached = True
                                                break
                                            if (feedback_index == 3
                                                    or actual[2] < .42
                                                    or abs(axis[2]) > .2
                                                    or max(abs(value) for value in physical_error) > .07):
                                                manifest.setdefault('correction', {}).setdefault(
                                                    'high_destination_position_feedback', []).extend(feedback)
                                                manifest['motion_failure'] = {
                                                    'phase': label + '/high_transit_probe',
                                                    'failed_waypoint_index': 0,
                                                    'failed_waypoint_position_m': list(commanded_target),
                                                    'requested_final_position_m': physical_target,
                                                    'actual_ee_position_m': actual,
                                                    'physical_error_m': physical_error,
                                                    'closing_axis_world': axis,
                                                    'high_destination_position_feedback': feedback,
                                                }
                                                raise RuntimeError(
                                                    '高位目标位置反馈未在安全边界内收敛: '
                                                    + json.dumps(feedback, ensure_ascii=False))
                                            # Apply only half of the measured high-pose
                                            # residual, then verify against the unchanged
                                            # physical target on the next controller move.
                                            commanded_target = [commanded_target[i]
                                                                + .5 * physical_error[i]
                                                                for i in range(3)]
                                            if (not .20 <= commanded_target[0] <= .50
                                                    or not -.20 <= commanded_target[1] <= .20
                                                    or not .42 <= commanded_target[2] <= .55):
                                                raise RuntimeError('高位姿态校正命令超出已核验工作空间')
                                        if not reached:
                                            raise RuntimeError('高位侧夹姿态未达到原物理目标')
                                    else:
                                        machine = motion([rack_high],
                                                         label + '/high_transit_probe',
                                                         orientation_wxyz=candidate,
                                                         side_pose_observed_bias_xyz_m=destination_bias,
                                                         physical_tolerance_m=high_probe_physical_tolerance_m,
                                                         controller_bias_margin_m=high_probe_controller_margin_m)
                                    destination_closing_axis = gripper_closing_axis_world()
                                    destination_approach_axis = (gripper_approach_axis_world()
                                                                 if top_down_vertical_descent else None)
                                    if (abs(float(destination_closing_axis[2])) > .2
                                            or (top_down_vertical_descent
                                                and float(destination_approach_axis[2]) > -.85)):
                                        raise RuntimeError('电极架上方未通过指端朝下、闭合轴水平的姿态检查')
                                    selected_orientation = candidate
                                    break
                                machine = motion([rack_high],
                                                 label + '/high_transit_probe',
                                                 orientation_wxyz=candidate,
                                                 side_pose_observed_bias_xyz_m=side_pose_observed_bias_xyz_m,
                                                 physical_tolerance_m=high_probe_physical_tolerance_m,
                                                 controller_bias_margin_m=high_probe_controller_margin_m)
                                selected_orientation = candidate
                                break
                            except RuntimeError as exc:
                                rejected.append({'orientation_wxyz': candidate,
                                                 'reason': str(exc)[:240]})
                                continue
                        rejected.append({'orientation_wxyz': candidate,
                                         'reason': ('夹爪未通过朝下且水平闭合检查'
                                                    if top_down_vertical_descent
                                                    else '夹爪闭合轴仍接近竖直'),
                                         'closing_axis_world': axis,
                                         'gripper_approach_axis_world': approach_axis})
                    if selected_orientation is None:
                        if correction_profile:
                            manifest['correction'].setdefault('pose_preflight', {})[label] = {
                                'staging_position_m': list(staging),
                                'selected_orientation_wxyz': None,
                                'closing_axis_world': None,
                                'gripper_approach_axis_world': None,
                                'rejected_candidates': rejected,
                                'preflight_status': ('no_reachable_top_down_grasp_pose'
                                                     if top_down_vertical_descent
                                                     else 'no_reachable_side_grasp_pose')}
                        grasp_description = ('朝下且水平闭合的夹持姿态'
                                             if top_down_vertical_descent else '侧向夹持角度')
                        raise RuntimeError(f'安全高位姿态预检未找到可达的{grasp_description}: '
                                           + json.dumps(rejected, ensure_ascii=False))
                    if correction_profile:
                        manifest['correction'].setdefault('pose_preflight', {})[label] = {
                            'staging_position_m': list(staging),
                            'selected_orientation_wxyz': selected_orientation,
                            'closing_axis_world': gripper_closing_axis_world(),
                            'gripper_approach_axis_world': gripper_approach_axis_world(),
                            'rejected_candidates': rejected}
                    orientation_wxyz = selected_orientation
                    route_offset = approach_offset_xy_m or (0.0, 0.0)
                    if (approach_offset_xy_m is not None
                            and any(abs(float(value)) > 1e-6 for value in approach_offset_xy_m)):
                        route_approach = (approach[0] + float(route_offset[0]),
                                          approach[1] + float(route_offset[1]), approach[2])
                        poses = [route_approach, approach, tuple(requested)]
                    else:
                        poses = [approach, tuple(requested)]
                else:
                    poses = [(float(current[0]), float(current[1]), high),
                             (float(requested[0]), float(requested[1]), high),
                             approach, tuple(requested)]
            else:
                if correction == 0 and route_waypoints_m:
                    poses = [tuple(float(value) for value in point)
                             for point in route_waypoints_m]
                    poses.extend((approach, tuple(requested)))
                else:
                    poses = [approach, tuple(requested)] if correction == 0 else [tuple(requested)]
            # A residual measured during a clear, high side-pose probe is not
            # transferable to the lower electrode approach, where the wrist
            # transform and nearby fixtures differ. Keep that compensation at
            # the high waypoint only; let the existing physical feedback loop
            # converge the low pose against its original target.
            descent_bias = (None if high_transit_orientation_wxyz is not None
                            else side_pose_observed_bias_xyz_m)
            if path_step_m is not None:
                current = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                poses = expand_route(current, poses, path_step_m)
            machine = motion(poses, label, precision=.005, orientation_wxyz=orientation_wxyz,
                             side_pose_observed_bias_xyz_m=descent_bias,
                             physical_tolerance_m=.005)
            if correction:
                for _ in range(max(1, round(.3 / dt))):
                    tick(machine)
            observed = [float(v) for v in obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()]
            error = [desired[i] - observed[i] for i in range(3)]
            if enforce_constraint(correction_budget.observe,error):
                return machine, observed, error

            next_z = requested[2] + error[2]
            if samples:
                prev_command, prev_observed = samples[-1]
                command_delta = requested[2] - prev_command[2]
                observed_delta = observed[2] - prev_observed[2]
                if abs(command_delta) > .0001 and abs(observed_delta) > .0001:
                    secant_z = requested[2] + error[2] * command_delta / observed_delta
                    if abs(secant_z - desired[2]) <= .02:
                        next_z = secant_z
            samples.append((requested.copy(), observed.copy()))
            requested = [requested[0] + error[0], requested[1] + error[1], next_z]
        raise RuntimeError(f"末端执行器未达到支架/安装位置公差: {json.dumps({'target_m': target, 'observed_m': observed, 'error_m': error}, ensure_ascii=False)}")

    def ee_for_grasp(position):
        ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
        grasp = obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
        return tuple(position[i] - (grasp[i]-ee[i]) for i in range(3))

    def plan_grasp_clearance_route(target, approach_z, label):
        ee=obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
        grasp=obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
        target_ee=ee_for_grasp((target[0],target[1],approach_z))
        if command.get('step_id') in ('ui_pick_right_lead','ui_connect_right_lead'):
            actual_orientation=obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist()
            orientation=active_route_orientation_wxyz or actual_orientation
            goal=ee_for_grasp(target)
            plan=enforce_constraint(search_approach,ee,goal,target_ee[2],
                runtime_policy['motion']['registered_transit_z_m'][1],
                lambda points:runtime_scene.check_path(ee,points,orientation,actual_orientation),
                lambda points:plan_live_route(env.action_manager.get_term('Actions_robot_arm_action'),
                                              points,orientation,command['step_id']))
        else:
            plan=enforce_constraint(runtime_scene.overhead_route,ee,target_ee[:2])
        offset=[grasp[i]-ee[i] for i in range(3)]
        grasp_waypoints=[tuple(float(point[i])+offset[i] for i in range(3))
                         for point in plan['waypoints_m']]
        manifest['presentation'].setdefault('motion_plans',[]).append({'action':label,**plan})
        return grasp_waypoints

    def move_grasp_exact(target, label, tolerance=.0001, approach_z=.31,
                         route_waypoints_m=None, preserve_current_orientation=False):
        global active_route_orientation_wxyz
        ee_route = ([ee_for_grasp(point) for point in route_waypoints_m]
                    if route_waypoints_m else None)
        previous_route_orientation=active_route_orientation_wxyz
        if preserve_current_orientation:
            active_route_orientation_wxyz=(obs['articulations']['robot__ee_world_quat'][0]
                .detach().cpu().tolist())
        try:
            if (preserve_current_orientation and ee_route
                    and command.get('step_id') in ('ui_pick_right_lead','ui_connect_right_lead')):
                start=obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                motion(expand_route(start,ee_route,.02),label+'/above_target',precision=.005,
                       orientation_wxyz=active_route_orientation_wxyz)
                ee_route=None
            machine, _, _ = move_exact(ee_for_grasp(target), label, tolerance=tolerance,
                                       approach_z=ee_for_grasp((target[0],target[1],approach_z))[2],
                                       route_waypoints_m=ee_route, path_step_m=.02)
        finally:
            active_route_orientation_wxyz=previous_route_orientation
        observed = obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
        error = [target[i]-observed[i] for i in range(3)]
        if max(abs(v) for v in error)>tolerance:
            raise RuntimeError('引线操作片未与实际夹持框架对准：'+str(error))
        return machine,observed,error

    def gripper_gap_m():
        values = obs['articulations']['robot__gripper_pos'][0].detach().cpu().flatten().tolist()
        if len(values) < 2:
            raise RuntimeError('未读取到 Franka 双指夹爪位置')
        # Matterix reports the mirrored second finger with the opposite sign;
        # opening is the sum of the two absolute finger displacements.
        return abs(float(values[0])) + abs(float(values[1]))

    def state_observation():
        robot = env.scene['robot']
        return {
            'source': 'matterix_physx_articulation',
            'robot': {
                'joint_names': list(robot.joint_names),
                'joint_positions': robot.data.joint_pos[0].detach().cpu().tolist(),
                'joint_velocities': robot.data.joint_vel[0].detach().cpu().tolist(),
                'end_effector_position_m': obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist(),
                'end_effector_orientation_wxyz': obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist(),
                'gripper_gap_m': gripper_gap_m(),
            },
            'semantic_components': {
                'held_pipette_id': held_pipette_id,
                'held_electrode_side': held_electrode_side,
                'held_salt_bridge': bool(held_bridge),
                'electrode_installed': dict(electrode_installed),
                'salt_bridge_connected': bool(salt_bridge_connected),
                'measurement_leads_connected': dict(lead_installed),
                'tip_attached': bool(attached.Get()),
            },
            'evidence_ref': {'trace': 'process_validation/trace.jsonl',
                             'frame_index': max(0, frames - 1)},
            'limitations': ['仿真物理引擎/语义状态观测', '不代表接触力或真实硬件实测'],
        }

    def set_gripper_open_joint_target(value):
        term = env.action_manager.get_term("Actions_robot_gripper_action")
        open_command = getattr(term, "_open_command", None)
        if open_command is None or open_command.numel() != 2:
            raise RuntimeError("当前夹爪动作项不支持设置开口目标")
        open_command[:] = float(value)

    resumed_open_target = resumed_gripper_open_target(
        bool(resume_archive),
        any((held_pipette_id is not None, held_electrode_side is not None,
             bool(held_bridge), held_lead_side is not None)))
    if resumed_open_target is not None:
        set_gripper_open_joint_target(resumed_open_target)
        gripper_command = 1.0

    def gripper_closing_axis_world():
        robot = env.scene['robot']
        names = list(robot.body_names)
        if 'panda_leftfinger' not in names or 'panda_rightfinger' not in names:
            raise RuntimeError('没有读取到夹爪双指实际位姿，停止电极抓取')
        positions = robot.data.body_pos_w[0]
        delta = positions[names.index('panda_leftfinger')] - positions[names.index('panda_rightfinger')]
        length = float(torch.linalg.norm(delta).item())
        if length <= 1e-8:
            raise RuntimeError('夹爪闭合方向无效，停止电极抓取')
        return (delta / length).detach().cpu().tolist()

    def gripper_approach_axis_world():
        if process_trace is None:
            raise RuntimeError('没有实际夹爪网格轨迹，停止电极抓取')
        try:
            return latest_approach_axis(process_trace.last_sample,
                                        frame_index=frames-1, run_id=session.run_id)
        except (OSError, ValueError, TypeError, IndexError) as exc:
            raise RuntimeError(f'没有可用的实际夹爪朝向测量，停止电极抓取: {exc}') from exc

    def correction_action_spec(step_id):
        if not correction_profile:
            return None
        return correction_profile.get('actions', {}).get(step_id)

    def correction_route_spec(step_id):
        if not correction_profile:
            return None
        return correction_profile.get('routes', {}).get(step_id)

    def verify_repair_pose(spec, label):
        if not spec.get('orientation_wxyz'):
            return
        evidence = pose_axis_evidence(spec, gripper_closing_axis_world(),
                                      gripper_approach_axis_world())
        manifest.setdefault('correction', {}).setdefault('route_pose_checks', []).append(
            {'phase': label, **evidence})
        if not evidence['passed']:
            raise RuntimeError('局部夹持姿态未达到登记的指端方向和左右闭合门禁: '
                               + json.dumps(evidence, ensure_ascii=False))

    def prepare_repair_route(spec, label):
        if not spec:
            return
        if spec.get('gripper_open_joint_target_m') is not None:
            set_gripper_open_joint_target(spec['gripper_open_joint_target_m'])
        orientation = spec.get('orientation_wxyz')
        if spec.get('direct_overhead'):
            ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
            grasp = obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
            route = ee_route_waypoints(spec, ee, grasp)
            carried=(enforce_constraint(runtime_scene.object_bounds,
                     str(root.GetPath())+'/MeasurementLead_'+held_lead_side.title())
                     if held_lead_side is not None else None)
            planned=enforce_constraint(runtime_scene.overhead_route,ee,route[-1][:2],carried)
            manifest['physical_constraints'].setdefault('planned_transfers',[]).append(
                {'phase':label,**planned,'preserves_registered_xy_target':True})
            motion(expand_route(ee,planned['waypoints_m'],.02),label+'/repair_high_route',
                   orientation_wxyz=orientation)
            verify_repair_pose(spec, label + '/repair_high_route')
            return
        if orientation:
            actual_orientation=obs['articulations']['robot__ee_world_quat'][0].detach().cpu().tolist()
            if orientation_error_deg(actual_orientation,orientation)>runtime_policy['motion']['orientation_deadband_deg']:
                motion([(.40,.00,.48)],label+'/horizontal_pose_probe',orientation_wxyz=orientation)
            verify_repair_pose(spec, label + '/horizontal_pose_probe')
        ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
        grasp = obs['articulations']['robot__grasping_frame_world_pos'][0].detach().cpu().tolist()
        route = ee_route_waypoints(spec, ee, grasp)
        motion(expand_route(ee, route, spec.get('waypoint_step_m', .035)),
               label + '/repair_high_route', orientation_wxyz=orientation)
        verify_repair_pose(spec, label + '/repair_high_route')

    def restore_full_open_at_clearance(machine):
        set_gripper_open_joint_target(.04)
        for _ in range(max(1, round(.6 / dt))):
            tick(machine)

    def set_repair_stage(stage_name, status, **details):
        correction = manifest.get("correction")
        if not correction:
            return
        for stage in correction.get("pipeline_stages", []):
            if stage.get("stage") == stage_name:
                stage.update({"status": status, **details})
                return

    def gripper_runtime_state():
        def values(value):
            return value.detach().cpu().flatten().tolist() if value is not None else None
        robot = env.scene['robot']
        term = env.action_manager.get_term('Actions_robot_gripper_action')
        return {
            'command_value': gripper_command,
            'action_term_raw': values(getattr(term, '_raw_actions', None)),
            'action_term_processed': values(getattr(term, '_processed_actions', None)),
            'joint_positions_m': values(robot.data.joint_pos[0, 7:9]),
            'joint_position_targets_m': values(getattr(robot.data, 'joint_pos_target', None)[0, 7:9]
                                               if getattr(robot.data, 'joint_pos_target', None) is not None else None),
            'observed_gripper_gap_m': gripper_gap_m()}

    if resume_archive:
        # Bind the restored articulation and mesh axes to a real initial video
        # frame before a pose-reuse decision reads the measured trace.
        current_label = 'checkpoint/restored'
        if not capture() or process_trace is None or process_trace.last_sample is None:
            raise RuntimeError('未取得检查点恢复后的同步画面与几何采样，停止局部重跑')
        restore_metadata['robot_checkpoint_restore']['initial_frame_index'] = frames - 1
        persist()

    while session.status == "running":
        command = session.prepare()
        persist()
        changed = False
        kind = command["action"]
        route_spec = correction_route_spec(command['step_id']) or {}
        active_route_orientation_wxyz = route_spec.get('orientation_wxyz')
        if (command['step_id'] in ('ui_pick_right_lead','ui_connect_right_lead')
                and active_route_orientation_wxyz is None):
            active_route_orientation_wxyz=(obs['articulations']['robot__ee_world_quat'][0]
                .detach().cpu().tolist())
        water_action = command.get("material_id") == "water"
        phase_kind = (f"{kind}_water" if water_action and kind in {"load_tip", "aspirate", "dispense", "discard_tip"}
                      else kind)
        if command.get('pipette_id') in ('gilson_pipetman_p200', 'gilson_pipetman_p1000'):
            command_profile = pipette_visual_profile(command['pipette_id'])
            command['tip_id'] = command_profile['tip_id']
            command['pipette_display_name'] = (
                'P200 / D200' if command_profile['tip_id'] == 'PIPETMAN_DIAMOND_D200'
                else 'P1000 / D1200')
        active_liquid_material = water_material if water_action else liquid_material
        UsdShade.MaterialBindingAPI.Apply(chamber.GetPrim()).Bind(active_liquid_material)
        for drop in drops:
            UsdShade.MaterialBindingAPI.Apply(drop.GetPrim()).Bind(active_liquid_material)
        print(f"COMMAND action={kind} requested={command['requested_volume_ul']} actual={command['actual_volume_ul']}", flush=True)
        if kind == "pick_pipette":
            selected_id = command.get('pipette_id')
            if selected_id not in pipette_visuals or held_pipette_id is not None:
                raise RuntimeError(f'移液器抓取状态无效: pipette_id={selected_id!r}, held={held_pipette_id!r}')
            select_pipette_visual(selected_id)
            stand_pose = (*PIPETTE_STAND_XY_BY_ID[selected_id], PIPETTE_DOCK_EE_Z_M)
            machine, observed, pose_error = move_exact(stand_pose, 'pick_pipette/align', approach_z=.34)
            gap_before_close = gripper_gap_m()
            if gap_before_close < .065:
                raise RuntimeError(f'移液器抓取前夹爪开口不足: {gap_before_close:.6f} m')
            gripper_command = -1.0
            for _ in range(max(1, round(.5 / dt))):
                tick(machine)
            gap_after_close = gripper_gap_m()
            if not pipette_grip_aligned(gap_after_close):
                raise RuntimeError(f'{command["pipette_display_name"]} 腰部夹持间距未对齐: {gap_after_close:.6f} m')
            held_pipette_id = selected_id
            pipette_parked = False
            pipette_grasp_gap_m = gap_after_close
            machine = motion([(stand_pose[0], stand_pose[1], .34)], 'pick_pipette/retract')
            manifest['presentation']['pipette_grasp'].update({
                'last_pipette_id': selected_id, 'gripper_gap_measured_m': gap_after_close,
                'stand_pose_m': list(stand_pose), 'pose_error_m': pose_error,
                'alignment_verified': True})
            changed = True
        elif kind == "load_tip":
            selected_id = command.get('pipette_id')
            if selected_id not in tip_racks:
                raise RuntimeError(f'移液动作没有受支持的枪型标识: {selected_id!r}')
            if held_pipette_id != selected_id or current_pipette_id != selected_id:
                raise RuntimeError(f'量程与当前抓取移液器不一致: requested={selected_id}, held={held_pipette_id}')
            command['tip_id'] = current_pipette_profile['tip_id']
            command['pipette_model_name'] = current_pipette_profile['model_name']
            tip_index = sum(action.kind == 'load_tip' and action.pipette_id == selected_id
                            for action in session.actions[:session.index + 1]) - 1
            if not 0 <= tip_index < len(rack_tip_prims_by_pipette[selected_id]):
                raise RuntimeError(f'{current_pipette_profile["tip_id"]} 架位已耗尽')
            rack_x, rack_y = tip_rack['tip_positions_m'][tip_index]
            dock_ee_z = tip_rack['dock_ee_z_m']
            machine = motion([(rack_x, rack_y, 0.35), (rack_x, rack_y, dock_ee_z)], phase_kind + "/dock")
            if session.includes_left_electrode:
                measured_gap = gripper_gap_m()
                if not pipette_grip_aligned(measured_gap):
                    raise RuntimeError(f'移液枪夹持几何未对齐: measured_gap={measured_gap:.6f} m')
                pipette_grasp_gap_m = measured_gap
                manifest['presentation']['pipette_grasp'].update({
                    'gripper_gap_after_first_dock_m': measured_gap,
                    'grip_waist_gap_error_m': abs(measured_gap - PIPETTE_GRIP_RADIUS_M * 2),
                    'gripper_close_command': gripper_command,
                    'geometry_alignment_verified': True})
            attached.Set(True)
            root_tip_attached.Set(True)
            rack_tip_prims_by_pipette[selected_id][tip_index].GetVisibilityAttr().Set('invisible')
            manifest['presentation']['pipette_usage'].append({
                'step_id': command['step_id'], 'pipette_id': selected_id,
                'pipette_model_name': current_pipette_profile['model_name'],
                'tip_id': current_pipette_profile['tip_id'], 'rack_slot_index': tip_index,
                'requested_volume_ul': command.get('requested_volume_ul'),
                'actual_volume_ul': command.get('actual_volume_ul'),
                'tip_length_m': current_tip_length_m,
                'tool_exchange_visual_only': False,
                'tool_exchange_method': 'separate_tool_proxy_pickup_and_park_actions'})
            changed = True
            for _ in range(max(1, round(0.7 / dt))):
                tick(machine)
            machine = motion([(rack_x, rack_y, 0.35)], phase_kind + "/retract")
        elif kind == "discard_tip":
            # Continue from the actual post-dispense pose, with no scene reset.
            machine = motion([(*WASTE_XY, .36), (*WASTE_XY, .30)], "discard_tip/position")
            ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
            start_center = (ee[0], ee[1], ee[2]
                            + PIPETTE_NOZZLE_BOTTOM_OFFSET_M-current_tip_length_m/2)
            if (not attached.Get() or
                    max(abs(ee[0]-WASTE_XY[0]), abs(ee[1]-WASTE_XY[1])) > WASTE_INNER_HALF - .004
                    or start_center[2] - current_tip_length_m/2 <= WASTE_RIM_Z):
                raise RuntimeError('弃头位置或枪头安装状态未通过核对')
            current_label = 'discard_tip/settle'
            for _ in range(max(1, round(.7 / dt))):
                tick(machine)
            attached.Set(False)
            root_tip_attached.Set(False)
            disposal.set_position(start_center)
            changed = True
            current_label = 'discard_tip/fall'
            for i in range(max(1, round(1.2 / dt))):
                disposal.set_position(falling_center(start_center,
                    (i + 1) / max(1, round(1.2 / dt)), current_tip_length_m))
                tick(machine)
            candidate = session.pending['candidate']
            display_volumes['tip'] = candidate.tip.liquid.volume_l * 1_000_000
            display_volumes['waste'] = candidate.waste.volume_l * 1_000_000
            for name in ('tip', 'waste'):
                volume_attrs[name].Set(display_volumes[name])
            manifest['tip_disposal'].update({'step_id': command['step_id'],
                'release_center_m': start_center, 'final_center_m': falling_center(start_center, 1),
                'residual_to_waste_ul': command['actual_volume_ul'], 'position_verified': True})
            current_label = 'discard_tip/settle'
            for _ in range(max(1, round(.8 / dt))):
                tick(machine)
            machine = motion([(*WASTE_XY, .36)], 'discard_tip/retract')
        elif kind in {"mix", "mix_pair"}:
            if kind == "mix_pair" and not (args.include_paired_mix and session.includes_paired_mix):
                raise RuntimeError('双池同步混匀未在本次步骤计划中启用')
            if kind == "mix" and not (args.include_left_mix and session.includes_left_mix):
                raise RuntimeError('左池混匀未在本次步骤计划中启用')
            # Clear the robot from the plate; the magnetic stirrer is the
            # instrument and no pipette motion is used as a mixing substitute.
            machine = motion([(.40, .00, .42)], 'mix/robot_clear')
            changed = True
            current_mix_duration_s = float(command.get('duration_s', session.conditions.get('mixing_duration_s', 10.0)))
            stirring_started_s = steps*dt
            current_label = 'mix/rotation'
            for _ in range(max(1, round(current_mix_duration_s / dt))):
                tick(machine)
            current_label = 'mix/settle'
            for _ in range(max(1, round(.8 / dt))):
                tick(machine)
        elif kind == "park_pipette":
            selected_id = command.get('pipette_id')
            if held_pipette_id != selected_id:
                raise RuntimeError(f'移液器停放编号与当前抓取工具不匹配: {selected_id!r}')
            changed = True
            stand_pose = (*PIPETTE_STAND_XY_BY_ID[selected_id], PIPETTE_DOCK_EE_Z_M)
            machine, observed, pose_error = move_exact(stand_pose, 'park_pipette/support', approach_z=.34)
            # Reassert the open command at the support before accepting release.
            # This also makes the measured opening explicit in the segment receipt.
            gripper_command = 1.0
            current_label = 'park_pipette/open_gripper'
            for _ in range(max(1, round(.6 / dt))):
                tick(machine)
            gap_before_release = gripper_gap_m()
            probe = gripper_runtime_state()
            if resume_archive:
                manifest['presentation'].setdefault('pipette_exchange_actions', {})['park_gripper_probe'] = probe
                print('GRIPPER_RUNTIME_PROBE=' + json.dumps(probe, ensure_ascii=False), flush=True)
            manifest['presentation'].setdefault('pipette_exchange_actions', {})['park_gripper_probe'] = {
                **probe,
                'expected_open_gap_m_min': .065}
            if abs(gap_before_release - .08) > .015:
                raise RuntimeError(f'移液器停放前夹爪未处于打开状态: command=1.0, gap={gap_before_release:.6f} m')
            pipette_parked = True
            held_pipette_id = None
            machine = motion([(.40, .00, .42)], 'park_pipette/release_and_clear')
            manifest['presentation'].setdefault('pipette_exchange_actions', {})[command['step_id']] = {
                'support_id': command['support_id'], 'pipette_id': selected_id,
                'dock_pose_m': list(stand_pose),
                'actual_ee_pose_m': observed, 'pose_error_m': pose_error,
                'gripper_gap_before_release_m': gap_before_release,
                'fixture_contact_plane_z_m': fixtures['pipette_stands'][selected_id]['collar_contact_z_m']}
        elif kind == "pick_electrode":
            if pipette_parked is not True or held_electrode_side is not None:
                raise RuntimeError('取电极前移液器未停放或夹爪状态异常')
            changed = True
            side = 'right' if command.get('target') == 'right_half_cell' else 'left'
            electrode_visual = electrode_visuals[side]
            rack_pose = tuple(electrode_visual['rack_center_m'])
            correction_spec = correction_action_spec(command['step_id'])
            if (not correction_spec
                    or correction_spec.get('gripper_open_joint_target_m') is None
                    or correction_spec.get('restore_full_open_before_pick') is True):
                # A resumed simulator may retain the previous correction's
                # 12 mm-per-finger setpoint; restore the general 40 mm target
                # before moving to an uncorrected pickup.
                set_gripper_open_joint_target(.04)
                gripper_command = 1.0
            grasp_target_offset_z = (float(correction_spec.get('top_down_grasp_target_offset_z_m', 0.0))
                                     if correction_spec else 0.0)
            grasp_target = (rack_pose[0], rack_pose[1], rack_pose[2] + grasp_target_offset_z)
            orientation = correction_spec.get('orientation_wxyz') if correction_spec else None
            top_down_pick = bool(correction_spec and correction_spec.get('top_down_vertical_descent'))
            transit_height = correction_spec['transit_height_m'] if correction_spec else None
            orientation_candidates = (correction_spec.get('orientation_candidates_wxyz')
                                      if correction_spec else None)
            staging_offset = correction_spec.get('staging_offset_xy_m') if correction_spec else None
            approach_offset = correction_spec.get('approach_offset_xy_m') if correction_spec else None
            open_joint_target = (correction_spec.get('gripper_open_joint_target_m')
                                 if correction_spec else None)
            pose_bias = (correction_spec.get('side_pose_observed_bias_xyz_m')
                         if correction_spec else None)
            high_probe_tolerance = (correction_spec.get('high_probe_physical_tolerance_m', .008)
                                    if correction_spec else .008)
            high_probe_margin = (correction_spec.get('high_probe_controller_margin_m', .012)
                                 if correction_spec else .012)
            if open_joint_target is not None:
                set_gripper_open_joint_target(open_joint_target)
                gripper_command = 1.0
            machine, observed, pose_error = move_exact(
                grasp_target, 'pick_electrode/align',
                approach_z=(correction_spec.get('top_down_approach_z_m', .32)
                            if top_down_pick else .32),
                orientation_wxyz=orientation, transit_height_m=transit_height,
                orientation_candidates_wxyz=orientation_candidates,
                staging_offset_xy_m=staging_offset,
                approach_offset_xy_m=approach_offset,
                side_pose_observed_bias_xyz_m=pose_bias,
                high_probe_physical_tolerance_m=high_probe_tolerance,
                high_probe_controller_margin_m=high_probe_margin,
                orientation_probe_bias_xyz_m=(correction_spec.get('orientation_probe_bias_xyz_m')
                                              if correction_spec else None),
                high_transit_orientation_wxyz=(correction_spec.get('high_transit_orientation_wxyz')
                                               if correction_spec else None),
                high_transit_destination_pose_bias=(correction_spec.get(
                    'high_transit_destination_pose_bias', False) if correction_spec else False),
                high_destination_position_feedback=(correction_spec.get(
                    'high_destination_position_feedback', False) if correction_spec else False),
                high_transit_waypoint_step_m=(correction_spec.get(
                    'high_transit_waypoint_step_m') if correction_spec else None),
                top_down_vertical_descent=top_down_pick,
                orientation_probe_at_pregrasp=bool(correction_spec and
                                                   correction_spec.get('orientation_probe_at_pregrasp')))
            gap_before_close = gripper_gap_m()
            if open_joint_target is not None:
                expected_open_gap = 2 * float(open_joint_target)
                if not expected_open_gap * .8 <= gap_before_close <= expected_open_gap * 1.2:
                    raise RuntimeError(f'电极抓取前夹爪开口未达到修正目标: {gap_before_close:.6f} m')
            elif gap_before_close < .065:
                raise RuntimeError(f'电极抓取前夹爪开口不足: {gap_before_close:.6f} m')
            closure_axis = gripper_closing_axis_world()
            approach_axis = gripper_approach_axis_world() if top_down_pick else None
            if correction_spec and (abs(float(closure_axis[2])) > .2
                                    or (top_down_pick and float(approach_axis[2]) > -.85)):
                raise RuntimeError('自动修正姿态未通过指端朝下、闭合轴水平检查；在夹爪闭合前停止')
            gripper_command = -1.0
            for _ in range(max(1, round(.5 / dt))):
                tick(machine)
            gap_after_close = gripper_gap_m()
            if not .007 <= gap_after_close <= .013:
                raise RuntimeError(f'电极帽夹持开口未落在 8–12 mm: {gap_after_close:.6f} m')
            held_electrode_grasp_offset_z_m = grasp_target_offset_z
            held_electrode_side = side
            if correction_spec:
                current_ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                lift_z = max(float(transit_height), float(current_ee[2]) + .01)
                machine = motion([(float(current_ee[0]), float(current_ee[1]), lift_z)],
                                 'pick_electrode/lift_clearance',
                                 orientation_wxyz=orientation,
                                 side_pose_observed_bias_xyz_m=pose_bias)
            else:
                machine = motion([(rack_pose[0], rack_pose[1], .25)], 'pick_electrode/lift',
                                 orientation_wxyz=orientation)
            manifest['presentation']['electrode_installation'].setdefault('picks', {})[side] = {
                'side': side, 'target': command['target'],
                'source': 'electrode_rack', 'component': 'copper_electrode',
                'rack_pose_m': list(rack_pose), 'actual_ee_pose_m': observed,
                'tool_grasp_reference_m': list(grasp_target),
                'grasp_target_offset_z_m': grasp_target_offset_z,
                'pose_error_m': pose_error, 'gripper_gap_before_close_m': gap_before_close,
                'gripper_gap_after_close_m': gap_after_close,
                'closing_axis_world': closure_axis,
                'gripper_approach_axis_world': approach_axis,
                'correction_candidate_id': (correction_profile or {}).get('candidate_id')}
        elif kind == 'pick_lead':
            if not salt_bridge_connected or held_lead_side is not None or held_bridge or held_electrode_side:
                raise RuntimeError('取引线前须完成盐桥并释放夹爪')
            side = command['target'].split('_')[0]
            clip = measurement_visual['clips'][side]
            rack_pose = clip['rack_center_m']
            route_spec = correction_route_spec(command['step_id']) or {}
            changed = True
            prepare_repair_route(route_spec, 'pick_lead')
            approach_z=route_spec.get('approach_z_m', .30)
            clearance_route=plan_grasp_clearance_route(rack_pose,approach_z,'pick_lead/align')
            machine, observed, pose_error = move_grasp_exact(
                rack_pose, 'pick_lead/align', approach_z=approach_z,
                route_waypoints_m=clearance_route,preserve_current_orientation=True)
            verify_repair_pose(route_spec, 'pick_lead/before_close')
            if not open_gap_verified(route_spec, gripper_gap_m()):
                raise RuntimeError('引线抓取前夹爪开口不足')
            gripper_command = -1.
            current_label = 'pick_lead/close_gripper'
            for _ in range(max(1,round(.6/dt))): tick(machine)
            gap_after_close = gripper_gap_m()
            if not .007 <= gap_after_close <= .013:
                raise RuntimeError('引线适配夹持片与夹爪间距不匹配')
            held_lead_side = side
            machine = motion([ee_for_grasp((rack_pose[0],rack_pose[1],.30))],'pick_lead/lift')
            manifest['presentation']['measurement'].setdefault('picks',{})[side] = {
                'rack_pose_m':list(rack_pose),'actual_ee_m':observed,
                'error_m':pose_error,'gripper_gap_m':gap_after_close}
        elif kind == 'connect_lead':
            side = command['target'].split('_')[0]
            if held_lead_side != side or not all(electrode_installed.values()):
                raise RuntimeError('未夹持对应侧引线，不能连接')
            clip = measurement_visual['clips'][side]
            desired = clip['target_center_m']
            route_spec = correction_route_spec(command['step_id']) or {}
            changed = True
            prepare_repair_route(route_spec, 'connect_lead')
            approach_z=route_spec.get('approach_z_m', .31)
            clearance_route=plan_grasp_clearance_route(desired,approach_z,'connect_lead/align_terminal')
            machine, observed, pose_error = move_grasp_exact(
                desired, 'connect_lead/align_terminal', tolerance=.0001,
                approach_z=approach_z,route_waypoints_m=clearance_route,
                preserve_current_orientation=True)
            verify_repair_pose(route_spec, 'connect_lead/before_release')
            electrode_center = electrode_visuals[side][side+'_cell_center_m']
            lead_check = lead_geometry(clip, observed, CELLS[command['target']], electrode_center,
                                       vessel_geometry[command['target']]['surface_z'],
                                       CELLS['left_half_cell' if side=='right' else 'right_half_cell'])
            if not all(lead_check['checks'].values()):
                raise RuntimeError('引线端子几何检查失败：'+json.dumps(lead_check))
            current_label = 'connect_lead/clamp_terminal'
            for _ in range(max(1,round(.8/dt))): tick(machine)
            # Spring jaws remain on the electrode; the robot opens only its
            # handling-tab grasp. The wire remains connected after withdrawal.
            held_lead_side = None
            set_lead_center(clip,desired,closed=True)
            lead_installed[side] = True
            gripper_command = 1.
            current_label = 'connect_lead/release'
            for _ in range(max(1,round(.6/dt))): tick(machine)
            gap_after_release = gripper_gap_m()
            if not open_gap_verified(route_spec, gap_after_release):
                raise RuntimeError('接线后机器人夹爪未释放')
            if side=='right':
                current_ee=obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                retreat=enforce_constraint(runtime_scene.overhead_route,current_ee,(.40,.00))
                manifest['presentation'].setdefault('motion_plans',[]).append(
                    {'action':'connect_lead/release_and_clear',**retreat})
                machine=motion(expand_route(current_ee,retreat['waypoints_m'],.02),
                               'connect_lead/release_and_clear')
            else:
                machine = motion([ee_for_grasp((desired[0],desired[1],.32)),(.40,.00,.42)],'connect_lead/release_and_clear')
            if route_spec.get('gripper_open_joint_target_m') is not None:
                restore_full_open_at_clearance(machine)
            manifest['presentation']['measurement'].setdefault('connections',{})[side] = {
                **lead_check,'observed_grip_m':observed,'target_grip_m':list(desired),
                'pose_error_m':pose_error,'lead_roles':command['lead_roles'],
                'gripper_gap_after_release_m':gap_after_release}
        elif kind == 'measure_voltage':
            if not all(lead_installed.values()) or held_lead_side is not None or gripper_gap_m()<.065:
                raise RuntimeError('测量前两侧引线连接与机械臂释放状态未通过')
            changed = True
            machine = motion([(.40,.00,.42)],'measure_voltage/robot_clear')
            current_label = 'measure_voltage/acquire'
            for _ in range(max(1,round(3./dt))): tick(machine)
            manifest['presentation']['measurement']['virtual_measurement'] = {
                'voltage':command['voltage'], 'no_instrument_noise_or_drift':True,
                'no_physical_settling_claim':True,'source':'existing_engine_ideal_virtual_meter'}
        elif kind == 'pick_salt_bridge':
            if not (pipette_parked and all(electrode_installed.values())) or held_electrode_side or held_bridge:
                raise RuntimeError('取盐桥前双侧电极或夹爪状态未确认')
            changed = True
            rack_pose = tuple(bridge_visual['rack_center_m'])
            route_spec = correction_route_spec(command['step_id']) or {}
            grasp_offset = float(route_spec.get('grasp_reference_offset_z_m', 0.))
            grasp_target = (rack_pose[0], rack_pose[1], rack_pose[2] + grasp_offset)
            prepare_repair_route(route_spec, 'pick_salt_bridge')
            machine, observed_ee, pose_error = move_exact(
                grasp_target, 'pick_salt_bridge/align',
                approach_z=route_spec.get('approach_z_m', .34) + grasp_offset)
            verify_repair_pose(route_spec, 'pick_salt_bridge/before_close')
            if not open_gap_verified(route_spec, gripper_gap_m()):
                raise RuntimeError('盐桥抓取前夹爪开口不足')
            gripper_command = -1.0
            current_label = 'pick_salt_bridge/close_gripper'
            for _ in range(max(1, round(.6 / dt))):
                tick(machine)
            gap_after_close = gripper_gap_m()
            if not .007 <= gap_after_close <= .013:
                raise RuntimeError('盐桥夹持帽与夹爪间距不匹配')
            held_bridge_grasp_offset_z_m = grasp_offset
            held_bridge = True
            observed = [observed_ee[0], observed_ee[1], observed_ee[2] - grasp_offset]
            machine = motion([(rack_pose[0], rack_pose[1], .29 + grasp_offset)], 'pick_salt_bridge/lift')
            manifest['presentation']['salt_bridge']['pick'] = {
                'actual_grasp_center_m': observed, 'pose_error_m': pose_error,
                'actual_ee_pose_m': observed_ee, 'grasp_reference_offset_z_m': grasp_offset,
                'gripper_gap_m': gap_after_close}
        elif kind == 'connect_salt_bridge':
            if not held_bridge or not all(electrode_installed.values()):
                raise RuntimeError('须持有盐桥且双侧电极已安装')
            changed = True
            desired = bridge_visual['target_center_m']
            route_spec = correction_route_spec(command['step_id']) or {}
            grasp_offset = held_bridge_grasp_offset_z_m
            grasp_target = (desired[0], desired[1], desired[2] + grasp_offset)
            prepare_repair_route(route_spec, 'connect_salt_bridge')
            machine, observed_ee, pose_error = move_exact(
                grasp_target, 'connect_salt_bridge/align_and_insert', tolerance=.0001,
                approach_z=route_spec.get('approach_z_m', .30) + grasp_offset)
            verify_repair_pose(route_spec, 'connect_salt_bridge/before_release')
            observed = [observed_ee[0], observed_ee[1], observed_ee[2] - grasp_offset]
            bridge_check = bridge_geometry(observed, CELLS, display_volumes)
            if not all(bridge_check['checks'].values()):
                raise RuntimeError('盐桥几何检查未通过: ' + json.dumps(bridge_check, ensure_ascii=False))
            for _ in range(max(1, round(.8 / dt))):
                tick(machine)
            gripper_command = 1.0
            for _ in range(max(1, round(.6 / dt))):
                tick(machine)
            gap_after_release = gripper_gap_m()
            if not open_gap_verified(route_spec, gap_after_release):
                raise RuntimeError('盐桥承托后夹爪未释放')
            held_bridge = False
            salt_bridge_connected = True
            set_bridge_center(bridge_visual, desired)
            machine = motion([(desired[0], desired[1], .29 + grasp_offset), (.40, .00, .42)],
                             'connect_salt_bridge/release_and_clear')
            if route_spec.get('gripper_open_joint_target_m') is not None:
                restore_full_open_at_clearance(machine)
            manifest['presentation']['salt_bridge']['placement'] = {
                **bridge_check, 'actual_grasp_center_m': observed, 'pose_error_m': pose_error,
                'actual_ee_pose_m': observed_ee, 'grasp_reference_offset_z_m': grasp_offset,
                'gripper_gap_after_release_m': gap_after_release}
        elif kind == "place_electrode":
            target_name = command['target']
            side = 'right' if target_name == 'right_half_cell' else 'left'
            if held_electrode_side != side or not pipette_parked:
                raise RuntimeError(f'安装{side}侧电极前没有确认移液器已停放且抓取了对应电极')
            changed = True
            electrode_visual = electrode_visuals[side]
            desired = electrode_visual[f'{side}_cell_center_m']
            tool_target = (desired[0], desired[1], desired[2] + held_electrode_grasp_offset_z_m)
            correction_spec = correction_action_spec(command['step_id'])
            orientation = correction_spec['orientation_wxyz'] if correction_spec else None
            transit_height = correction_spec['transit_height_m'] if correction_spec else None
            orientation_candidates = (correction_spec.get('orientation_candidates_wxyz')
                                      if correction_spec else None)
            pose_bias = (correction_spec.get('side_pose_observed_bias_xyz_m')
                         if correction_spec else None)
            high_probe_tolerance = (correction_spec.get('high_probe_physical_tolerance_m', .008)
                                    if correction_spec else .008)
            high_probe_margin = (correction_spec.get('high_probe_controller_margin_m', .012)
                                 if correction_spec else .012)
            machine, observed, pose_error = move_exact(
                tool_target, 'place_electrode/align_and_insert', approach_z=.30,
                orientation_wxyz=orientation, transit_height_m=transit_height,
                orientation_candidates_wxyz=orientation_candidates,
                side_pose_observed_bias_xyz_m=pose_bias,
                high_probe_physical_tolerance_m=high_probe_tolerance,
                high_probe_controller_margin_m=high_probe_margin,
                high_transit_orientation_wxyz=(correction_spec.get('high_transit_orientation_wxyz')
                                               if correction_spec else None),
                high_transit_waypoint_step_m=(correction_spec.get('high_transit_waypoint_step_m')
                                              if correction_spec else None),
                top_down_vertical_descent=bool(correction_spec and
                                              correction_spec.get('top_down_vertical_descent')))
            cell = CELLS[target_name]
            axis_x = cell.xy[0] + CELL_AXIS_X_OFFSET_M
            floor_z = cell.support_z + cell.floor_thickness
            observed_electrode_center = (observed[0], observed[1],
                                        observed[2] - held_electrode_grasp_offset_z_m)
            stack_geometry = electrode_stack_geometry(observed_electrode_center[2])
            plate_bottom = stack_geometry['plate_bottom_z_m']
            plate_top = stack_geometry['plate_top_z_m']
            support_surface = fixtures['left_electrode_cradle']['support_surface_z_m']
            liquid_surface_z = vessel_geometry[target_name]['surface_z']
            immersed_depth = max(0.0, min(plate_top, liquid_surface_z) - plate_bottom)
            checks = {
                f'axis_inside_{side}_cell': math.hypot(axis_x-cell.xy[0] + ELECTRODE_PLATE_M[0]/2,
                                                       ELECTRODE_PLATE_M[1]/2) < cell.inner_radius,
                'stir_bar_clearance': plate_bottom - floor_z >= .0029,
                'working_plate_immersed_in_solution': immersed_depth >= .001,
                'working_plate_meets_cap_bottom': abs(plate_top - support_surface) <= .001,
                'cap_bottom_resting_on_holder': abs(stack_geometry['cap_bottom_z_m'] - support_surface) <= .001,
                'salt_bridge_reserved': abs((cell.xy[0]+.0025)-axis_x) >= ELECTRODE_PLATE_M[0]/2 + .001,
                'cap_supported': (max(abs(value) for value in pose_error) <= .001
                                  and abs(desired[2] - ELECTRODE_CAP_M[2]/2 - support_surface) <= 1e-7)}
            if not all(checks.values()):
                raise RuntimeError(f'{side}侧电极安装几何检查未通过: ' + json.dumps(checks, ensure_ascii=False))
            open_joint_target = (correction_spec.get('gripper_open_joint_target_m')
                                 if correction_spec else None)
            if open_joint_target is not None:
                set_gripper_open_joint_target(open_joint_target)
            gripper_command = 1.0
            for _ in range(max(1, round(.6 / dt))):
                tick(machine)
            gap_after_release = gripper_gap_m()
            if open_joint_target is not None:
                expected_open_gap = 2 * float(open_joint_target)
                if not expected_open_gap * .8 <= gap_after_release <= expected_open_gap * 1.2:
                    raise RuntimeError(f'{side}侧电极释放后夹爪开口未达到修正目标: {gap_after_release:.6f} m')
            elif gap_after_release < .065:
                raise RuntimeError(f'{side}侧电极释放后夹爪仍未打开: {gap_after_release:.6f} m')
            set_electrode_center(electrode_visual, desired)
            held_electrode_side = None
            electrode_installed[side] = True
            if correction_spec:
                current_ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
                clear_z = max(float(transit_height), float(current_ee[2]) + .01)
                machine = motion([(float(current_ee[0]), float(current_ee[1]), clear_z),
                                  (.40, .00, clear_z)],
                                 'place_electrode/release_and_clearance',
                                 orientation_wxyz=orientation,
                                 side_pose_observed_bias_xyz_m=pose_bias)
            else:
                machine = motion([(desired[0], desired[1], .26), (.40, .00, .42)],
                                 'place_electrode/release_and_clear', orientation_wxyz=orientation)
            if open_joint_target is not None:
                set_gripper_open_joint_target(.04)
                gripper_command = 1.0
                for _ in range(max(1, round(.5 / dt))):
                    tick(machine)
                if gripper_gap_m() < .065:
                    raise RuntimeError(f'{side}侧电极动作结束后未恢复夹爪通用开口: {gripper_gap_m():.6f} m')
            manifest['presentation']['electrode_installation']['placements'][side] = {
                'side': side, 'component': 'copper_electrode', 'target': target_name,
                'actual_ee_pose_m': observed, 'pose_error_m': pose_error,
                'actual_electrode_center_m': list(observed_electrode_center),
                'grasp_target_offset_z_m': held_electrode_grasp_offset_z_m,
                'support_surface_z_m': support_surface,
                'electrode_tip_z_m': plate_bottom, 'electrode_plate_top_z_m': plate_top,
                'cell_floor_z_m': floor_z, 'liquid_surface_z_m': liquid_surface_z,
                'immersed_depth_m': immersed_depth,
                'liquid_depth_m': vessel_geometry[target_name]['liquid_depth'],
                'gripper_gap_after_release_m': gap_after_release,
                'correction_candidate_id': (correction_profile or {}).get('candidate_id'),
                'geometry_checks': checks,
                'salt_bridge_axis_xy_m': [cell.xy[0]+.0025, cell.xy[1]]}
        else:
            vessel_name = (command['source'] if kind == 'aspirate' else command['target'])
            geometry = vessel_geometry[vessel_name]
            xy = geometry['xy']
            work_z = geometry['aspirate_ee_z' if kind == 'aspirate' else 'dispense_ee_z']
            work_z += tool_apex_offset_m - vessel_shape.nozzle_offset_m
            # Stop near the target, then separately check actual XY alignment,
            # cone/lip clearance and height. The arm's observed vertical steady
            # offset must not be confused with horizontal aperture clearance.
            precision = geometry.get('position_tolerance_m', .015)
            end_pose = (*xy, work_z)
            if vessel_name in CELLS and kind == 'dispense':
                end_pose = compensated_target(end_pose, isaaclab_version=runtime_isaaclab_version)
            machine = motion([(*xy, 0.36), end_pose], phase_kind + "/position", precision)
            actual_ee = obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist()
            geometry = vessel_geometry_at(vessel_name, display_volumes[vessel_name])
            xy = geometry['xy']
            nozzle_z = actual_ee[2] - tool_apex_offset_m
            if math.hypot(actual_ee[0]-xy[0], actual_ee[1]-xy[1]) > geometry['radius'] * .7:
                raise RuntimeError('枪头未对准容器内部')
            max_nozzle_z = geometry.get('dispense_nozzle_max_z', geometry['rim_z'])
            if not geometry['floor_z'] - .003 <= nozzle_z <= max_nozzle_z:
                raise RuntimeError(f'枪头高度未落在容器工作区间: nozzle={nozzle_z:.6f}, floor={geometry["floor_z"]:.6f}, rim={geometry["rim_z"]:.6f}')
            if vessel_name in CELLS:
                # Check the actual cone radius at the lip, not just its centre.
                cone_at_rim = max(0., min(current_tip_radius_m,
                    (geometry['rim_z']-nozzle_z)/current_tip_length_m*current_tip_radius_m))
                horizontal_error = math.hypot(actual_ee[0]-xy[0],actual_ee[1]-xy[1])
                if (horizontal_error > geometry['horizontal_tolerance_m']
                        or horizontal_error+cone_at_rim+.0005 >= geometry['radius']
                        or nozzle_z < geometry['surface_z']+.0005):
                    raise RuntimeError('小体积池分液枪头的杯口间隙或液面距离不足: '+json.dumps({
                        'horizontal_error_m': horizontal_error,
                        'horizontal_tolerance_m': geometry['horizontal_tolerance_m'],
                        'cone_radius_at_rim_m': cone_at_rim,
                        'cell_inner_radius_m': geometry['radius'],
                        'nozzle_z_m': nozzle_z,
                        'minimum_nozzle_z_m': geometry['surface_z']+.0005,
                        'nozzle_max_z_m': max_nozzle_z,
                        'ee_position_tolerance_m': precision}, ensure_ascii=False))
            manifest.setdefault('vessel_position_checks', []).append({
                'step_id': command['step_id'], 'nozzle_xy_m': actual_ee[:2],
                'nozzle_z_m': nozzle_z, 'vessel_geometry': geometry, 'passed': True})
            candidate = session.pending["candidate"]
            start = dict(display_volumes)
            start_cell_cu = dict(display_cell_cu_mol)
            finish = {"copper_stock": candidate.containers["copper_stock"].volume_l * 1_000_000,
                      "water": candidate.containers["water"].volume_l * 1_000_000,
                      "left_half_cell": candidate.containers["left_half_cell"].volume_l * 1_000_000,
                      "right_half_cell": candidate.containers["right_half_cell"].volume_l * 1_000_000,
                      "tip": candidate.tip.liquid.volume_l * 1_000_000}
            finish_cell_cu = {cell_id: candidate.containers[cell_id].amounts_mol.get('Cu', 0.)
                              for cell_id in CELLS}
            # Slower visible dispensing; timing changes do not resample errors.
            count = max(1, round((2.5 if kind == 'dispense' else 1.5) / dt))
            current_label = phase_kind + "/volume_transfer"
            changed = True
            for i in range(count):
                dispense_progress = (i + 1) / count if kind == 'dispense' else None
                for name in finish:
                    display_volumes[name] = start[name] + (finish[name] - start[name]) * (i + 1) / count
                    volume_attrs[name].Set(display_volumes[name])
                for cell_id in CELLS:
                    display_cell_cu_mol[cell_id] = (start_cell_cu[cell_id] +
                        (finish_cell_cu[cell_id] - start_cell_cu[cell_id]) * (i + 1) / count)
                tick(machine)
            dispense_progress = None
            current_label = phase_kind + '/settle'
            for _ in range(max(1, round(.8 / dt))):
                tick(machine)
            machine = motion([(*xy, 0.36)], phase_kind + "/retract")
            for name in finish:
                if not math.isclose(volume_attrs[name].Get(), finish[name], abs_tol=1e-8):
                    raise RuntimeError("USD 语义液量与动作结果不一致")
        receipt = {name: command[name] for name in ("run_id", "command_id", "step_id", "executor")}
        receipt.update({"status": "succeeded", "simulation_time_s": steps * dt,
                        "preconditions_verified": True,
                        "evidence": {"scene_prim": str(root.GetPath()),
                                     "video_manifest": str(run_dir / "video_manifest.json"),
                                     "frame_end": frames - 1,
                                     "model": "semantic_pipetting_and_magnetic_stirring_visual_adapter_v2"}})
        if kind == "load_tip":
            receipt["tip_attached"] = bool(attached.Get())
            if session.includes_left_electrode:
                receipt.update({'pipette_grasp_alignment_verified': pipette_grasp_gap_m is not None,
                                'gripper_gap_measured_m': pipette_grasp_gap_m,
                                'grip_collar_diameter_m': PIPETTE_GRIP_RADIUS_M * 2,
                                'attachment_model': 'kinematic_visual_alignment_only'})
        elif kind == "discard_tip":
            receipt.update({'tip_attached': bool(attached.Get()), 'waste_position_verified': True,
                            'actual_volume_ul': volume_attrs['waste'].Get() - session.state.waste.volume_l * 1_000_000,
                            'actual_source': 'command_assumed'})
        elif kind == 'pick_pipette':
            receipt.update({'component_attached': 'pipette' if held_pipette_id == command['pipette_id'] else None,
                            'pipette_id': command['pipette_id'],
                            'source': command['source'], 'support_id': command['support_id'],
                            'gripper_gap_measured_m': gap_after_close,
                            'actual_source': command['actual_source'],
                            'attachment_model': 'kinematic_visual_alignment_only_no_force_validation'})
        elif kind == 'mix':
            receipt.update({"mixing_efficiency": command["mixing_efficiency"],
                            "mixing_method": command["mixing_method"],
                            "actual_source": command["mixing_efficiency_source"],
                            "liquid_volume_change_ul": 0.0})
        elif kind == 'mix_pair':
            receipt.update({"mixing_efficiencies": command["mixing_efficiencies"],
                            "mixing_method": command["mixing_method"],
                            "actual_source": command["mixing_efficiency_source"],
                            "liquid_volume_change_ul": 0.0})
        elif kind in {'pick_lead','connect_lead','measure_voltage'}:
            receipt.update(actual_source=command['actual_source'],liquid_volume_change_ul=0.,cell_output_enabled=False)
            if kind == 'pick_lead':
                receipt.update(component_attached='measurement_lead',source='measurement_lead_rack',
                               target=command['target'],gripper_gap_measured_m=gap_after_close)
            elif kind == 'connect_lead':
                receipt.update(target=command['target'],lead_roles=command['lead_roles'],
                               gripper_released=open_gap_verified(route_spec, gap_after_release),
                               geometry_checks=lead_check['checks'])
            else:
                receipt.update(lead_map=LEAD_MAP,instrument_model='ideal_virtual_ocp',voltage=command['voltage'])
        elif kind in {'pick_salt_bridge', 'connect_salt_bridge'}:
            receipt.update(actual_source=command['actual_source'], liquid_volume_change_ul=0.,
                           target='salt_bridge_holder')
            if kind == 'pick_salt_bridge':
                receipt.update(component_attached='salt_bridge', source='salt_bridge_rack',
                               gripper_gap_measured_m=gap_after_close)
            else:
                receipt.update(component_installed='salt_bridge',
                               gripper_released=open_gap_verified(route_spec, gap_after_release),
                               geometry_checks=bridge_check['checks'])
        elif kind == 'park_pipette':
            receipt.update({'pipette_parked': pipette_parked,
                            'gripper_released': gap_before_release >= .065,
                            'pipette_id': command['pipette_id'],
                            'support_id': command['support_id'],
                            'support_pose_verified': True,
                            'actual_source': command['actual_source']})
        elif kind == 'pick_electrode':
            side = 'right' if command.get('target') == 'right_half_cell' else 'left'
            receipt.update({'component_attached': 'copper_electrode' if held_electrode_side == side else None,
                            'source': 'electrode_rack',
                            'target': command['target'],
                            'gripper_opening_m': command['gripper_opening_m'],
                            'gripper_gap_measured_m': gap_after_close,
                            'actual_source': command['actual_source']})
        elif kind == 'place_electrode':
            side = 'right' if command.get('target') == 'right_half_cell' else 'left'
            receipt.update({'component_installed': 'copper_electrode' if electrode_installed[side] else None,
                            'target': command['target'],
                            'insertion_depth_m': command['insertion_depth_m'],
                            'immersed_depth_m': manifest['presentation']['electrode_installation']['placements'][side]['immersed_depth_m'],
                            'geometry_checks': checks,
                            'actual_source': command['actual_source']})
        else:
            receipt.update({"actual_volume_ul": command["actual_volume_ul"], "actual_source": "simulated_error"})
        receipt['state_observation'] = state_observation()
        session.accept_receipt(receipt)
        completed_command = dict(command)
        if kind == 'mix':
            manifest['presentation']['mixing_visual'].update({
                'status': 'completed_after_receipt',
                'efficiency_from_existing_model': command['mixing_efficiency'],
                'receipt_step_id': command['step_id'],
                'liquid_volume_change_ul': 0.0})
        elif kind == 'mix_pair':
            manifest['presentation']['mixing_visual'].update({
                'status': 'completed_after_receipt',
                'efficiencies_from_existing_model': command['mixing_efficiencies'],
                'receipt_step_id': command['step_id'],
                'liquid_volume_change_ul': 0.0})
        changed = False
        persist()
        current_label = phase_kind + '/confirmed'
        for _ in range(max(1, round(1.0 / dt))):
            tick(machine)
        manifest['presentation']['continuity']['checkpoints'][command['step_id']] = make_robot_checkpoint(
            command['step_id'], session.revision, receipt, state_observation(),
            manifest['presentation']['runtime'])
        persist()
        print(f"STEP_COMMITTED={command['step_id']} revision={session.revision}", flush=True)
        if continuation_profile:
            manifest['continuation']['executed_step_ids'].append(command['step_id'])
        elif args.resume_archive or args.stop_after_actions is not None:
            manifest['diagnostic']['executed_step_ids'].append(command['step_id'])
        if (args.stop_after_step == command['step_id'] or
                (args.stop_after_actions is not None and
                 len(manifest['diagnostic']['executed_step_ids']) >= args.stop_after_actions)):
            break
    diagnostic_run = args.stop_after_step is not None or args.stop_after_actions is not None
    current_label = "diagnostic_segment_complete" if diagnostic_run else "process_completed"
    for _ in range(max(1, round(3 / dt))):
        tick(machine)
    # Continuity evidence for the next extension: keep these cell IDs/poses,
    # resume motion from this actual end pose, and retain this same ledger.
    manifest['presentation']['continuity']['end_state'] = {
        'checkpoint_step_id': completed_command['step_id'] if completed_command else None,
        'robot_joint_positions_rad': env.scene['robot'].data.joint_pos[0].detach().cpu().tolist(),
        'robot_joint_velocities': env.scene['robot'].data.joint_vel[0].detach().cpu().tolist(),
        'end_effector_position_m': obs['articulations']['robot__ee_world_pos'][0].detach().cpu().tolist(),
        'gripper_gap_m': gripper_gap_m(),
        'held_component': session.state.cell_setup.get('held_component'),
        'tip_attached': bool(attached.Get()), 'volumes_ul': dict(display_volumes),
        'cell_cu_amounts_mol': dict(display_cell_cu_mol),
        'mixing_completed': bool(session.state.cell_setup.get('left_solution_mixed')
                                 and session.state.cell_setup.get('right_solution_mixed')),
        'right_preparation_completed': session.state.containers['right_half_cell'].volume_l > 0,
        'electrode_and_bridge_assembly_completed': bool(all(electrode_installed.values()) and salt_bridge_connected),
        'left_copper_electrode_installed': electrode_installed['left'],
        'right_electrode_installed': electrode_installed['right'], 'salt_bridge_connected': salt_bridge_connected,
        'pipette_parked': pipette_parked, 'measurement_leads_connected': all(lead_installed.values()),
        'voltage_measurement_completed': bool(session.state.cell_setup.get('voltage_measurement_completed'))}
    if correction_profile:
        executed = (manifest.get('diagnostic', {}).get('executed_step_ids', [])
                    or manifest.get('continuation', {}).get('executed_step_ids', []))
        set_repair_stage('execute', 'completed', executed_step_ids=list(executed))
    writer.close()
    writer = None
    os.replace(partial, output)
    manifest.update({"status": "segment_diagnostic_ready" if diagnostic_run else "ready",
                     "video_path": str(output),
                     "frame_count": frames, "duration_s": frames / fps})
    session.update_video("ready", str(output), frame_count=frames, fps=fps,
                         manifest_ref=str(run_dir / "video_manifest.json"),
                         recording_scope=manifest['recording_scope'],
                         source_run_id=(continuation_profile or {}).get('source_run_id'),
                         full_workflow_video=manifest['full_workflow_video'])
    persist()
    preview_terminal = "ended"
    result_kind = "SEGMENT_DIAGNOSTIC_COMPLETED" if diagnostic_run else "PROCESS_COMPLETED"
    print(f"{result_kind} run_id={session.run_id} VIDEO_SAVED={output} frames={frames}", flush=True)
except Exception as exc:
    import traceback
    print(f"RUN_FAILED={exc!r}", flush=True)
    traceback.print_exc()
    if correction_profile and callable(globals().get('set_repair_stage')):
        set_repair_stage('execute', 'failed', reason=str(exc))
    if command and session.pending and session.status == "running":
        receipt = {name: command[name] for name in ("run_id", "command_id", "step_id", "executor")}
        receipt.update({"status": "partial" if changed else "failed", "no_state_change": not changed,
                        "reason": str(exc), "simulation_time_s": steps * float(env.step_dt) if env else 0.0,
                        "evidence": {"log": "runner_exception", "model": "semantic_pipetting_proxy_v1"}})
        if env is not None and obs is not None:
            try:
                receipt['state_observation'] = state_observation()
            except Exception:
                pass
        session.accept_receipt(receipt)
    manifest.update({"status": "failed", "reason": str(exc), "frame_count": frames})
    session.update_video("failed", reason=str(exc))
    persist()
    raise
finally:
    if process_trace is not None:
        process_trace.close()
    if preview is not None:
        try:
            preview.close(preview_terminal)
        except Exception:
            pass
    if writer is not None:
        writer.close()
    if frames and manifest.get('status')=='failed' and partial.is_file():
        manifest.update(video_path=str(partial),duration_s=frames/fps,
                        recording_finalized=True,partial_recording=True)
        persist()
    if rgb is not None:
        rgb.detach()
    if close_rgb is not None:
        close_rgb.detach()
    if close_product is not None:
        close_product.destroy()
    if product is not None:
        product.destroy()
    if env is not None:
        env.close()
    if frames and manifest.get('status') in {'ready','segment_diagnostic_ready','failed'}:
        try:
            start_automatic_audit(run_dir, sys.executable)
            print('PROCESS_VALIDATION_STARTED='+session.run_id,flush=True)
        except Exception as audit_error:
            print(f'PROCESS_VALIDATION_START_ERROR={audit_error!r}',flush=True)
    # SimulationApp.close may terminate the interpreter; enqueue the independent
    # CPU audit before calling it, after the MP4/trace have been closed.
    app.close()

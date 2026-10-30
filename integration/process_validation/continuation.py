"""Read-only eligibility and provenance for continuing a checked correction."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

RUN_ID = re.compile(r"[a-f0-9]{32}")
REQUIRED_CHECKS = {
    ("records", "run_identity"), ("records", "protocol_order"),
    ("ledger", "conservation"), ("video", "timeline"),
    ("video", "full_decode"), ("video", "resolution"),
    ("video", "encoded_count"), ("sync", "trace_length"),
    ("sync", "confirmed_ledger"), ("geometry", "coverage"),
    ("geometry", "sampled_intersections"),
    ("geometry", "grasp_orientation"),
}

# A reviewed segment may advance only to the next registered physical action
# pair. This preserves a human video-review boundary between component groups.
REVIEWED_SEGMENTS = {
    "ui_place_right_copper_electrode": ("ui_pick_salt_bridge", "ui_connect_salt_bridge"),
    "ui_connect_salt_bridge": ("ui_pick_left_lead", "ui_connect_left_lead"),
    "ui_connect_left_lead": ("ui_pick_right_lead", "ui_connect_right_lead"),
}

# These exact fingerprints are compatible with the current action/scene code.
# The first value predates moving the deterministic cell-capacity assignment
# ahead of checkpoint restoration; that ordering fix does not alter any
# already-validated robot action, geometry, chemistry, or acceptance rule.
COMPATIBLE_EXECUTION_FINGERPRINTS = {
    "fee674443e7831d051bf09b97e5a099b90258a8aaa654d963013915f1ad2f264":
        "a982d5a99bcf972a9e40bd96914bef1b7d93800843bc763604fea7922fc1dad0",
}

# One user-reviewed handoff was approved after watching the left-lead video.
# It may resume from that exact checkpoint under the newly published motion
# guards, and may run only the registered right-lead pair. This is not a
# general old/new runtime equivalence; the new guards apply to every new step.
REVIEWED_GUARD_HANDOFFS = {
    "cd6fc7238a03480cbae1752e0d445717": {
        "source_execution_fingerprint": "295f22ed7978e0ea0a80313f88066c4a2b157fed9775c495ff8ec06b0917fae1",
        "target_execution_fingerprint": "00395f6d956a561de647231144e6d46c033d5a1436645edc362c7d4375def2ca",
        "checkpoint_step_id": "ui_connect_left_lead",
        "next_step_ids": ["ui_pick_right_lead", "ui_connect_right_lead"],
        "stop_after_step": "ui_connect_right_lead",
    }
}


def reviewed_guard_handoff_matches(review, source_execution, target_execution):
    if not isinstance(review, dict):
        return False
    scope=REVIEWED_GUARD_HANDOFFS.get(review.get('run_id'))
    return bool(scope
        and source_execution==scope['source_execution_fingerprint']
        and target_execution==scope['target_execution_fingerprint']
        and review.get('checkpoint_step_id')==scope['checkpoint_step_id']
        and review.get('next_step_ids')==scope['next_step_ids']
        and review.get('stop_after_step')==scope['stop_after_step'])


def execution_fingerprint(project_root=None):
    """Pin chemistry, scene, action and asset definitions, independent of paths."""
    root = Path(project_root or Path(__file__).resolve().parents[2])
    paths = []
    for relative in ("electrochemistry_simulation/electrochem_sim",
                     "electrochemistry_simulation/config", "integration/first_transfer",
                     "integration/process_validation"):
        paths.extend(p for p in (root / relative).rglob("*") if p.is_file()
                     and p.suffix in {".py", ".json"} and not p.name.startswith("test_")
                     and p.name != "continuation.py"
                     and "__pycache__" not in p.parts)
    assets = root / "integration/reference/ChemLab-Assets/assets"
    paths.extend(assets.rglob("*.glb"))
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_revisions(folder):
    """Bind a report to completed evidence without repeatedly hashing the MP4."""
    folder = Path(folder)
    result = {}
    for name in ('run.json', 'video_manifest.json', 'first_transfer.mp4',
                 'process_validation/geometry.json', 'process_validation/trace.jsonl'):
        path = folder / name
        if not path.is_file():
            continue
        stat = path.stat()
        result[name] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        if name.endswith('.json'):
            result[name]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _blocked(reason, run_id=None, status="blocked"):
    return {"run_id": run_id, "eligible": False, "status": status, "reason": reason}


def _finite(values, size):
    return (isinstance(values, list) and len(values) == size
            and all(not isinstance(v, bool) and isinstance(v, (int, float))
                    and math.isfinite(v) for v in values))


def build_continuation_plan(run_dir, report=None, *, project_root=None, reviewed_handoff=None):
    """No execution or writes. Only a successful, checked electrode segment qualifies."""
    folder = Path(run_dir).resolve()
    try:
        archive_bytes = (folder / "run.json").read_bytes()
        manifest_bytes = (folder / "video_manifest.json").read_bytes()
        archive, manifest = json.loads(archive_bytes), json.loads(manifest_bytes)
    except (OSError, ValueError):
        return _blocked("缺少完整的修正运行档案", status="not_applicable")
    snapshot = archive.get("snapshot", {})
    run_id = snapshot.get("run_id")
    correction = manifest.get("correction") or {}
    if not correction or manifest.get("continuation"):
        return _blocked("当前记录不是已完成的局部修正", run_id, "not_applicable")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or manifest.get("run_id") != run_id:
        return _blocked("修正档案和录像编号不一致", run_id)
    if manifest.get("status") != "segment_diagnostic_ready":
        return _blocked("局部修正尚未成功结束", run_id, "waiting")
    if snapshot.get("state_uncertain") is not False or archive.get("pending_command"):
        return _blocked("修正后的实验状态尚未确认", run_id)
    try:
        report_bytes = (folder / "process_validation/report.json").read_bytes()
        saved_report = json.loads(report_bytes)
        if report is not None and report != saved_report:
            return _blocked("检查报告已变化，请重新读取", run_id)
        report = saved_report
        audit_path = folder / "process_validation/audit_status.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    except (OSError, ValueError):
        return _blocked("修正已完成，等待过程校验", run_id, "waiting")
    if audit.get("status") in {"running", "failed"}:
        return _blocked("过程校验尚未成功结束，请先完成检查", run_id, "waiting")
    if report.get("run_id") != run_id:
        return _blocked("检查报告不是这次修正的结果", run_id)
    revisions = source_revisions(folder)
    if (len(revisions) != 5 or report.get('source_revisions') != revisions):
        return _blocked('修正证据已变化或旧报告未绑定证据，请重新检查这次过程', run_id)
    rows = report.get("checks", []) + report.get("issues", [])
    # The documented limitations remain visible. The diagnostic scope itself is
    # intentionally incomplete; neither of these says that an observed fault passed.
    allowed = {("records", "scope", "review"), ("coverage", "not_implemented", "uncovered")}
    reviewed_intent=(isinstance(reviewed_handoff,dict)
        and reviewed_handoff.get('schema_version')=='human_reviewed_bounded_v1'
        and reviewed_handoff.get('reviewer')=='user'
        and reviewed_handoff.get('decision')=='accepted')
    current_fingerprint = execution_fingerprint(project_root)
    reviewed_guard_handoff = (reviewed_intent and reviewed_guard_handoff_matches(
        reviewed_handoff, manifest.get('execution_fingerprint'), current_fingerprint))
    if reviewed_guard_handoff:
        allowed.add(("geometry", "cable_mechanics", "uncovered"))
    from .chemistry_checks.numbers import pending_measurement_check
    blockers = [row for row in rows if row.get("status") != "pass"
                and (row.get("group"), row.get("code"), row.get("status")) not in allowed
                and not pending_measurement_check(row, snapshot)]
    if blockers:
        messages = list(dict.fromkeys(row.get("message", "仍有未核对的检查项") for row in blockers))
        return _blocked("修正后还有问题：" + "；".join(messages[:3]), run_id)
    passed = {(row.get("group"), row.get("code")) for row in rows if row.get("status") == "pass"}
    dependency = correction.get('dependency_plan')
    proposed_plan, proposed_count = snapshot.get('plan', []), snapshot.get('completed_steps')
    end_action = (proposed_plan[proposed_count-1].get('action') if type(proposed_count) is int
                  and 0 < proposed_count <= len(proposed_plan) else None)
    required_checks = set(REQUIRED_CHECKS)
    if dependency and end_action in {'connect_salt_bridge', 'connect_lead'}:
        required_checks.discard(('geometry', 'grasp_orientation'))
        if any(spec.get('orientation_wxyz') for spec in (correction.get('routes') or {}).values()):
            required_checks.add(('geometry', 'registered_repair_pose'))
    if not required_checks.issubset(passed):
        return _blocked("修正后的记录、几何、画面同步或录像检查尚不完整", run_id)

    plan = snapshot.get("plan", [])
    completed = [event for event in archive.get("events", []) if event.get("type") == "step.completed"]
    count = snapshot.get("completed_steps")
    if (isinstance(count, bool) or not isinstance(count, int) or not 0 < count < len(plan)
            or count != snapshot.get("state_revision") or count != len(completed)
            or [e.get("step_id") for e in completed] != [s.get("step_id") for s in plan[:count]]):
        return _blocked("已确认步骤与协议前缀不一致，不能确定续跑起点", run_id)
    checkpoint = completed[-1]["step_id"]
    diagnostic = manifest.get("diagnostic", {})
    if (checkpoint != diagnostic.get("stop_after_step") or
            not diagnostic.get("executed_step_ids") or
            diagnostic["executed_step_ids"][-1] != checkpoint or
            (end_action != "place_electrode" and not (dependency and end_action in {'connect_salt_bridge', 'connect_lead'}))):
        return _blocked("修正片段没有结束在已登记的安装/连接释放确认点", run_id)
    reviewed_scope = None
    if reviewed_handoff is not None:
        # A human can accept an independently checked segment and request one
        # explicitly bounded next segment, while effect attribution stays
        # inconclusive. Ordinary automatic continuation never supplies this.
        expected_next = REVIEWED_SEGMENTS.get(checkpoint)
        next_steps = [row.get('step_id') for row in plan[count:count + len(expected_next or ())]]
        if (not isinstance(reviewed_handoff, dict)
                or reviewed_handoff.get('schema_version') != 'human_reviewed_bounded_v1'
                or reviewed_handoff.get('reviewer') != 'user'
                or reviewed_handoff.get('decision') != 'accepted'
                or not isinstance(reviewed_handoff.get('review_message'), str)
                or not reviewed_handoff['review_message'].strip()
                or not isinstance(reviewed_handoff.get('continuation_request'), str)
                or not reviewed_handoff['continuation_request'].strip()
                or reviewed_handoff.get('run_id') != run_id
                or reviewed_handoff.get('checkpoint_step_id') != checkpoint
                or reviewed_handoff.get('reviewed_step_ids') != diagnostic['executed_step_ids']
                or not expected_next
                or next_steps != list(expected_next)
                or reviewed_handoff.get('next_step_ids') != list(expected_next or ())
                or reviewed_handoff.get('stop_after_step') != (expected_next[-1] if expected_next else None)
                or reviewed_handoff.get('source_revisions') != revisions
                or reviewed_handoff.get('report_sha256') != hashlib.sha256(report_bytes).hexdigest()):
            return _blocked('人工确认或限定的下一段与当前证据不一致', run_id)
        from .repairs.effects.version_compatibility import _checker
        current_root = Path(project_root or Path(__file__).resolve().parents[2])
        if report.get('checker_fingerprint') != _checker(current_root):
            return _blocked('人工确认前的报告需由当前校验器重新检查', run_id)
        reviewed_scope = {'mode': 'human_reviewed_bounded_segment',
                          'reviewed_run_id': run_id, 'next_step_ids': next_steps,
                          'stop_after_step': next_steps[-1],
                          'segment_label': ' + '.join(next_steps),
                          'automatic_continuation_allowed': False}
    if dependency:
        from .repairs.effects.storage import evaluate_repair
        effect = evaluate_repair(folder, report)
        if effect.get('status') != 'improved' or not effect.get('eligible_for_continuation'):
            reasons = effect.get('reasons', [effect.get('status', '')])
            version_only = (effect.get('status') == 'inconclusive' and reasons == [
                '检查器/策略版本不一致或旧报告缺少版本证据，不能直接归因于修复'])
            if reviewed_scope is None or not version_only:
                return _blocked('修复前后效果尚未独立确认改善：' + '；'.join(reasons), run_id)
            reviewed_scope['repair_effect_status'] = 'inconclusive'
            reviewed_scope['repair_effect_reason'] = reasons[0]
    receipt = completed[-1].get("payload", {}).get("executor_receipt", {})
    geometry = receipt.get("geometry_checks")
    if receipt.get("status") != "succeeded" or not geometry or not all(v is True for v in geometry.values()):
        return _blocked("电极安装回执或末端几何尚未通过", run_id)
    end = manifest.get("presentation", {}).get("continuity", {}).get("end_state", {})
    setup = snapshot.get("state", {}).get("cell_setup", {})
    if (end.get("checkpoint_step_id") != checkpoint or
            not _finite(end.get("robot_joint_positions_rad"), 9) or
            not _finite(end.get("end_effector_position_m"), 3) or
            not _finite(end.get("robot_joint_velocities"), 9)):
        return _blocked("缺少修正后的机械臂末态，不能接着运行", run_id)
    if (max(abs(v) for v in end["robot_joint_velocities"][:7]) > .05 or
            max(abs(v) for v in end["robot_joint_velocities"][7:]) > .005):
        return _blocked("修正结束时机械臂尚未停稳", run_id)
    gap = end.get("gripper_gap_m")
    if (not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < .065
            or end.get("held_component") is not None or not end.get("pipette_parked")
            or end.get("tip_attached") is not False or setup.get("held_component") is not None
            or setup.get("active_pipette_id") is not None or not setup.get("pipette_parked")
            or snapshot.get("state", {}).get("tip", {}).get("attached") is not False):
        return _blocked("工具尚未释放或停放状态没有确认", run_id)
    for side, key in (("left", "left_copper_electrode_installed"), ("right", "right_electrode_installed")):
        if bool(setup.get(side + "_electrode_placed")) != bool(end.get(key)):
            return _blocked("安装状态与场景末态不一致", run_id)
    if dependency and (bool(setup.get('salt_bridge_connected')) != bool(end.get('salt_bridge_connected'))
            or bool(setup.get('measurement_leads_connected')) != bool(end.get('measurement_leads_connected'))):
        return _blocked('盐桥/引线连接状态与场景末态不一致', run_id)
    if snapshot.get("conservation", {}).get("passed") is not True:
        return _blocked("修正后的物料守恒未确认", run_id)
    state = snapshot['state']
    stores = {**state['containers'], 'tip': state['tip']['liquid'], 'waste': state['waste']}
    shown = end.get('volumes_ul') or {}
    if any(name not in shown or not isinstance(shown[name], (int, float))
           or not math.isfinite(shown[name])
           or not math.isclose(shown[name], store['volume_ul'], rel_tol=1e-10, abs_tol=1e-8)
           for name, store in stores.items()):
        return _blocked('场景末态液量与确认账本不一致', run_id)
    source_fingerprint = manifest.get("execution_fingerprint")
    compatible_fingerprint = COMPATIBLE_EXECUTION_FINGERPRINTS.get(source_fingerprint)
    if (source_fingerprint != current_fingerprint and
            compatible_fingerprint != current_fingerprint):
        from .repairs.effects.version_compatibility import prove_compatible, _checker
        current_root = Path(project_root or Path(__file__).resolve().parents[2])
        try:
            proof = prove_compatible(
                {'manifest': manifest, 'report': report},
                {'manifest': {'execution_fingerprint': current_fingerprint},
                 'report': {'checker_fingerprint': _checker(current_root)}})
        except (OSError, ValueError, TypeError):
            proof = None
        if not proof and not reviewed_guard_handoff_matches(
                reviewed_handoff,source_fingerprint,current_fingerprint):
            return _blocked("执行代码或场景已更新，需要按当前版本重新完成局部修正和校验", run_id)

    parameters = correction.get("parameters") or {}
    source_side = correction.get("side") or plan[count - 1].get("target", "").split("_")[0]
    source_pick = parameters.get(f"ui_pick_{source_side}_copper_electrode")
    source_place = parameters.get(f"ui_place_{source_side}_copper_electrode")
    if (not source_pick or not source_place) and not dependency:
        return _blocked("没有保存已复验的电极修正参数", run_id)
    actions = deepcopy(parameters)
    # Keep only parameters actually validated on the completed segment.
    routes = deepcopy(correction.get('routes') or {})
    if dependency:
        from .repairs.profile import validate_repair_profile
        try:
            validate_repair_profile({'source_run_id': run_id, 'actions': actions, 'routes': routes}, run_id)
        except ValueError as exc:
            return _blocked('已复验参数无效：' + str(exc), run_id)
    root_run_id = correction.get("root_run_id") or correction.get("source_run_id")
    signature = hashlib.sha256(archive_bytes + manifest_bytes + report_bytes + current_fingerprint.encode()).hexdigest()
    return {"run_id": run_id, "source_run_id": run_id, "root_run_id": root_run_id,
            "eligible": True, "status": "ready_reviewed_segment" if reviewed_scope else "ready",
            "reason": (f"用户已确认上一局部画面，仅可执行下一段：{reviewed_scope['segment_label']}，不自动续接后缀"
                       if reviewed_scope else "修正已完成，当前可用检查已通过，可以继续剩余实验"),
            "checkpoint_step_id": checkpoint, "expected_revision": count,
            "checkpoint_signature": signature, "remaining_steps": len(plan) - count,
            "correction_profile": {"source_run_id": run_id, "root_run_id": root_run_id,
                "side": source_side, "candidate_id": correction.get("candidate_id"),
                "attempt_index": correction.get("attempt_index"),
            "repair_contract": correction.get("repair_contract"),
                "attempts_by_side": correction.get("attempts_by_side", {source_side: correction.get("attempt_index", 1)}),
                "inherited_for_continuation": True, "actions": actions, "routes": routes},
            "coverage_limits": [row["message"] for row in rows if row.get("group") == "coverage"],
            "reviewed_handoff": reviewed_scope}

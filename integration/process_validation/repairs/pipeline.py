"""Orchestrate classify -> strategy -> bounded local segment planning."""
import json
import re
from pathlib import Path

from .classifier import classify_failures
from .models import RepairTarget, blocked
from .evidence import validate_report_binding
from .profile import validate_repair_profile
from .registry import route_for
from .dependency_plan import build_dependency_plan
from .sequence import select_cohort, scoped_electrode_report, sequence_metadata
from .failed_retry import retryable_timeout
from .strategies.top_down_grasp import (retryable_top_down_fork_collision,
                                        retryable_top_down_release_gap)
from .lineage_queue import previous_observations
from .effects.fingerprints import candidate_fingerprint
from .effects.history import repeated_candidate
from .effects.storage import evaluate_repair
from .strategies.palm_clearance import (supports_opposed_lead_approach,
                                       predict_opposed_hand_clearance)


def build_repair_plan(run_dir, report=None):
    folder = Path(run_dir).resolve()
    try:
        archive = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        manifest = json.loads((folder / "video_manifest.json").read_text(encoding="utf-8"))
        saved_report = json.loads(
            (folder / "process_validation" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return blocked(folder.name, "缺少可读取的运行档案、录像清单或校验报告。", "unavailable")
    if not all(isinstance(value, dict) for value in (archive, manifest, saved_report)):
        return blocked(folder.name, "运行档案、清单或校验报告结构无效。")
    run_id = archive.get("snapshot", {}).get("run_id")
    if run_id != folder.name or manifest.get("run_id") != run_id:
        return blocked(run_id, "运行档案、录像清单和目录编号不一致。")
    if report is not None and report != saved_report:
        return blocked(run_id, "传入报告与已保存报告不一致，请重新读取。")
    binding_failure = validate_report_binding(folder, saved_report, run_id)
    if binding_failure:
        return binding_failure
    previous_dependency = ((manifest.get('correction') or {}).get('dependency_plan')
                           if not manifest.get('continuation') else None)
    if previous_dependency:
        effect = evaluate_repair(folder, saved_report)
        if not effect.get('comparable'):
            return blocked(run_id, '上轮修复缺少可比较的独立证据：' + '；'.join(effect.get('reasons', [])))
        retryable_fork_collision = retryable_top_down_fork_collision(
            effect, archive, manifest, saved_report, folder)
        retryable_release_gap = retryable_top_down_release_gap(
            effect, archive, manifest, saved_report, folder)
        if (effect.get('status') == 'regressed'
                and not retryable_timeout(effect, archive, manifest)
                and not retryable_fork_collision
                and not retryable_release_gap):
            return blocked(run_id, '上轮修复引入新失败或破坏已确认前缀，停止自动链并保留对比证据。')
    targets, failure = classify_failures(saved_report, run_id, manifest=manifest, archive=archive)
    if failure:
        return failure
    try:
        targets, deferred = select_cohort(archive, targets)
    except (ValueError, TypeError, AttributeError) as exc:
        return blocked(run_id, '不能确定实验顺序：' + str(exc))
    sequence = sequence_metadata(targets, deferred)
    if not deferred:
        observed = previous_observations(folder, archive, manifest, targets)
        if observed is not None:
            sequence['deferred_step_ids'] = observed
            sequence['deferred_source'] = 'root_report_historical_observations'
        elif (manifest.get('correction') or {}).get('root_run_id'):
            sequence['deferred_source'] = 'unavailable_revalidate_remaining_workflow'
    families = {target.family for target in targets}
    if "electrode" in families:
        # Keep the established evidence-heavy electrode strategy independent.
        from ..correction import build_correction_plan
        result = build_correction_plan(folder, scoped_electrode_report(saved_report, targets))
        result['repair_sequence'] = sequence
        if result.get('eligible'):
            return _finalize(folder, archive, result, [target.step_id for target in targets])
        return result

    plan = archive.get("snapshot", {}).get("plan", [])
    step_ids = [row.get("step_id") for row in plan]
    if not step_ids or any(not isinstance(step, str) for step in step_ids) or len(set(step_ids)) != len(step_ids):
        return blocked(run_id, "来源协议步骤编号缺失或重复。")
    completed_events = [event for event in archive.get("events", [])
                        if event.get("type") == "step.completed"]
    completed_ids = [event.get("step_id") for event in completed_events]
    if (completed_ids != step_ids[:len(completed_ids)]
            or len(completed_ids) > len(step_ids)
            or archive['snapshot'].get('completed_steps') != len(completed_ids)
            or archive['snapshot'].get('state_revision') != len(completed_ids)):
        return blocked(run_id, "成功回执不是连续且一致的确认前缀，不能规划检查点。")
    target_ids = {target.step_id for target in targets}
    if not target_ids.issubset(set(step_ids)):
        return blocked(run_id, "失败步骤不在来源运行的协议中。")
    opposed_approach = supports_opposed_lead_approach(targets, manifest)
    diagonal_approach = (manifest.get('correction', {}).get('strategy_family') == 'lead_palm_clearance'
                         and manifest.get('motion_failure', {}).get('phase') == 'pick_lead/repair_high_route'
                         and all(target.family == 'measurement_lead' for target in targets))
    cohort = ('measurement_lead_pose' if opposed_approach or any(
        target.family == 'measurement_lead' and any(issue.get('code') == 'execution_failed' for issue in target.issues)
        for target in targets) else None)
    try:
        dependency = build_dependency_plan(archive, list(target_ids), cohort=cohort)
    except (ValueError, TypeError, KeyError) as exc:
        return blocked(run_id, '局部依赖无法确认：' + str(exc))
    route_targets = {target.step_id: target for target in targets}
    for step_id in dependency['parameter_step_ids']:
        if step_id not in route_targets:
            action = plan[step_ids.index(step_id)]['action']
            family = 'measurement_lead' if action in {'pick_lead', 'connect_lead'} else 'salt_bridge'
            origin = next(target for target in targets if target.family == family)
            route_targets[step_id] = RepairTarget(step_id, family, origin.issues)
    checkpoint_step = dependency['checkpoint_step_id']

    previous_correction = {} if manifest.get('continuation') else manifest.get("correction", {})
    previous_attempts = [
        (0 if manifest.get('continuation') else
         (manifest.get("repair_strategy_attempts") or {}).get("fixture_clearance_route", 0)),
        previous_correction.get("attempt_index", 0),
    ]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in previous_attempts):
        return blocked(run_id, "来源修复次数记录无效，不能选择下一候选。")
    attempt = 1 + max(previous_attempts)
    max_attempts = (4 if (opposed_approach and attempt == 4)
                    or previous_correction.get('strategy_family') == 'lead_palm_clearance' else 3)
    if (diagonal_approach and attempt == 5) or previous_correction.get('strategy_family') == 'lead_diagonal_palm_clearance':
        max_attempts = 5
    if attempt > max_attempts:
        return blocked(run_id, f"局部已登记候选已达到 {max_attempts} 次上限；保留证据并停止自动调整。", "limit_reached")
    candidate_preflight = None
    if opposed_approach and attempt == 4:
        candidate_preflight = predict_opposed_hand_clearance(folder, manifest)
        if not candidate_preflight.get('passed'):
            return blocked(run_id, candidate_preflight.get('reason')
                           or '手掌反向候选的测量网格预检仍有相交，停止启动。')
    if diagonal_approach and attempt == 5:
        reference_id = previous_correction.get('source_run_id', '')
        if not isinstance(reference_id, str) or not re.fullmatch('[a-f0-9]{32}', reference_id):
            return blocked(run_id, '缺少原水平夹持的测量来源，不能规划斜向候选。')
        reference_dir = folder.parent / reference_id
        try:
            reference_manifest = json.loads((reference_dir / 'video_manifest.json').read_text(encoding='utf8'))
        except (OSError, ValueError):
            return blocked(run_id, '无法读取原水平夹持的测量档案，不能规划斜向候选。')
        if reference_manifest.get('run_id') != reference_id:
            return blocked(run_id, '原水平夹持测量来源编号不一致。')
        candidate_preflight = predict_opposed_hand_clearance(reference_dir, reference_manifest, 135.)
        if not candidate_preflight.get('passed'):
            return blocked(run_id, candidate_preflight.get('reason')
                           or '斜向水平夹持的测量网格预检仍有相交，停止启动。')
    routes = {}
    for target in route_targets.values():
        try:
            route = route_for(target, manifest, attempt)
        except (ValueError, TypeError, OverflowError):
            route = None
        if not route:
            return blocked(run_id, f"{target.step_id} 缺少可验证的目标位置，不能生成路线候选。")
        routes[target.step_id] = route
    try:
        validate_repair_profile({'source_run_id': run_id, 'routes': routes}, run_id)
    except ValueError as exc:
        return blocked(run_id, f"候选未通过执行参数契约：{exc}")
    completed_events = [event for event in archive.get("events", [])
                        if event.get("type") == "step.completed"]
    revision = next((i for i, event in enumerate(completed_events, 1)
                     if event.get("step_id") == checkpoint_step), None)
    if revision is None:
        return blocked(run_id, "无法确定恢复检查点的账本版本。")
    evidence = [{key: issue[key] for key in
                 ("group", "code", "status", "step_id", "message", "frame_index",
                  "last_frame_index", "video_time_s", "body", "obstacle") if key in issue}
                for target in targets for issue in target.issues]
    candidate_id = ("lead_diagonal_horizontal_grasp_v1" if diagonal_approach and attempt == 5 else
                    "lead_palm_away_from_cell_support_v1" if opposed_approach and attempt == 4
                    else f"fixture_clearance_route_v{attempt}")
    fault_step_ids = [row for row in step_ids if row in target_ids]
    result = {
        "run_id": run_id, "source_run_id": run_id,
        "root_run_id": archive.get("snapshot", {}).get("state_ledger", {})
                          .get("lineage", {}).get("root_run_id") or run_id,
        "eligible": True, "status": "ready",
        "reason": "失败项已关联到受支持的局部路径策略，可从最近成功检查点复演并独立复查。",
        "candidate_id": candidate_id,
        "strategy_family": ("lead_diagonal_palm_clearance" if diagonal_approach and attempt == 5 else
                            "lead_palm_clearance" if opposed_approach and attempt == 4 else "fixture_clearance_route"),
        "attempt_index": attempt, "max_attempts": max_attempts,
        "candidate_preflight": candidate_preflight,
        "repair_sequence": sequence,
        "fault_step_id": fault_step_ids[0], "fault_step_ids": fault_step_ids,
        "repair_step_ids": [step_id for step_id in step_ids if step_id in route_targets],
        "checkpoint_step_id": checkpoint_step, "checkpoint_revision": revision,
        "stop_after_step": dependency['stop_after_step'], "actions": {}, "routes": routes,
        "dependency_plan": dependency,
        "pipeline_stages": [
            {"stage": "detect", "status": "completed", "source": "independent_process_validation",
             "report_run_id": run_id, "policy_id": saved_report.get("policy_id")},
            {"stage": "classify", "status": "completed", "target_step_ids": fault_step_ids,
             "families": sorted(families)},
            {"stage": "solve", "status": "candidate_ready", "candidate_id": candidate_id,
             "attempt_index": attempt},
            {"stage": "execute", "status": "pending"},
            {"stage": "check", "status": "pending", "checker": "independent_process_validation"},
        ],
        "fault_evidence": evidence,
        "preserve": ["conditions", "random_seed", "accepted_receipts",
                     "chemical_state_at_checkpoint", "chemical_model_state_at_checkpoint",
                     "independent_source_archive", "repair_targets"],
    }
    return _finalize(folder, archive, result, fault_step_ids)


def _finalize(folder, archive, result, fault_ids):
    try:
        dependency = result.get('dependency_plan') or build_dependency_plan(
            archive, list(result.get('actions', {})) + list(result.get('routes', {})))
        result.update(dependency_plan=dependency, checkpoint_step_id=dependency['checkpoint_step_id'],
                      checkpoint_revision=dependency['checkpoint_revision'],
                      stop_after_step=dependency['stop_after_step'], fault_step_ids=fault_ids,
                      repair_step_ids=dependency['parameter_step_ids'])
        result['proposal_fingerprint'] = candidate_fingerprint(result, archive)
        history = repeated_candidate(folder, result, archive)
        result['candidate_history'] = history
        if history['blocked']:
            return blocked(archive['snapshot']['run_id'], history['reason'], 'repeated_or_invalid_history')
        return result
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return blocked(archive['snapshot']['run_id'], '局部依赖或方案历史校验失败：' + str(exc))

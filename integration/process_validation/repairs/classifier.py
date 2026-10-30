"""Map independent audit findings to an action-specific repair family."""
from collections import defaultdict
import json
import math

from .models import RepairTarget, blocked
from .evidence import report_rows


STEP_FAMILIES = {
    "ui_pick_salt_bridge": "salt_bridge",
    "ui_connect_salt_bridge": "salt_bridge",
    "ui_pick_left_lead": "measurement_lead",
    "ui_pick_right_lead": "measurement_lead",
    "ui_connect_left_lead": "measurement_lead",
    "ui_connect_right_lead": "measurement_lead",
    "ui_pick_left_copper_electrode": "electrode",
    "ui_place_left_copper_electrode": "electrode",
    "ui_pick_right_copper_electrode": "electrode",
    "ui_place_right_copper_electrode": "electrode",
}


def _event_reason(event):
    payload = event.get('payload', {})
    return payload.get('reason') or payload.get('executor_receipt', {}).get('reason')


def _measured_timeout_matches(issue, step_id, failed_events, failure):
    """A phase label alone cannot establish which failed action a pose belongs to."""
    if not any(row.get('step_id') == step_id and _event_reason(row) == issue.get('message')
               for row in failed_events):
        return False
    message = issue.get('message', '')
    try:
        measured, _ = json.JSONDecoder().raw_decode(message[message.index('{'):])
    except (ValueError, TypeError):
        return False
    if not isinstance(measured, dict):
        return False
    if any(measured.get(key) != failure.get(key) for key in ('phase', 'failed_waypoint_index')):
        return False
    for key in ('actual_ee_position_m', 'failed_waypoint_position_m'):
        vector = measured.get(key)
        if (not isinstance(vector, list) or len(vector) != 3
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in vector)
                or any(not math.isclose(a, b, abs_tol=1e-9, rel_tol=1e-10)
                       for a, b in zip(vector, failure[key]))):
            return False
    return True


def classify_failures(report, run_id, *, manifest=None, archive=None):
    grouped = defaultdict(list)
    unsupported = []
    manifest, archive = manifest or {}, archive or {}
    failed_events = [row for row in archive.get('events', []) if row.get('type') == 'step.failed']
    try:
        rows = report_rows(report)
    except ValueError as exc:
        return None, blocked(run_id, str(exc))
    if report.get('run_id') != run_id:
        return None, blocked(run_id, '校验报告与分类对象运行编号不一致。')
    scope_failures = []
    for issue in rows:
        if issue.get("status") != "fail":
            continue
        if issue.get('group') == 'records' and issue.get('code') == 'scope':
            scope_failures.append(issue)
            continue
        step_id = issue.get("step_id")
        if not step_id and issue.get('group') == 'records' and issue.get('code') == 'execution_failed':
            matches = {row.get('step_id') for row in failed_events
                       if _event_reason(row) == issue.get('message') and row.get('step_id')}
            if len(matches) == 1:
                step_id = matches.pop()
                issue = {**issue, 'step_id': step_id}
        family = STEP_FAMILIES.get(step_id)
        if family == "electrode":
            # Preserve the established electrode planner's broader evidence
            # gates; it owns classification for its supported pose/path cases.
            grouped[step_id].append(issue)
            continue
        failure = manifest.get('motion_failure', {})
        prior_pose = manifest.get('correction', {}).get('routes', {}).get(step_id, {})
        finite_positions = all(isinstance(failure.get(key), list) and len(failure[key]) == 3
                               and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                                       for v in failure[key])
                               for key in ('actual_ee_position_m', 'failed_waypoint_position_m'))
        known_unreachable_pose = (
            failure.get('phase') == 'pick_lead/align'
            and failure.get('failed_waypoint_index') == 0
            and prior_pose.get('expected_approach_axis_world') == [0., 0., -1.]
        ) or (
            failure.get('phase') == 'pick_lead/repair_high_route'
            and manifest.get('correction', {}).get('strategy_family') == 'lead_palm_clearance'
            and prior_pose.get('expected_approach_axis_world') == [0., 1., 0.]
        )
        if (family == 'measurement_lead' and step_id.startswith('ui_pick_')
                and issue.get('group') == 'records' and issue.get('code') == 'execution_failed'
                and known_unreachable_pose
                and finite_positions
                and _measured_timeout_matches(issue, step_id, failed_events, failure)
                and archive.get('snapshot', {}).get('state', {}).get('cell_setup', {}).get('held_component') is None
                and '机械臂动作超时' in issue.get('message', '')):
            grouped[step_id].append(issue)
            continue
        if not family or issue.get("group") != "geometry" or issue.get("code") != "fixture_intersection":
            unsupported.append(issue)
            continue
        grouped[step_id].append(issue)
    # An incomplete run's scope failure is a consequence of an evidenced
    # execution failure, not a separate route repair or permission to ignore
    # unrelated failed checks. Keep it in the original report.
    if scope_failures and not any(issue.get('code') == 'execution_failed'
                                  for issues in grouped.values() for issue in issues):
        unsupported.extend(scope_failures)
    if unsupported:
        first = unsupported[0]
        return None, blocked(run_id,
            f"当前没有适用于 {first.get('step_id') or '未绑定步骤'} / "
            f"{first.get('group')}:{first.get('code')} 的修复策略。")
    if not grouped:
        return None, blocked(run_id, "报告中没有可修复的失败项。", "not_applicable")
    targets = [RepairTarget(step_id, STEP_FAMILIES[step_id], tuple(rows))
               for step_id, rows in grouped.items()]
    return targets, None

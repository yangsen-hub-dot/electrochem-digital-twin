"""Pure before/after evaluation. Absence of a failure alone is not resolution."""
from copy import deepcopy

from .identities import failures, describe, bound_step
from ..dependency_plan import checkpoint_result, checkpoint_signature, released_state
from ..evidence import report_rows
from ...chemistry_checks.numbers import pending_measurement_check


def compare_records(before, after):
    """Inputs contain archive, manifest, report; filesystem binding is checked by storage."""
    a, b = before['archive'], after['archive']
    ar, br = before['report'], after['report']
    version_evidence = None
    if (ar.get('checker_fingerprint') != br.get('checker_fingerprint')
            or before['manifest'].get('execution_fingerprint') != after['manifest'].get('execution_fingerprint')):
        from .version_compatibility import prove_compatible
        version_evidence = prove_compatible(before, after)
    correction = after['manifest'].get('correction') or {}
    contract = correction.get('repair_contract') or {}
    dependency = correction.get('dependency_plan') or contract.get('dependency_plan') or {}
    source_id = a.get('snapshot', {}).get('run_id')
    result = {'schema_version': '1.0', 'source_run_id': source_id,
              'run_id': b.get('snapshot', {}).get('run_id'), 'status': 'inconclusive',
              'resolved': [], 'remaining': [], 'new_failures': [], 'unverified': [],
              'preservation_violations': [], 'reasons': [], 'eligible_for_continuation': False}
    result['comparable'] = False
    if correction.get('source_run_id') != source_id or contract.get('source_run_id') != source_id:
        result['reasons'].append('修复分支未绑定到所比较的来源运行')
        return result
    if (ar.get('policy_id') != br.get('policy_id') or not ar.get('checker_fingerprint')
            or (ar.get('checker_fingerprint') != br.get('checker_fingerprint') and not version_evidence)
            or not ar.get('policy_fingerprint') or ar.get('policy_fingerprint') != br.get('policy_fingerprint')):
        result['reasons'].append('检查器/策略版本不一致或旧报告缺少版本证据，不能直接归因于修复')
        return result
    execution = before['manifest'].get('execution_fingerprint')
    if not execution or (execution != after['manifest'].get('execution_fingerprint') and not version_evidence):
        result['reasons'].append('执行/场景版本不一致或缺失，不能直接归因于参数修复')
        return result
    if not dependency or type(dependency.get('checkpoint_revision')) is not int:
        result['reasons'].append('缺少明确的局部重跑依赖范围')
        return result
    result['comparable'] = True
    result['version_evidence'] = version_evidence
    first = dependency['checkpoint_revision']
    for field in ('config', 'seed'):
        if a.get(field) != b.get(field): result['preservation_violations'].append(field)
    for field in ('conditions', 'plan'):
        if a.get('snapshot', {}).get(field) != b.get('snapshot', {}).get(field):
            result['preservation_violations'].append(field)
    try:
        for revision in range(1, first+1):
            if checkpoint_signature(checkpoint_result(a, revision)) != checkpoint_signature(checkpoint_result(b, revision)):
                result['preservation_violations'].append('confirmed_prefix:' + str(revision))
    except (ValueError, KeyError, TypeError):
        result['preservation_violations'].append('missing_confirmed_prefix')
    old, new = failures(ar, a), failures(br, b)
    targets = set(contract.get('fault_step_ids') or correction.get('fault_step_ids') or [])
    original = {key for key in old if key[2] in targets}
    executed = after['manifest'].get('diagnostic', {}).get('executed_step_ids', [])
    expected = dependency.get('rerun_step_ids', [])
    completed = {e.get('step_id') for e in b.get('events', []) if e.get('type') == 'step.completed'}
    passed = {(r['group'], r['code']) for r in report_rows(br) if r['status'] == 'pass'}
    captured = {frame.get('step_id') for frame in after['manifest'].get('frames', [])}
    diagnostic = after['manifest'].get('diagnostic', {})
    end_confirmed = (b.get('snapshot', {}).get('state_uncertain') is False
                     and after['manifest'].get('status') == 'segment_diagnostic_ready'
                     and not b.get('pending_command')
                     and diagnostic.get('stop_after_step') == dependency.get('stop_after_step')
                     and diagnostic.get('restored_after_step') == dependency.get('checkpoint_step_id')
                     and diagnostic.get('prior_actions_replayed') is False
                     and released_state(b.get('snapshot', {}).get('state', {}))
                     and b.get('snapshot', {}).get('completed_steps') == first + len(expected)
                     and b.get('snapshot', {}).get('state_revision') == first + len(expected))
    ordered_completed = [e.get('step_id') for e in b.get('events', []) if e.get('type') == 'step.completed']
    if ordered_completed != dependency.get('preserved_prefix_step_ids', []) + expected:
        end_confirmed = False
    unverified, resolved = set(), set()
    for key in original - new.keys():
        coverage = ({('geometry', 'coverage'), ('geometry', 'sampled_intersections'),
                     ('sync', 'trace_length'), ('sync', 'confirmed_ledger')}
                    if key[0] == 'geometry' else {('records', 'protocol_order'), ('ledger', 'conservation')})
        if key[1] in {'vertical_grasp_axis', 'upward_grasp_axis'}:
            coverage.add(('geometry', 'grasp_orientation'))
        if key[1] == 'repair_pose_deviation': coverage.add(('geometry', 'registered_repair_pose'))
        if key[2] not in executed or key[2] not in completed or key[2] not in captured or not coverage.issubset(passed):
            unverified.add(key)
        else: resolved.add(key)
    result['resolved'] = describe(resolved, old)
    result['remaining'] = describe(original & new.keys(), new)
    result['unverified'] = describe(unverified, old)
    added = new.keys() - old.keys()
    result['new_failures'] = describe(added, new)
    if executed != expected[:len(executed)] or any(step not in expected for step in executed):
        result['preservation_violations'].append('execution_scope')
    limitations = []
    blockers = []
    for row in report_rows(br):
        if row['status'] == 'pass': continue
        triple = (row['group'], row['code'], row['status'])
        if triple in {('records', 'scope', 'review'), ('coverage', 'not_implemented', 'uncovered')}:
            limitations.append(deepcopy(row))
        elif pending_measurement_check(row, b.get('snapshot', {})):
            limitations.append(deepcopy(row))
        elif row['status'] != 'fail': blockers.append(deepcopy(row))
    result['coverage_limits'], result['review_or_missing'] = limitations, blockers
    if result['preservation_violations'] or added:
        result['status'] = 'regressed'
    elif not original:
        result['reasons'].append('来源报告没有与修复目标绑定的明确失败')
    elif unverified or executed != expected or not end_confirmed:
        result['reasons'].append('目标步骤尚未完整重跑或相关检查未覆盖，不能把问题消失记为解决')
    elif result['remaining']:
        result['status'] = 'partially_improved' if resolved else 'not_improved'
    elif new:
        result['status'] = 'partially_improved'
    elif blockers:
        result['status'] = 'needs_review'
    else:
        result['status'] = 'improved'
        result['eligible_for_continuation'] = True
    result['interpretation'] = '比较已确认前缀、独立检查与实际执行范围；未覆盖项仍未覆盖，不代表完整实验或真机验收'
    return result

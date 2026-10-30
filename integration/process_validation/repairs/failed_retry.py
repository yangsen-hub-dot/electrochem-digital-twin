"""Allow a new candidate after a bounded, evidenced execution timeout."""


def retryable_timeout(effect, archive, manifest):
    correction = manifest.get('correction') or {}
    dependency = correction.get('dependency_plan') or {}
    targets = set(correction.get('fault_step_ids') or [])
    failures = effect.get('new_failures') or []
    if (not effect.get('comparable') or effect.get('status') != 'regressed'
            or effect.get('preservation_violations') or not targets or not failures
            or archive.get('snapshot', {}).get('state_uncertain') is not True):
        return False
    if not all(row.get('group') == 'records' and row.get('code') == 'execution_failed'
               and row.get('step_id') in targets
               and '机械臂动作超时' in row.get('message', '') for row in failures):
        return False
    events = [event for event in archive.get('events', []) if event.get('type') == 'step.failed']
    if len(events) != 1 or events[0].get('step_id') not in targets:
        return False
    return bool(dependency.get('checkpoint_step_id')
                and events[0].get('step_id') in dependency.get('rerun_step_ids', []))

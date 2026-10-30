"""Select one ordered repair cohort without changing the evidence report."""
from copy import deepcopy


def select_cohort(archive, targets):
    plan = archive.get('snapshot', {}).get('plan', [])
    ids = [row.get('step_id') for row in plan]
    if not ids or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('实验步骤编号缺失或重复')
    if any(target.step_id not in ids for target in targets):
        raise ValueError('失败步骤不在原实验流程中')
    ordered = sorted(targets, key=lambda target: ids.index(target.step_id))
    first = ordered[0]
    # Keep a component's pick/place (or pick/connect) together because the
    # latter depends on the former. Different sides are separate cohorts.
    def key(target):
        side = 'left' if '_left_' in target.step_id else 'right' if '_right_' in target.step_id else None
        return target.family, side
    boundary = next((index for index, target in enumerate(ordered)
                     if key(target) != key(first)), len(ordered))
    selected, deferred = ordered[:boundary], ordered[boundary:]
    return selected, deferred


def scoped_electrode_report(report, selected):
    """Temporary strategy input; the bound, full report is never rewritten."""
    selected_ids = {target.step_id for target in selected}
    view = deepcopy(report)
    for group in ('checks', 'issues'):
        view[group] = [row for row in view.get(group, [])
                       if row.get('status') != 'fail' or row.get('step_id') in selected_ids]
    return view


def sequence_metadata(selected, deferred):
    return {'selected_step_ids': [t.step_id for t in selected],
            'deferred_step_ids': [t.step_id for t in deferred],
            'deferred_are_unverified': True,
            'next_step': 'revalidate_then_continue_from_confirmed_checkpoint'}

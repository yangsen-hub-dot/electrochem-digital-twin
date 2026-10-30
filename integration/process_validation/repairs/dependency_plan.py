"""Choose a bounded contiguous segment; earlier confirmed actions are restored."""
from copy import deepcopy
import hashlib
import json
import math

from .dependency_graph import build_graph
from .dependency_rules import PAIR_KINDS, REPAIRABLE_COMPONENTS, RELEASE_ACTIONS


def checkpoint_result(archive, revision):
    rows = [row for row in archive.get('accepted_receipts', {}).values()
            if row.get('receipt', {}).get('status') == 'succeeded'
            and row.get('result', {}).get('state_revision') == revision]
    if len(rows) != 1:
        raise ValueError('恢复点缺少唯一的成功回执状态')
    return rows[0]


def released_state(state):
    setup = state.get('cell_setup', {})
    return (setup.get('held_component') is None and 'held_component' in setup
            and setup.get('active_pipette_id') is None and setup.get('pipette_parked') is True
            and state.get('tip', {}).get('attached') is False)


def checkpoint_signature(row):
    receipt = deepcopy(row['receipt'])
    receipt.pop('run_id', None)
    return hashlib.sha256(json.dumps({'receipt': receipt, 'state': row['result'].get('state'),
        'error_model_realized': row['result'].get('error_model_realized')},
        sort_keys=True, allow_nan=False).encode()).hexdigest()


def _checkpoint_facts(state):
    setup = state.get('cell_setup', {})
    facts = {'gripper:free', 'pipette:parked', 'tip:detached'} if released_state(state) else set()
    for side in ('left', 'right'):
        identity = side + '_half_cell'
        volume = state.get('containers', {}).get(identity, {}).get('volume_ul')
        if (not isinstance(volume, bool) and isinstance(volume, (int, float))
                and math.isfinite(volume) and volume > 0): facts.add('solution:'+identity+':present')
        if setup.get(side+'_electrode_placed') is True: facts.add('installed:electrode:'+identity)
        elif setup.get(side+'_electrode_placed') is False: facts.add('available:electrode:'+identity)
        if setup.get(side+'_leads_connected') is True: facts.add('connected:lead:'+identity)
        elif (setup.get(side+'_leads_connected', False) is False
                and identity not in (setup.get('measurement_lead_map') or {})):
            facts.add('available:lead:'+identity)
    if setup.get('salt_bridge_connected') is True: facts.add('connected:salt_bridge')
    elif setup.get('salt_bridge_connected') is False: facts.add('available:salt_bridge:salt_bridge_holder')
    return facts


def build_dependency_plan(archive, parameter_step_ids, *, cohort=None):
    snapshot = archive.get('snapshot', {})
    plan = snapshot.get('plan', [])
    graph = build_graph(plan)
    ids = [step['step_id'] for step in plan]
    selected = set(parameter_step_ids)
    if not selected or not selected.issubset(ids):
        raise ValueError('修复参数没有绑定协议中的具体步骤')
    if cohort not in {None, 'measurement_lead_pose'}:
        raise ValueError('未登记的参数联动范围')
    if cohort:
        earliest = min(ids.index(step) for step in selected)
        selected.update(row['step_id'] for row in plan[earliest:]
                        if row['action'] in {'pick_lead', 'connect_lead'})
    closure = {ids.index(step) for step in selected}
    for index in list(closure):
        kind = PAIR_KINDS.get(plan[index]['action'], (None,))[0]
        if kind not in REPAIRABLE_COMPONENTS or index not in graph['pairs']:
            raise ValueError('该动作没有已登记的可局部修复夹取/释放依赖')
        closure.add(graph['pairs'][index])
    first, last = min(closure), max(closure)
    if first == 0 or plan[last]['action'] not in RELEASE_ACTIONS:
        raise ValueError('局部段缺少前置恢复点或组件释放终点')
    # The runner supports one contiguous segment. Never silently re-execute
    # liquid handling merely to bridge two separated mechanical faults.
    for step in plan[first:last+1]:
        if step['action'] not in {'pick_electrode', 'place_electrode', 'pick_salt_bridge',
                                  'connect_salt_bridge', 'pick_lead', 'connect_lead'}:
            raise ValueError('局部范围跨越未授权重放的配液/混匀/测量或其他动作')
    completed = [e for e in archive.get('events', []) if e.get('type') == 'step.completed']
    if ([e.get('step_id') for e in completed] != ids[:len(completed)]
            or len(completed) != snapshot.get('completed_steps')
            or len(completed) != snapshot.get('state_revision') or first > len(completed)):
        raise ValueError('恢复点之前不是连续确认前缀')
    row = checkpoint_result(archive, first)
    event = completed[first-1]
    if (row['receipt'].get('step_id') != ids[first-1]
            or row['receipt'].get('run_id') != snapshot.get('run_id')
            or event.get('payload', {}).get('executor_receipt') != row['receipt']
            or not released_state(row['result'].get('state', {}))):
        raise ValueError('检查点回执不一致，或工具/枪头尚未释放与停放')
    facts = _checkpoint_facts(row['result']['state'])
    initial_facts = sorted(facts)
    for node in graph['nodes'][first:last+1]:
        missing = set(node['requires']) - facts
        if missing: raise ValueError('检查点或前置动作未证明所需状态：' + ','.join(sorted(missing)))
        facts.difference_update(node['consumes'])
        facts.update(node['produces'])
    return {'schema_version': '1.0', 'checkpoint_step_id': ids[first-1], 'checkpoint_revision': first,
        'checkpoint_signature': checkpoint_signature(row), 'stop_after_step': ids[last],
        'confirmed_checkpoint_facts': initial_facts,
        'parameter_step_ids': [ids[i] for i in sorted(closure)],
        'rerun_step_ids': ids[first:last+1], 'preserved_prefix_step_ids': ids[:first],
        'deferred_step_ids': ids[last+1:], 'cohort': cohort,
        'prerequisite_edges': graph['edges'],
        'action_contracts': graph['nodes'][first:last+1],
        'bridging_step_ids': [ids[i] for i in range(first,last+1) if i not in closure],
        'end_requires': ['gripper:free', 'pipette:parked', 'tip:detached'],
        'interpretation': '只恢复已确认前缀；执行所列连续局部段；剩余后缀待独立复验后再续跑'}


def validate_dependency_profile(profile, archive, resume_after=None, stop_after=None):
    spec = profile.get('dependency_plan')
    if not spec:
        return  # Legacy profiles remain subject to their existing gates.
    rebuilt = build_dependency_plan(archive, list(profile.get('actions', {})) + list(profile.get('routes', {})),
                                    cohort=spec.get('cohort'))
    if spec != rebuilt or (resume_after is not None and resume_after != spec['checkpoint_step_id']) or (
            stop_after is not None and stop_after != spec['stop_after_step']):
        raise ValueError('局部依赖计划、恢复点或终点已变化，拒绝执行')
    if profile.get('proposal_fingerprint'):
        from .effects.fingerprints import candidate_fingerprint
        if profile['proposal_fingerprint'] != candidate_fingerprint(profile, archive):
            raise ValueError('方案实际参数与已保存指纹不一致')

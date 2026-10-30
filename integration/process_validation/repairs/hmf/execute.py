"""Execute only the planned precondition and failed numeric step from a snapshot."""
from __future__ import annotations

import random
from copy import deepcopy
from uuid import uuid4

from electrochem_sim.chemical_models import resolve_chemical_model
from electrochem_sim.conservation import check_conservation
from electrochem_sim.error_model import ErrorModel
from electrochem_sim.workflow_actions import get_action
from electrochem_sim.workflow_checkpoint import restore_snapshot
from electrochem_sim.workflow_checks import check_transition
from .planner import build_plan
from .verify import verify_repair


def execute_repair(source, expected_plan):
    plan = build_plan(source)
    if not plan.get('eligible') or plan != expected_plan:
        raise ValueError('修复计划已失效，或与当前来源证据不一致。')
    config = deepcopy(source['config'])
    state = restore_snapshot(source['steps'][-1]['before'])
    initial = state.snapshot()
    original_inventory = restore_snapshot(source['initial_state']).inventory()
    model = resolve_chemical_model(config)
    rng = random.Random(source['seed'])
    context = {'rng':rng, 'errors':ErrorModel(config.get('error_model'), rng=rng), 'model':model,
               'species':config.get('species', {}), 'locked_containers':set()}
    branch_id = uuid4().hex
    records, failure = [], None
    for step in plan['steps']:
        before = state.snapshot()
        try:
            candidate, event = get_action(step['action'])(state, step, context)
            previous = deepcopy(state.chemical_model_state)
            candidate.chemical_model_state = model.advance(previous, candidate, step=step, event=event,
                elapsed_time_s=event.get('duration_s',0.0), config=config)
            if model.validate_transition(state, candidate, state.chemical_model_state,
                                         candidate.chemical_model_state, config=config).get('passed') is not True:
                raise ValueError('模型状态转移校验未通过')
            conservation = check_transition(original_inventory, state, candidate, set())
            if not records:
                candidate.ledger.set_lineage(run_id=branch_id, source_run_id=source['run_id'],
                    checkpoint_step_id=plan['dependency_plan']['checkpoint_step_id'] if 'dependency_plan' in plan else (
                        source['steps'][-2]['event']['step_id'] if len(source['steps'])>1 else None),
                    checkpoint_revision=plan['checkpoint_revision'], candidate_id=plan['candidate_fingerprint'])
            candidate.ledger.record_step(state_revision=state.ledger.revision+1, step_id=step['step_id'],
                action=step['action'], outcome='succeeded', source='local_numeric_repair', simulation_time_s=None,
                command=step, receipt={'actual_source':'numerical_simulation', 'event':event})
            candidate.ledger.provenance[-1]['workflow_event'] = deepcopy(event)
            if step['action'] in {'grasp_electrode', 'install_electrode'}:
                entity = 'electrode:' + step['electrode_id']
                candidate.ledger.register_entity(entity, 'electrode')
                candidate.ledger._set_relation(entity,
                    'held_by' if step['action'] == 'grasp_electrode' else 'installed_in',
                    'robot_gripper' if step['action'] == 'grasp_electrode' else step['target'],
                    revision=candidate.ledger.revision, command_id=step['step_id'], source='semantic_simulation')
            if step == plan['steps'][-1]:
                candidate.ledger.workflow['status'] = 'paused'
            state = candidate
            records.append({'status':'completed','event':event,'before':before,'after':state.snapshot(),'conservation':conservation})
        except (ValueError, KeyError, TypeError) as exc:
            failure = {'step_id':step['step_id'],'action':step['action'],'reason':str(exc)}
            state.ledger.record_step(state_revision=state.ledger.revision, step_id=step['step_id'],
                action=step['action'], outcome='failed', source='local_numeric_repair', simulation_time_s=None,
                command=step, receipt=failure)
            records.append({'status':'failed','event':failure,'before':before,'after':state.snapshot()})
            break
    state.ledger.workflow['status'] = 'paused' if not failure else 'failed'
    result = {'run_id':branch_id, 'status':'repair_segment_completed' if not failure else 'failed',
              'data_origin':'numerical_simulation', 'model_id':source['model_id'], 'seed':source['seed'],
              'config':config, 'initial_state':initial, 'final_state':state.snapshot(), 'steps':records,
              'failure':failure, 'conservation':check_conservation(original_inventory,state.inventory()),
              'conservation_scope':'recorded inventories only; reaction is not modeled',
              'repair':{'source_run_id':source['run_id'], 'candidate_fingerprint':plan['candidate_fingerprint'],
                        'checkpoint_revision':plan['checkpoint_revision'], 'remaining_original_steps':plan['remaining_original_steps']}}
    result['repair']['verification'] = verify_repair(source,plan,result)
    if not result['repair']['verification']['target_resolved'] and not failure:
        result['status'] = 'repair_verification_failed'
    return result

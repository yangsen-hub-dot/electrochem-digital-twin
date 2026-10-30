"""Identify a bounded, evidenced HMF numerical repair from an archived run."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from ..models import RepairTarget, blocked
from ..effects.fingerprints import candidate_fingerprint
from electrochem_sim.conservation import check_conservation
from electrochem_sim.workflow_checkpoint import restore_snapshot


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def build_plan(result):
    run_id = result.get('run_id')
    try:
        if result.get('data_origin') != 'numerical_simulation' or result.get('status') != 'failed':
            raise ValueError('仅支持失败的 HMF 数值运行。')
        config, rows, failure = result['config'], result['steps'], result['failure']
        if result.get('model_id') != 'hmf_tempo_fdca_endpoint_dargo2025_v1':
            raise ValueError('模型与 HMF 修复策略不匹配。')
        if config.get('error_model', {}).get('enabled'):
            raise ValueError('启用误差时缺少可恢复的随机状态，暂不自动局部重跑。')
        if not rows or rows[-1].get('status') != 'failed' or failure != rows[-1].get('event'):
            raise ValueError('失败回执不完整。')
        steps = config['workflow']
        if len(rows) > len(steps) or any(
            row.get('event', {}).get('step_id') != step.get('step_id') or
            row.get('event', {}).get('action') != step.get('action') for row, step in zip(rows, steps)):
            raise ValueError('运行回执与流程不匹配。')
        if any(row.get('status') != 'completed' for row in rows[:-1]):
            raise ValueError('前缀存在未确认步骤。')
        for previous, following in zip(rows[:-1], rows[1:]):
            if previous.get('after') != following.get('before'):
                raise ValueError('前缀状态不连续。')
        for revision, row in enumerate(rows[:-1], start=1):
            ledger = row.get('after', {}).get('state_ledger', {})
            provenance = ledger.get('provenance', [])
            if (ledger.get('revision') != revision or len(provenance) != revision or
                    provenance[-1].get('step_id') != row['event']['step_id'] or
                    provenance[-1].get('workflow_event') != row['event']):
                raise ValueError('前缀账本回执缺失或不匹配。')
        if rows[0].get('before') != result['initial_state']:
            raise ValueError('初始状态不匹配。')
        checkpoint = rows[-1]['before']
        restored = restore_snapshot(checkpoint)
        if restored.completed_steps != len(rows)-1 or restored.ledger.revision != len(rows)-1:
            raise ValueError('检查点版本与已确认前缀不匹配。')
        if any(row.get('event', {}).get('action') == 'electrolyze' for row in rows[:-1]):
            raise ValueError('电解后状态未知，不能恢复修复。')
        initial = restore_snapshot(result['initial_state'])
        if not check_conservation(initial.inventory(), restored.inventory())['passed']:
            raise ValueError('前缀库存守恒无法确认。')
        failed_step = steps[len(rows)-1]
        action, reason = failed_step['action'], failure['reason']
        if action == 'install_electrode' and reason == 'electrode must be grasped before installation':
            eid = failed_step.get('electrode_id')
            if not isinstance(eid, str) or not eid or restored.cell_setup.get('workflow_held_electrode') is not None:
                raise ValueError('夹爪或电极状态不足，不能自动补抓。')
            fix = {'step_id': 'repair_precondition_' + failed_step['step_id'], 'action':'grasp_electrode', 'electrode_id':eid}
            family = 'hmf_electrode_precondition'
        elif action == 'aspirate' and reason == '吸液前必须装枪头':
            if restored.tip.loaded or restored.tip.liquid.volume_l != 0:
                raise ValueError('枪头状态不足，不能自动装头。')
            fix = {'step_id': 'repair_precondition_' + failed_step['step_id'], 'action':'load_tip'}
            family = 'hmf_pipette_precondition'
        else:
            raise ValueError('该问题尚无经验证的 HMF 数值修复策略。')
        if fix['step_id'] in {s.get('step_id') for s in steps}:
            raise ValueError('修复步骤编号与原流程冲突。')
        target = RepairTarget(failed_step['step_id'], family, ({'code':'execution_failed','message':reason},))
        profile = {'actions': {'precondition': fix, 'retry': failed_step},
                   'dependency_plan': {'checkpoint_step_id': rows[-2]['event']['step_id'] if len(rows)>1 else None,
                                       'stop_after_step': failed_step['step_id']}}
        fingerprint = candidate_fingerprint(profile, {'snapshot': {'conditions': config}, 'seed': result['seed']})
        return {'run_id':run_id, 'eligible':True, 'status':'planned', 'family':target.family,
                'target_step_id':target.step_id, 'checkpoint_revision':restored.ledger.revision,
                'source_digest':_digest(result), 'candidate_fingerprint':fingerprint,
                'steps':[fix, deepcopy(failed_step)], 'remaining_original_steps':deepcopy(steps[len(rows):]),
                'scope':'仅数值模拟；恢复已确认前缀并停在原失败步骤，不自动继续。'}
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        return blocked(run_id, str(exc))


def source_digest(result):
    return _digest(result)

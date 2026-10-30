"""Independently check the intended local HMF precondition and retried action."""
from __future__ import annotations


def verify_repair(source, plan, result):
    reasons = []
    records = result.get('steps', [])
    if result.get('initial_state') != source['steps'][-1]['before']:
        reasons.append('恢复点与原失败前状态不一致')
    if len(records) != 2 or any(r.get('status') != 'completed' for r in records):
        reasons.append('修复步骤或原失败步骤未成功执行')
    elif [r['event'].get('step_id') for r in records] != [s['step_id'] for s in plan['steps']]:
        reasons.append('执行步骤与候选不一致')
    if not result.get('conservation', {}).get('passed'):
        reasons.append('记录库存守恒未通过')
    if not reasons:
        final = result['final_state']
        failed_step = plan['steps'][-1]
        if plan['family'] == 'hmf_electrode_precondition':
            electrode = final['cell_setup'].get('workflow_electrodes', {}).get(failed_step['electrode_id'])
            if not electrode or any(electrode.get(key) != failed_step.get(key) for key in ('target','role','material')):
                reasons.append('电极未按原目标安装')
            if final['cell_setup'].get('workflow_held_electrode') is not None:
                reasons.append('电极仍处于夹持状态')
        elif plan['family'] == 'hmf_pipette_precondition':
            if not final['tip']['loaded'] or final['tip']['liquid']['volume_l'] <= 0:
                reasons.append('枪头未装好或没有吸到液体')
            if records[-1]['event'].get('source') != failed_step.get('source'):
                reasons.append('吸液来源不匹配')
        else:
            reasons.append('未知问题类型')
    return {'status':'improved' if not reasons else 'unverified', 'target_resolved':not reasons,
            'reasons':reasons, 'scope':'仅数值步骤；未验证机器人几何或真实设备。',
            'remaining_steps_executed':False}

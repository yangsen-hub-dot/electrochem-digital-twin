"""Audit confirmed archived chemistry, independently of stored passed flags."""
from .numbers import Comparisons, EvidenceError, number
from .states import initial_stores, read_stores, check_inventory, compare_stores
from .transfers import check_transfer
from .voltage import check_voltage, SUPPORTED_MODELS


def check_chemistry(archive, report):
    if not isinstance(archive, dict):
        report.issue('chemistry', 'state_evidence', 'fail', '运行档案结构无效')
        return
    config, snapshot = archive.get('config'), archive.get('snapshot')
    if not isinstance(config, dict) or not isinstance(snapshot, dict) or not isinstance(snapshot.get('state'), dict):
        report.check('chemistry', 'archive_schema', 'uncovered', '旧档案缺完整配置或化学状态，未作独立化学核验')
        return
    cell = config.get('cell') or {}
    if not isinstance(cell, dict):
        report.issue('chemistry', 'state_evidence', 'fail', '化学模型配置结构无效')
        return
    model = cell.get('chemical_model_id') or cell.get('equilibrium_model')
    state_model = snapshot['state'].get('chemical_model') or {}
    if not isinstance(state_model, dict) or not isinstance(state_model.get('state', {}), dict):
        report.issue('chemistry', 'state_evidence', 'fail', '化学模型状态结构无效')
        return
    dynamics = state_model.get('state', {}).get('dynamics_enabled')
    model_ids = [value for value in (model, cell.get('equilibrium_model'), state_model.get('model_id')) if value]
    if (any(not isinstance(value, str) for value in model_ids)
            or (dynamics is not None and not isinstance(dynamics, bool))):
        report.issue('chemistry', 'state_evidence', 'fail', '化学模型编号或动力学开关无效')
        return
    if any(value not in SUPPORTED_MODELS for value in model_ids) or model not in SUPPORTED_MODELS or dynamics is True:
        report.check('chemistry', 'model_scope', 'uncovered', '独立物料守恒规则目前限于无反应消耗的平衡浓差电池；该模型需专属规则')
        return
    compare = Comparisons(report)
    try:
        if snapshot.get('run_id') != report.data['run_id']:
            raise EvidenceError('化学记录运行编号与检查对象不一致')
        initial = initial_stores(config)
        rows = list(archive.get('accepted_receipts', {}).values())
        if any(not isinstance(row, dict) or not isinstance(row.get('receipt'), dict)
               or not isinstance(row.get('result'), dict) for row in rows):
            raise EvidenceError('成功回执结构无效')
        rows = [row for row in rows if row['receipt'].get('status') == 'succeeded']
        if any(type(row['result'].get('state_revision')) is not int for row in rows):
            raise EvidenceError('成功回执版本无效')
        rows.sort(key=lambda row: row['result']['state_revision'])
        if ([row['result']['state_revision'] for row in rows] != list(range(1, len(rows) + 1))
                or snapshot.get('completed_steps') != len(rows) or snapshot.get('state_revision') != len(rows)):
            raise EvidenceError('成功回执不是完整连续前缀，不能核对逐步化学变化')
        events = [e for e in archive.get('events', []) if e.get('type') == 'step.completed']
        if len(events) != len(rows):
            raise EvidenceError('成功事件与成功回执数量不一致')
        plan = snapshot.get('plan') or []
        previous, measured = initial, False
        for i, (row, event) in enumerate(zip(rows, events)):
            receipt, result = row['receipt'], row['result']
            step_id = receipt.get('step_id')
            payload = event.get('payload') or {}
            if (event.get('step_id') != step_id or i >= len(plan) or plan[i].get('step_id') != step_id
                    or event.get('action') != plan[i].get('action')
                    or any(record.get('run_id') != snapshot.get('run_id') for record in (receipt, result, event))
                    or payload.get('command_id') != receipt.get('command_id')
                    or payload.get('state_revision') != result['state_revision']
                    or payload.get('executor_receipt') != receipt):
                raise EvidenceError('事件、协议和成功回执没有绑定到同一运行/步骤/命令/版本')
            action = event.get('action')
            field = 'source' if action == 'aspirate' else 'target' if action == 'dispense' else None
            if field and payload.get(field) != plan[i].get(field):
                raise EvidenceError('实际物料来源/目标与协议不一致')
            current = read_stores(result.get('state'), compare, step_id=step_id)
            check_inventory(initial, current, compare, step_id=step_id)
            compare.equal('transfer_balance', result['state'].get('temperature_k'),
                          number(config.get('temperature_k'), positive=True), 'temperature', step_id=step_id)
            check_transfer(event.get('action'), payload, receipt, previous, current, compare, step_id=step_id)
            measured |= check_voltage(result, current, config, compare, step_id=step_id)
            previous = current
        final = read_stores(snapshot['state'], compare, step_id='final_snapshot')
        check_inventory(initial, final, compare, step_id='final_snapshot')
        compare_stores(previous, final, compare, 'final_state_binding', step_id='final_snapshot')
        compare.equal('final_state_binding', snapshot['state'].get('temperature_k'),
                      config.get('temperature_k'), 'temperature', step_id='final_snapshot')
        if rows and snapshot.get('voltage') != rows[-1]['result'].get('voltage'):
            compare.fail('final_state_binding', '最终电压记录与最后成功回执不一致', field='voltage_record')
        measured |= check_voltage(snapshot, final, config, compare, step_id='final_snapshot')
        if snapshot.get('state_uncertain'):
            report.check('chemistry', 'observation_scope', 'review', '仅核对已提交的模拟化学记录，失败动作现场的物理状态仍不确定')
    except (EvidenceError, KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        compare.fail('state_evidence', '化学独立核验缺少有效且一致的证据：' + str(exc))
        # No successful aggregate may hide an interrupted/partial verification.
        for code in ('independent_inventory', 'concentration_balance', 'transfer_balance',
                     'final_state_binding', 'nernst_baseline', 'voltage_sum'):
            if code not in compare.failed:
                report.check('chemistry', code, 'uncovered', '化学核验因无效证据中断，未作全面通过判定')
        return
    pending = (snapshot.get('voltage', {}).get('valid') is False
               and not any(step.get('action') == 'measure_voltage' for step in plan[:len(rows)]))
    compare.finish(awaiting_measurement=pending)
    enabled = [name for name in ('activity_correction', 'liquid_junction_potential')
               if (cell.get(name) or {}).get('enabled')]
    report.check('coverage' if enabled else 'chemistry', 'not_implemented' if enabled else 'correction_models', 'uncovered' if enabled else 'pass',
                 '本轮开启的活度/液接修正仅核对分项合计，尚未独立复算其物理模型'
                 if enabled else '本轮未开启活度与液接修正，不涉及其物理模型独立复算')

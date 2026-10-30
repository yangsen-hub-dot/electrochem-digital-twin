"""Numerical comparison contracts, not calibration or physical error bounds."""
import math
from collections import Counter


class EvidenceError(ValueError):
    pass


def number(value, *, nonnegative=False, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (nonnegative and value < 0)
            or (positive and value <= 0)):
        raise EvidenceError('需要有限数值及正确的正负范围，不能为缺失值、布尔值或字符串')
    return float(value)


# Roundoff-only tolerances in the explicitly named units. No resampling occurs.
ABS_TOL = {'volume': 1e-7, 'amount': 1e-14, 'concentration': 1e-10, 'voltage': 1e-7,
           'temperature': 1e-9}
MESSAGES = {
    'independent_inventory': '独立累加各容器、枪头和废弃物，核对初始总液量及各物种物质量',
    'concentration_balance': '逐状态按物质量/升计算浓度，并核对有记录的混合分区总量',
    'transfer_balance': '逐动作核对实际交付、来源/目标/枪头/废弃物增减及记录物质量',
    'final_state_binding': '最终化学库存与最后成功回执一致',
    'nernst_baseline': '使用独立反应商公式核对实际平均浓度对应的能斯特分项',
    'voltage_sum': '独立核对电压分项合计与预测值，理想虚拟仪表按其契约核对',
}


class Comparisons:
    def __init__(self, report):
        self.report, self.evaluated, self.failed = report, Counter(), set()

    def equal(self, code, actual, expected, kind, **where):
        actual, expected = number(actual), number(expected)
        self.evaluated[code] += 1
        if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=ABS_TOL[kind]):
            self.fail(code, MESSAGES[code] + '：数值不一致',
                      actual=actual, expected=expected, quantity=kind, **where)

    def fail(self, code, message, **where):
        self.failed.add(code)
        self.report.issue('chemistry', code, 'fail', message, **where)

    def finish(self, *, awaiting_measurement=False):
        for code, message in MESSAGES.items():
            if code in self.failed:
                continue
            if self.evaluated[code]:
                self.report.check('chemistry', code, 'pass', message,
                                  comparisons=self.evaluated[code])
            else:
                pending = awaiting_measurement and code in {'nernst_baseline', 'voltage_sum'}
                self.report.check('chemistry', code, 'uncovered', message + '：缺少适用记录，未判定通过',
                                  reason_code='not_yet_measured' if pending else 'missing_evidence')


def pending_measurement_check(row, snapshot):
    """Only a future measurement may be deferred by checkpoint continuation."""
    if (row.get('group') != 'chemistry' or row.get('code') not in {'nernst_baseline', 'voltage_sum'}
            or row.get('status') != 'uncovered' or row.get('reason_code') != 'not_yet_measured'):
        return False
    plan, count = snapshot.get('plan'), snapshot.get('completed_steps')
    if (not isinstance(plan, list) or type(count) is not int or not 0 <= count < len(plan)
            or snapshot.get('voltage', {}).get('valid') is not False
            or snapshot.get('state', {}).get('cell_setup', {}).get('voltage_measurement_completed')):
        return False
    return (not any(step.get('action') == 'measure_voltage' for step in plan[:count])
            and any(step.get('action') == 'measure_voltage' for step in plan[count:]))

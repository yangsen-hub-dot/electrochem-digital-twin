"""Check committed material transfers; never replay error sampling or execution."""
from copy import deepcopy

from .numbers import EvidenceError, number
from .states import compare_stores


NO_MATERIAL_CHANGE = {'load_tip', 'pick_pipette', 'park_pipette', 'mix', 'mix_pair',
                      'pick_electrode', 'place_electrode', 'place_electrodes',
                      'pick_salt_bridge', 'connect_salt_bridge', 'pick_lead',
                      'connect_lead', 'measure_voltage'}


def check_transfer(action, payload, receipt, before, after, compare, *, step_id):
    if action in NO_MATERIAL_CHANGE:
        compare_stores(before, after, compare, 'transfer_balance', step_id=step_id)
        return
    if action not in {'aspirate', 'dispense', 'discard_tip'}:
        compare.report.issue('chemistry', 'unsupported_action', 'uncovered',
                             '该动作没有独立物料转移规则', step_id=step_id, action=action)
        return
    q = number(payload.get('simulated_volume_ul'), nonnegative=True)
    for observed in (payload.get('actual_volume_ul'), receipt.get('actual_volume_ul')):
        compare.equal('transfer_balance', observed, q, 'volume', step_id=step_id)
    source, target = ({'aspirate': (payload.get('source'), 'tip'),
                       'dispense': ('tip', payload.get('target')),
                       'discard_tip': ('tip', 'waste')})[action]
    if source not in before or target not in before or source == target:
        raise EvidenceError('物料转移来源/目标不在已记录库存中或相互冲突')
    if q > before[source]['volume_ul'] + 1e-7:
        compare.fail('transfer_balance', '实际转移量超过来源库存', step_id=step_id)
        return
    if action == 'discard_tip':
        compare.equal('transfer_balance', q, before['tip']['volume_ul'], 'volume', step_id=step_id)
    moved = payload.get('transferred_amounts_mol')
    if not isinstance(moved, dict):
        raise EvidenceError('缺少本次实际转移物质量记录')
    moved = {s: number(n, nonnegative=True) for s, n in moved.items()}
    expected = deepcopy(before)
    expected[source]['volume_ul'] -= q
    expected[target]['volume_ul'] += q
    for species in before[source]['amounts_mol'].keys() | moved.keys():
        total = before[source]['amounts_mol'].get(species, 0.)
        n = total * q / before[source]['volume_ul'] if before[source]['volume_ul'] else 0.
        compare.equal('transfer_balance', moved.get(species, 0.), n, 'amount',
                      step_id=step_id, species=species)
        expected[source]['amounts_mol'][species] = total - n
        expected[target]['amounts_mol'][species] = before[target]['amounts_mol'].get(species, 0.) + n
    compare_stores(expected, after, compare, 'transfer_balance', step_id=step_id)

"""Recompute stock totals and concentrations from explicit archived quantities."""
import math

from .numbers import EvidenceError, number


def initial_stores(config):
    definitions = config.get('containers')
    if not isinstance(definitions, dict) or not definitions or {'tip', 'waste'} & definitions.keys():
        raise EvidenceError('初始容器配置缺失或与枪头/废弃物保留编号冲突')
    stores = {}
    for name, definition in definitions.items():
        volume = number(definition.get('volume_ml'), nonnegative=True) * 1000.
        concentrations = definition.get('concentrations_m')
        if not isinstance(concentrations, dict):
            raise EvidenceError('缺少初始浓度记录')
        stores[name] = {'volume_ul': volume, 'amounts_mol': {
            species: number(c, nonnegative=True) * volume * 1e-6
            for species, c in concentrations.items()}}
    # This schema's initial tip/waste are empty, as specified by its constructor.
    stores.update(tip={'volume_ul': 0., 'amounts_mol': {}},
                  waste={'volume_ul': 0., 'amounts_mol': {}})
    return stores


def read_stores(state, compare, *, step_id=None):
    if (not isinstance(state, dict) or not isinstance(state.get('containers'), dict)
            or not isinstance(state.get('tip'), dict) or not isinstance(state.get('waste'), dict)):
        raise EvidenceError('缺少容器、枪头或废弃物的完整化学状态')
    raw = dict(state['containers'])
    if {'tip', 'waste'} & raw.keys():
        raise EvidenceError('容器编号与枪头/废弃物冲突')
    raw.update(tip=state['tip'].get('liquid'), waste=state['waste'])
    stores = {}
    for name, store in raw.items():
        where = {'step_id': step_id, 'container': name}
        if not isinstance(store, dict) or not isinstance(store.get('amounts_mol'), dict):
            raise EvidenceError(f'{name} 缺少显式物质量')
        volume = number(store.get('volume_ul'), nonnegative=True)
        amounts = {species: number(n, nonnegative=True) for species, n in store['amounts_mol'].items()}
        concentrations = store.get('concentrations_m')
        if not isinstance(concentrations, dict):
            raise EvidenceError(f'{name} 缺少浓度字段，不能独立核对')
        for species in amounts.keys() | concentrations.keys():
            n = amounts.get(species, 0.)
            if volume == 0:
                if n != 0 or concentrations.get(species) is not None:
                    compare.fail('concentration_balance', '零液量容器含非零物质量或声明了有效浓度',
                                 species=species, **where)
                else:
                    compare.evaluated['concentration_balance'] += 1
            else:
                compare.equal('concentration_balance', concentrations.get(species), n / (volume * 1e-6),
                              'concentration', species=species, **where)
        if store.get('capacity_ul') is not None:
            capacity = number(store['capacity_ul'], positive=True)
            if volume > capacity + 1e-7:
                compare.fail('state_evidence', '容器液量超过已记录容量', **where)
        if 'mixing_components' in store:
            parts = store['mixing_components']
            if not isinstance(parts, list) or any(not isinstance(p, dict) or not isinstance(p.get('amounts_mol'), dict) for p in parts):
                raise EvidenceError(f'{name} 的混合分区记录无效')
            compare.equal('concentration_balance', sum(number(p.get('volume_l'), nonnegative=True) for p in parts) * 1e6,
                          volume, 'volume', **where)
            species_set = set(amounts) | {species for p in parts for species in p['amounts_mol']}
            for species in species_set:
                compare.equal('concentration_balance', math.fsum(number(p['amounts_mol'].get(species, 0.), nonnegative=True) for p in parts),
                              amounts.get(species, 0.), 'amount', species=species, **where)
        stores[name] = {'volume_ul': volume, 'amounts_mol': amounts}
    return stores


def compare_stores(before, after, compare, code, **where):
    if before.keys() != after.keys():
        compare.fail(code, '容器/枪头/废弃物编号集合发生未解释变化', **where)
        return
    for name, expected in before.items():
        compare.equal(code, after[name]['volume_ul'], expected['volume_ul'], 'volume', container=name, **where)
        for species in expected['amounts_mol'].keys() | after[name]['amounts_mol'].keys():
            compare.equal(code, after[name]['amounts_mol'].get(species, 0.), expected['amounts_mol'].get(species, 0.),
                          'amount', container=name, species=species, **where)


def check_inventory(initial, current, compare, **where):
    if initial.keys() != current.keys():
        compare.fail('independent_inventory', '当前容器集合与初始配置不一致', **where)
        return
    compare.equal('independent_inventory', math.fsum(s['volume_ul'] for s in current.values()),
                  math.fsum(s['volume_ul'] for s in initial.values()), 'volume', **where)
    species_set = {species for stores in (initial, current) for store in stores.values() for species in store['amounts_mol']}
    for species in species_set:
        compare.equal('independent_inventory', math.fsum(s['amounts_mol'].get(species, 0.) for s in current.values()),
                      math.fsum(s['amounts_mol'].get(species, 0.) for s in initial.values()),
                      'amount', species=species, **where)

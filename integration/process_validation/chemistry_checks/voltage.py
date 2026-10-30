"""Independent Nernst baseline and arithmetic checks; no production solver import."""
import math

from .numbers import EvidenceError, number


SUPPORTED_MODELS = {'ideal_concentration_cell_nernst', 'reaction_quotient_concentration_cell_nernst'}
R, F = 8.314462618, 96485.33212


def check_voltage(result, stores, config, compare, *, step_id):
    voltage = result.get('voltage') or {}
    if voltage.get('valid') is not True:
        return False
    cell = config['cell']
    prediction = voltage.get('prediction')
    if (not isinstance(prediction, dict) or prediction.get('valid') is not True
            or prediction.get('model') not in SUPPORTED_MODELS):
        raise EvidenceError('有效电压缺少受支持的预测模型与分项记录')
    redox = cell.get('redox_reaction') or {}
    electrons = redox.get('electrons_transferred', cell.get('electrons_transferred', 2))
    if type(electrons) is not int or electrons <= 0:
        raise EvidenceError('电子数必须为正整数')
    terms = redox.get('reaction_quotient_terms', cell.get('reaction_quotient_terms'))
    active = cell.get('active_species', cell.get('copper_species'))
    if terms is None and active:
        terms = {active: -1.}
    if not isinstance(terms, dict) or not terms:
        raise EvidenceError('缺少反应商定义')
    left, right = stores[cell['left_container']], stores[cell['right_container']]
    temperature = number(result['state'].get('temperature_k'), positive=True)
    log_ratio = 0.
    concentrations = {}
    for species, exponent in terms.items():
        exponent = number(exponent)
        if exponent == 0:
            raise EvidenceError('反应商指数不能为零')
        cl = number(left['amounts_mol'].get(species), positive=True) / number(left['volume_ul'] * 1e-6, positive=True)
        cr = number(right['amounts_mol'].get(species), positive=True) / number(right['volume_ul'] * 1e-6, positive=True)
        log_ratio += exponent * (math.log(cr) - math.log(cl))
        concentrations[species] = (cl, cr)
    if voltage.get('polarity') != 'right_minus_left' or prediction.get('polarity') != 'right_minus_left':
        raise EvidenceError('缺少右减左的电压极性契约')
    parts = prediction.get('voltage_components_mv')
    if not isinstance(parts, dict):
        raise EvidenceError('缺少电压分项')
    ideal = -R * temperature / (electrons * F) * log_ratio * 1000.
    compare.equal('nernst_baseline', parts.get('ideal_nernst'), ideal, 'voltage', step_id=step_id)
    if active in concentrations:
        for key, value in zip(('c_left_m', 'c_right_m'), concentrations[active]):
            compare.equal('nernst_baseline', prediction.get(key), value, 'concentration', step_id=step_id, field=key)
    total = number(parts.get('ideal_nernst'))
    for config_key, field in (('activity_correction', 'activity_correction'),
                              ('liquid_junction_potential', 'liquid_junction_potential')):
        enabled = (cell.get(config_key) or {}).get('enabled', False)
        if not isinstance(enabled, bool):
            raise EvidenceError('电压修正开关必须为布尔值')
        component = parts.get(field)
        if component is None and not enabled:
            component = 0.
        component = number(component)
        if not enabled:
            compare.equal('voltage_sum', component, 0., 'voltage', step_id=step_id, field=field)
        total += component
    for field, actual in (('prediction.voltage_mv', prediction.get('voltage_mv')),
                          ('prediction.voltage_v', number(prediction.get('voltage_v')) * 1000.),
                          ('predicted_cell_mv', voltage.get('predicted_cell_mv'))):
        compare.equal('voltage_sum', actual, total, 'voltage', step_id=step_id, field=field)
    if voltage.get('instrument_model') == 'ideal_virtual_ocp' and voltage.get('instrument_error_enabled') is False:
        compare.equal('voltage_sum', voltage.get('virtual_meter_mv'), total, 'voltage', step_id=step_id)
    else:
        compare.report.check('chemistry', 'instrument_model', 'uncovered', '非理想虚拟仪表不按理想读数契约核验')
    return True

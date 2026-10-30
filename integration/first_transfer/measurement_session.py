"""Receipt-driven wiring and ideal virtual OCP; reuses the existing solver.

Gamry two-electrode topology: W+WS right, REF+CE left, cell output OFF.
No source-asset synthetic waveform, repeated operation error, or real reading.
"""
from copy import deepcopy
import math

LEAD_MAP = {'left_half_cell': ['reference_white', 'counter_red'],
            'right_half_cell': ['working_green', 'working_sense_blue']}
REQUIRED_GEOMETRY = ('terminal_contact', 'above_liquid', 'electrode_supported',
                     'bridge_clear', 'cable_continuous', 'opposite_terminal_clear')


def measurement_steps():
    steps = []
    for side in ('left', 'right'):
        target = side + '_half_cell'
        steps.extend([
            {'step_id': 'ui_pick_' + side + '_lead', 'action': 'pick_lead',
             'source': 'measurement_lead_rack', 'target': target, 'component': 'measurement_lead'},
            {'step_id': 'ui_connect_' + side + '_lead', 'action': 'connect_lead',
             'target': target, 'component': 'measurement_lead'}])
    steps.append({'step_id': 'ui_measure_voltage', 'action': 'measure_voltage',
                  'target': 'virtual_potentiostat', 'component': 'ideal_virtual_ocp'})
    return steps


def apply_measurement(state, action, config):
    candidate = deepcopy(state)
    setup = candidate.cell_setup
    if not (setup.get('electrodes_placed') and setup.get('salt_bridge_connected')
            and setup.get('pipette_parked')):
        raise ValueError('接线/测量前须停放移液器，并完成双侧电极及盐桥安装')
    event = {'step_id': action.step_id, 'action': action.kind, 'target': action.target,
             'component': action.component, 'liquid_volume_change_ul': 0.,
             'cell_output_enabled': False, 'data_origin': 'simulated'}
    if action.kind in {'pick_lead', 'connect_lead'}:
        if action.target not in LEAD_MAP or action.component != 'measurement_lead':
            raise ValueError('接线目标或部件无效')
        side = action.target.split('_')[0]
        key = side + '_leads_connected'
        if setup.get(key):
            raise ValueError('该侧引线已经连接，禁止重复接线')
        if action.kind == 'pick_lead':
            if action.source != 'measurement_lead_rack' or setup.get('held_component') or setup.get('active_tool') != 'none':
                raise ValueError('取引线前夹爪须空闲，且须从指定支架取件')
            setup.update(held_component='measurement_lead', held_component_target=action.target,
                         active_tool='measurement_lead')
        else:
            if setup.get('held_component') != 'measurement_lead' or setup.get('held_component_target') != action.target:
                raise ValueError('未持有对应侧引线，不能确认接线')
            setup.update({key: True, 'held_component': None, 'held_component_target': None, 'active_tool': 'none'})
            setup.setdefault('measurement_lead_map', {})[action.target] = LEAD_MAP[action.target].copy()
            setup['measurement_leads_connected'] = bool(setup.get('left_leads_connected') and setup.get('right_leads_connected'))
        event['lead_roles'] = LEAD_MAP[action.target].copy()
    elif action.kind == 'measure_voltage':
        if action.target != 'virtual_potentiostat' or setup.get('held_component'):
            raise ValueError('测量仪器目标或夹爪状态无效')
        if not setup.get('measurement_leads_connected') or setup.get('measurement_lead_map') != LEAD_MAP:
            raise ValueError('测量须先确认两侧引线及右减左极性映射')
        from electrochem_sim.electrochemistry import predict_voltage
        from electrochem_sim.simulator import run_experiment
        prediction = predict_voltage(state, config['cell'], config.get('species', {}))
        if not prediction['valid'] or not math.isfinite(prediction['voltage_mv']):
            raise ValueError('原电化学引擎未给出有效开路电压：' + str(prediction.get('reason')))
        baseline_config = deepcopy(config)
        baseline_config['error_model']['enabled'] = False
        baseline = run_experiment(baseline_config, seed=0)['prediction']
        if not baseline['valid']:
            raise ValueError('无操作误差基准计算失败')
        from voltage_measurement_model import specification_envelope
        measurement_uncertainty = specification_envelope(prediction['voltage_mv'])
        junction = prediction.get('liquid_junction_potential', {})
        result = {'valid': True, 'reason': None, 'polarity': 'right_minus_left',
                  'baseline_theoretical_mv': baseline['voltage_mv'],
                  'predicted_cell_mv': prediction['voltage_mv'],
                  'virtual_meter_mv': prediction['voltage_mv'], 'measured_mv': None,
                  'measurement_uncertainty': measurement_uncertainty,
                  'data_origin': 'simulated', 'instrument_model': 'ideal_virtual_ocp',
                  'instrument_error_enabled': False, 'instrument_uncertainty_enabled': True,
                  'cell_output_enabled': False,
                  'solver': 'existing_electrochem_sim.predict_voltage',
                  'lead_map': deepcopy(LEAD_MAP), 'prediction': prediction,
                  'baseline_prediction': baseline,
                  'liquid_junction_reference_estimate': junction.get('status') == 'estimated_from_reference_parameters',
                  'liquid_junction_parameter_use_policy': junction.get('parameter_use_policy'),
                  'limitations': ['单次读数仍为理想虚拟仪表；不是实测值，也未模拟重复读数噪声、漂移或稳定时间',
                                  '附带的 Gamry Reference 620 厂家规格范围仅为条件性敏感性情景，不代表当前系统校准',
                                  '混匀误差的局部情景范围来自现有引擎，不伪造空间采样点']}
        setup['measurement_result'] = result
        setup['voltage_measurement_completed'] = True
        event['voltage'] = deepcopy(result)
    else:
        raise ValueError('未知测量动作')
    candidate.completed_steps += 1
    return candidate, event


def validate_measurement_receipt(command, receipt):
    if receipt.get('actual_source') != command['actual_source'] or receipt.get('liquid_volume_change_ul') != 0.:
        raise ValueError('测量接线不得改液量或重复注入操作误差')
    if receipt.get('cell_output_enabled') is not False:
        raise ValueError('开路测量必须关闭仪器电流输出')
    if command['action'] == 'pick_lead':
        gap = receipt.get('gripper_gap_measured_m')
        if (receipt.get('component_attached') != 'measurement_lead'
                or receipt.get('source') != 'measurement_lead_rack' or receipt.get('target') != command['target']
                or isinstance(gap, bool) or not isinstance(gap, (int, float))
                or not math.isfinite(gap) or not .007 <= gap <= .013):
            raise ValueError('引线抓取回执未确认夹持、来源或目标侧')
    elif command['action'] == 'connect_lead':
        checks = receipt.get('geometry_checks', {})
        if (receipt.get('target') != command['target'] or receipt.get('gripper_released') is not True
                or receipt.get('lead_roles') != LEAD_MAP[command['target']]
                or not isinstance(checks, dict) or any(checks.get(k) is not True for k in REQUIRED_GEOMETRY)):
            raise ValueError('引线接触、避让、极性或释放回执未通过')
    else:
        if (receipt.get('lead_map') != LEAD_MAP or receipt.get('voltage') != command.get('voltage')
                or receipt.get('instrument_model') != 'ideal_virtual_ocp'):
            raise ValueError('虚拟读数须与本次引擎输出及接线映射一致')

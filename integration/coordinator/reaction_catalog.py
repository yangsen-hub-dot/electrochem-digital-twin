"""Reaction registration and batch expansion using the single-run condition builder.

The scheduler depends on conditions/execution adapters, not Cu chemistry. A new
reaction must supply an engine configuration and an explicitly supported adapter.
"""
from copy import deepcopy
from itertools import product
import json
import math

FIELDS = {
    'temperature_c': {'label': '温度', 'unit': '°C', 'type': 'number'},
    'left_concentration_m': {'label': '左侧目标浓度', 'unit': 'M', 'type': 'number'},
    'right_concentration_m': {'label': '右侧目标浓度', 'unit': 'M', 'type': 'number'},
    'left_volume_ml': {'label': '左侧最终体积', 'unit': 'mL', 'type': 'number'},
    'right_volume_ml': {'label': '右侧最终体积', 'unit': 'mL', 'type': 'number'},
    'mixing_duration_s': {'label': '混匀时间', 'unit': 's', 'type': 'number'},
    'salt_bridge_type': {'label': '盐桥种类', 'type': 'enum', 'options': ['KNO3', 'KCl', 'NaNO3']},
    'salt_bridge_concentration_m': {'label': '盐桥浓度', 'unit': 'M', 'type': 'number'},
    'activity_correction_enabled': {'label': '活度修正', 'type': 'boolean'},
    'liquid_junction_enabled': {'label': '液接电势修正', 'type': 'boolean'},
    'liquid_junction_model': {'label': '液接电势模型', 'type': 'enum', 'options': ['henderson_approximation', 'nernst_planck_zero_current_linear_profile']},
    'errors_enabled': {'label': '操作误差', 'type': 'boolean'},
}
FACTORS = ('pipetting', 'tip_carryover', 'mixing')
LEGACY_KEYS = {
    'left_concentrations_m': 'left_concentration_m', 'right_concentrations_m': 'right_concentration_m',
    'left_total_volume_ml': 'left_volume_ml', 'right_total_volume_ml': 'right_volume_ml',
    'salt_bridge_types': 'salt_bridge_type', 'salt_bridge_concentrations_m': 'salt_bridge_concentration_m',
    'liquid_junction_models': 'liquid_junction_model',
}


class ReactionCatalog:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.root = coordinator.engine_root / 'config'
        self.entries = {row['id']: row for row in json.loads((self.root / 'reaction_templates.json').read_text(encoding='utf-8'))['templates']}

    def entry(self, template_id):
        if template_id not in self.entries:
            raise ValueError('未知反应模板；须先注册对应模型与流程适配器')
        entry = self.entries[template_id]
        if entry['condition_adapter'] != 'stock_dilution_concentration_cell':
            raise ValueError('该反应尚未接入条件规划适配器')
        return entry

    def base(self, template_id):
        entry = self.entry(template_id)
        path = (self.root / entry['config']).resolve()
        if path.parent != self.root.resolve():
            raise ValueError('反应配置须位于配置目录内')
        return self.coordinator.simulator.load_config(path)

    def validate(self, template_id, conditions, execution_mode):
        entry = self.entry(template_id)
        if execution_mode not in entry['execution_modes']:
            raise ValueError('该反应尚未配置对应的动作/录像适配器，请选择数值仿真')
        if execution_mode == 'workflow_video' and entry.get('workflow_adapter') != 'copper_matterix':
            raise ValueError('该反应的动作适配器尚未接入，不能套用其他反应的机械臂流程')
        base = self.base(template_id)
        if entry.get('workflow_adapter') == 'copper_matterix':
            from microcell_labware import CELLS
            for cell_id, geometry in CELLS.items():
                base['containers'][cell_id]['capacity_ml'] = geometry.working_capacity_ul / 1000
        if conditions.get('errors_enabled') and not base.get('error_model', {}).get('profile_file'):
            raise ValueError('该反应尚无适用的操作误差档案，请关闭操作误差')
        conditions = deepcopy(conditions)
        if not base.get('error_model', {}).get('profile_file'):
            conditions.pop('operation_error_levels', None)
        config = self.coordinator.input.build_condition_config(base, conditions)
        # Preflight actual model applicability without drawing operation errors.
        nominal = deepcopy(config)
        nominal['error_model']['enabled'] = False
        result = self.coordinator.simulator.run_experiment(nominal, seed=0)
        if result['status'] != 'completed':
            raise ValueError('条件预检未通过：' + str(result['failure']))
        return config

    def describe(self):
        return [{**deepcopy(row), 'condition_fields': deepcopy(FIELDS), 'error_factors': list(FACTORS)}
                for row in self.entries.values()]

    def expand(self, payload, default_conditions):
        if not isinstance(payload, dict):
            raise ValueError('批量条件必须是对象')
        template_id = payload.get('template_id', 'copper_concentration_cell')
        mode = payload.get('execution_mode', 'workflow_video')
        self.entry(template_id)
        baseline = deepcopy(payload.get('base_conditions', default_conditions))
        if not isinstance(baseline, dict):
            raise ValueError('基础条件必须是对象')
        baseline.setdefault('liquid_junction_model', 'henderson_approximation')
        baseline.setdefault('operation_error_levels', dict.fromkeys(FACTORS, 'low'))
        settings = payload.get('axes', payload)
        if not isinstance(settings, dict):
            raise ValueError('扫描条件必须是对象')
        reserved = {'template_id', 'execution_mode', 'base_conditions', 'axes', 'replicates', 'max_scenarios', 'max_workers'}
        unknown = set(settings) - set(FIELDS) - set(LEGACY_KEYS) - {'temperatures_k', 'operation_error_levels'} - reserved
        if unknown:
            raise ValueError('未知批量条件：' + ', '.join(sorted(unknown)))
        axes = {}
        for key, value in settings.items():
            if key in reserved or key == 'operation_error_levels':
                continue
            target = LEGACY_KEYS.get(key, key)
            values = value if isinstance(value, list) else [value]
            if key == 'temperatures_k':
                target = 'temperature_c'
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
                    raise ValueError('温度必须是数字')
                values = [v - 273.15 for v in values]
            if not values or len({json.dumps(v, sort_keys=True) for v in values}) != len(values):
                raise ValueError('扫描列表不能为空或包含重复值')
            if target in axes:
                raise ValueError('同一条件不能重复指定')
            field = FIELDS[target]
            if field['type'] == 'number' and any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                raise ValueError(field['label'] + '必须是有限数字')
            if field['type'] == 'boolean' and any(not isinstance(v, bool) for v in values):
                raise ValueError(field['label'] + '必须为布尔值')
            axes[target] = values
        factor_axes = settings.get('operation_error_levels', {})
        if not isinstance(factor_axes, dict):
            raise ValueError('操作误差档位必须是对象')
        for factor, values in factor_axes.items():
            if factor not in FACTORS or not isinstance(values, list) or not values or any(v not in {'low','medium','high'} for v in values):
                raise ValueError('无效操作误差档位')
            if len(set(values)) != len(values):
                raise ValueError('操作误差档位不能重复')
            axes['operation_error_levels.' + factor] = values
        replicates = payload.get('replicates', 1)
        maximum = payload.get('max_scenarios', 500)
        if isinstance(replicates, bool) or not isinstance(replicates, int) or not 1 <= replicates <= 5000:
            raise ValueError('重复次数必须为 1–5000 的整数')
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 5000:
            raise ValueError('最大组数必须为 1–5000 的整数')
        count = math.prod(len(v) for v in axes.values()) * replicates
        if count > maximum:
            raise ValueError(f'共 {count} 组，超过最大组数 {maximum}')
        rows = []
        for combination in product(*axes.values()):
            conditions = deepcopy(baseline)
            for key, value in zip(axes, combination):
                if key.startswith('operation_error_levels.'):
                    conditions['operation_error_levels'][key.split('.')[1]] = value
                else:
                    conditions[key] = value
            config = self.validate(template_id, conditions, mode)
            for repeat in range(1, replicates + 1):
                rows.append({'conditions': deepcopy(config['condition_inputs']), 'replicate': repeat, 'replicate_count': replicates})
        return template_id, mode, rows

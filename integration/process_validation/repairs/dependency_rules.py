"""Action contracts for the current sequential concentration-cell workflow.

These describe prerequisite facts and a released handoff. They do not grant
repair strategies to liquid handling, measurements or unknown actions.
"""
PAIR_KINDS = {
    'pick_electrode': ('electrode', 'acquire'), 'place_electrode': ('electrode', 'release'),
    'pick_salt_bridge': ('salt_bridge', 'acquire'), 'connect_salt_bridge': ('salt_bridge', 'release'),
    'pick_lead': ('lead', 'acquire'), 'connect_lead': ('lead', 'release'),
    'pick_pipette': ('pipette', 'acquire'), 'park_pipette': ('pipette', 'release'),
}
KNOWN_ACTIONS = set(PAIR_KINDS) | {'load_tip', 'aspirate', 'dispense', 'discard_tip',
                                    'mix', 'mix_pair', 'measure_voltage'}
REPAIRABLE_COMPONENTS = {'electrode', 'salt_bridge', 'lead'}
RELEASE_ACTIONS = {'place_electrode', 'connect_salt_bridge', 'connect_lead'}
LIQUID_ACTIONS = {'load_tip', 'aspirate', 'dispense', 'discard_tip', 'mix', 'mix_pair'}


def component_key(step):
    kind, operation = PAIR_KINDS[step['action']]
    if kind == 'salt_bridge':
        return kind, 'salt_bridge_holder'
    if kind == 'pipette':
        identity = step.get('pipette_id')
    else:
        identity = step.get('target')
    if not isinstance(identity, str) or not identity:
        raise ValueError('组件身份缺失，不能按动作名称猜测依赖：' + step['step_id'])
    return kind, identity


def facts_for(step):
    action = step['action']
    if action == 'measure_voltage':
        return {'requires': ['connected:lead:left_half_cell', 'connected:lead:right_half_cell', 'connected:salt_bridge'],
                'produces': ['voltage:observed'], 'consumes': []}
    if action not in PAIR_KINDS:
        return {'requires': [], 'produces': [], 'consumes': []}
    kind, identity = component_key(step)
    held = f'held:{kind}:{identity}'
    if PAIR_KINDS[action][1] == 'acquire':
        needs = ['gripper:free']
        if kind != 'pipette': needs += ['pipette:parked', 'tip:detached']
        if kind in REPAIRABLE_COMPONENTS: needs += [f'available:{kind}:{identity}']
        if kind == 'electrode': needs += [f'solution:{identity}:present']
        if kind == 'salt_bridge': needs += ['installed:electrode:left_half_cell', 'installed:electrode:right_half_cell']
        if kind == 'lead': needs += [f'installed:electrode:{identity}', 'connected:salt_bridge']
        consumes = ['gripper:free'] + ([f'available:{kind}:{identity}'] if kind in REPAIRABLE_COMPONENTS else [])
        return {'requires': needs, 'produces': [held], 'consumes': consumes}
    stable = ('pipette:parked' if kind == 'pipette' else 'connected:salt_bridge' if kind == 'salt_bridge'
              else f'connected:lead:{identity}' if kind == 'lead' else f'installed:{kind}:{identity}')
    return {'requires': [held], 'produces': [stable, 'gripper:free'], 'consumes': [held]}

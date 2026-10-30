"""Display-only workflow groups derived from the actual command plan/receipts."""

STAGES = (
    ('prepare', '左右配液', 'Prepare both solutions'),
    ('mix', '双池同步混匀', 'Mix both cells'),
    ('electrodes', '左右电极安装', 'Install electrodes'),
    ('bridge', '连接盐桥', 'Connect salt bridge'),
    ('measure', '接线并测量', 'Wire and measure'),
)


def stage_for_action(action):
    if action in {'load_tip', 'aspirate', 'dispense', 'discard_tip'}:
        return 'prepare'
    if action in {'mix', 'mix_pair'}:
        return 'mix'
    if action in {'park_pipette', 'pick_electrode', 'place_electrode'}:
        return 'electrodes'
    if action in {'pick_salt_bridge', 'connect_salt_bridge', 'place_salt_bridge'}:
        return 'bridge'
    if action in {'connect_wire', 'connect_leads', 'measure_voltage', 'pick_lead', 'connect_lead'}:
        return 'measure'
    return None


def workflow_progress(plan=(), completed_steps=0, current_step_id=None,
                      active_action=None):
    """Never infer future-stage completion from the last command's stage."""
    completed_steps = max(0, min(int(completed_steps), len(plan)))
    current_stage = stage_for_action(active_action)
    if current_step_id:
        current = next((step for step in plan
                        if step.get('step_id') == current_step_id), None)
        if current:
            current_stage = stage_for_action(current.get('action'))
    rows = []
    for key, zh, en in STAGES:
        indices = [i for i, step in enumerate(plan)
                   if stage_for_action(step.get('action')) == key]
        if indices and all(i < completed_steps for i in indices):
            state = 'done'
        elif key == current_stage and (indices or not plan):
            state = 'current'
        else:
            state = 'pending' if indices else 'future'
        rows.append({'key': key, 'zh': zh, 'en': en, 'state': state})
    return rows

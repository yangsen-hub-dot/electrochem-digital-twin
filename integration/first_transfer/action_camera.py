"""Choose a detail-camera subject from the active semantic action.

This module only selects a visual focus; it does not change robot targets,
chemistry, liquid amounts, or the experiment protocol.
"""


def focus_key(phase, command=None, completed=None):
    phase_kind = str(phase).split("/", 1)[0]
    action = (command or {}).get("action")
    material = (command or {}).get("material_id")

    if phase_kind in {"process_completed", "diagnostic_segment_complete"}:
        action = (completed or {}).get("action", action)
        material = (completed or {}).get("material_id", material)

    if phase_kind.startswith('pick_lead') or action == 'pick_lead':
        return 'measurement_lead_rack'
    if phase_kind.startswith('connect_lead') or action == 'connect_lead':
        return 'measurement_connection'
    if phase_kind.startswith('measure_voltage') or action == 'measure_voltage':
        return 'measurement_result'
    if phase_kind.startswith('pick_salt_bridge') or action == 'pick_salt_bridge':
        return 'salt_bridge_rack'
    if phase_kind.startswith('connect_salt_bridge') or action == 'connect_salt_bridge':
        return 'salt_bridge_installation'
    if phase_kind.startswith("park_pipette") or action == "park_pipette" or action == "pick_pipette":
        support = (command or {}).get('support_id') or (command or {}).get('target') or ''
        if not support:
            support = (completed or {}).get('support_id') or (completed or {}).get('target') or ''
        return support if str(support).startswith('pipette_stand_') else 'pipette_stand'
    if phase_kind.startswith("pick_electrode") or action == "pick_electrode":
        return "electrode_rack"
    if phase_kind.startswith("place_electrode") or action == "place_electrode":
        target = (command or {}).get("target") or (completed or {}).get("target")
        return "right_electrode_installation" if target == "right_half_cell" else "left_electrode_installation"

    if phase_kind.startswith("load_tip") or action == "load_tip":
        return "tip_rack"
    if phase_kind.startswith("discard_tip") or action == "discard_tip":
        return "waste"
    if phase_kind.startswith("aspirate") or action == "aspirate":
        source = (command or {}).get("source")
        return source or ("water" if material == "water" else "copper_stock")
    if phase_kind.startswith("dispense") or action == "dispense":
        return (command or {}).get("target") or "left_half_cell"
    if action == "mix_pair":
        return "both_half_cells"
    if phase_kind.startswith("mix") or action == "mix":
        return "left_half_cell"
    return "left_half_cell"

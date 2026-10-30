"""Expand liquid-transfer plans with explicit range-tool exchanges.

The two instruments remain separate semantic identities. A range change adds
kinematic park/pick actions; this does not claim contact-force or hardware
validation.
"""

from copy import deepcopy


def expand_pipette_tool_actions(definitions):
    expanded = []
    active_id = None
    transition = 0
    for definition in definitions:
        item = deepcopy(definition)
        if item.get("action") == "load_tip":
            next_id = item.get("pipette_id")
            if next_id not in {"gilson_pipetman_p200", "gilson_pipetman_p1000"}:
                raise ValueError("移液步骤缺少受支持的 P200/P1000 工具编号")
            if next_id != active_id:
                transition += 1
                if active_id is not None:
                    expanded.append({"step_id": f"ui_park_{transition}_{active_id}",
                                     "action": "park_pipette", "pipette_id": active_id,
                                     "target": f"pipette_stand_{active_id}"})
                expanded.append({"step_id": f"ui_pick_{transition}_{next_id}",
                                 "action": "pick_pipette", "pipette_id": next_id,
                                 "source": f"pipette_stand_{next_id}"})
                active_id = next_id
        elif item.get("action") == "park_pipette":
            item["pipette_id"] = active_id
            item["target"] = f"pipette_stand_{active_id}" if active_id else "pipette_stand"
            active_id = None
        expanded.append(item)
    return expanded

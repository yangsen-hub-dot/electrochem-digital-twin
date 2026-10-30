"""Receipt-driven tool parking and paired-electrode assembly state.

This module changes only semantic assembly state. It does not model robot
contact forces, electrochemistry, liquid volume, or voltage.
"""

from __future__ import annotations

from copy import deepcopy

from .state import ExperimentState
from .cell_assembly import apply_assembly


def apply_tool_assembly(state: ExperimentState, action: str, step_id: str,
                        source: str | None = None, target: str | None = None,
                        component: str | None = None,
                        pipette_id: str | None = None) -> tuple[ExperimentState, dict]:
    next_state = deepcopy(state)
    setup = next_state.cell_setup
    inventory = setup.setdefault("pipette_inventory", {
        "gilson_pipetman_p200": "parked", "gilson_pipetman_p1000": "parked"})
    if action == "pick_pipette":
        if pipette_id not in inventory or inventory[pipette_id] != "parked":
            raise ValueError("只能从对应支架取用当前处于停放状态的移液器")
        if setup.get("held_component") is not None or setup.get("active_tool") != "none":
            raise ValueError("取移液器前夹爪必须空闲")
        inventory[pipette_id] = "held"
        setup["active_pipette_id"] = pipette_id
        setup["pipette_parked"] = False
        setup["active_tool"] = "pipette"
        event = {"step_id": step_id, "action": action, "pipette_id": pipette_id,
                 "source": f"pipette_stand_{pipette_id}", "semantic_only": True}
    elif action == "park_pipette":
        active_id = setup.get("active_pipette_id") or pipette_id
        if active_id not in inventory or inventory[active_id] != "held":
            raise ValueError("移液器停放编号与当前持有工具不匹配")
        if next_state.tip.loaded or next_state.tip.liquid.volume_l > 1e-15:
            raise ValueError("停放移液器前必须完成弃头且枪头内无残液")
        if setup.get("held_component"):
            raise ValueError("夹爪仍持有其他部件，不能停放移液器")
        inventory[active_id] = "parked"
        setup["active_pipette_id"] = None
        setup["pipette_parked"] = True
        setup["active_tool"] = "none"
        event = {"step_id": step_id, "action": action, "pipette_id": active_id,
                 "target": f"pipette_stand_{active_id}",
                 "semantic_only": True}
    elif action == "pick_electrode":
        if not setup.get("pipette_parked") or setup.get("active_tool") != "none":
            raise ValueError("抓取电极前须将移液器放回支架")
        if setup.get("held_component") is not None:
            raise ValueError("夹爪已有持有部件")
        if (component != "copper_electrode" or source != "electrode_rack"
                or target not in {"left_half_cell", "right_half_cell"}):
            raise ValueError("本流程只允许从电极架抓取铜电极并送往左右小池")
        side = "left" if target == "left_half_cell" else "right"
        if setup.get(f"{side}_electrode_placed"):
            raise ValueError(f"{side}侧铜电极已经安装，不能重复抓取")
        setup["held_component"] = component
        setup["held_component_target"] = target
        setup["active_tool"] = component
        event = {"step_id": step_id, "action": action, "source": source,
                 "target": target, "component": component, "semantic_only": True}
    elif action == "place_electrode":
        if setup.get("held_component") != "copper_electrode":
            raise ValueError("安装前夹爪必须持有铜电极")
        if target not in {"left_half_cell", "right_half_cell"}:
            raise ValueError("电极安装目标必须是左右小池之一")
        if setup.get("held_component_target") != target:
            raise ValueError("电极必须安装在本次抓取时指定的一侧")
        side = "left" if target == "left_half_cell" else "right"
        if setup.get(f"{side}_electrode_placed"):
            raise ValueError(f"{side}侧铜电极已经安装，不能重复安装")
        setup[f"{side}_electrode_placed"] = True
        setup["held_component"] = None
        setup["held_component_target"] = None
        setup["active_tool"] = "none"
        setup["electrodes_placed"] = bool(setup.get("left_electrode_placed")
                                          and setup.get("right_electrode_placed"))
        event = {"step_id": step_id, "action": action, "target": target,
                 "component": "copper_electrode", "electrodes_placed": setup["electrodes_placed"],
                 "semantic_only": True}
    elif action == "pick_salt_bridge":
        if not setup.get("electrodes_placed") or not setup.get("pipette_parked"):
            raise ValueError("取盐桥前须完成双侧电极安装并停放移液器")
        if setup.get("held_component") is not None or setup.get("active_tool") != "none":
            raise ValueError("取盐桥前夹爪须空闲")
        if (component != "salt_bridge" or source != "salt_bridge_rack"
                or target != "salt_bridge_holder" or setup.get("salt_bridge_connected")):
            raise ValueError("盐桥来源、承托位或连接状态不符合要求")
        setup.update(held_component="salt_bridge", held_component_target=target,
                     active_tool="salt_bridge")
        event = {"step_id": step_id, "action": action, "source": source,
                 "target": target, "component": component, "semantic_only": True}
    elif action == "connect_salt_bridge":
        if (setup.get("held_component") != "salt_bridge"
                or setup.get("held_component_target") != "salt_bridge_holder"
                or target != "salt_bridge_holder" or not setup.get("electrodes_placed")
                or setup.get("salt_bridge_connected")):
            raise ValueError("连接盐桥前须持有对应盐桥且双侧电极就位")
        next_state, event = apply_assembly(next_state, action, step_id)
        next_state.cell_setup.update(held_component=None, held_component_target=None,
                                     active_tool="none")
        event.update(target=target, component="salt_bridge", semantic_only=True,
                     electrical_leads_connected=False)
        return next_state, event
    else:
        raise ValueError(f"未知工具/电极动作: {action}")
    next_state.completed_steps += 1
    return next_state, event

"""电池组装状态处理器；不计算电压，也不模拟机械臂轨迹。"""

from __future__ import annotations

from copy import deepcopy

from .state import ExperimentState


def apply_assembly(state: ExperimentState, action: str, step_id: str, component: str | None = None) -> tuple[ExperimentState, dict]:
    next_state = deepcopy(state)
    if action == "place_electrodes":
        next_state.cell_setup["electrodes_placed"] = True
    elif action == "connect_salt_bridge":
        if not next_state.cell_setup.get("electrodes_placed"):
            raise ValueError("连接盐桥前需要先记录电极就位")
        next_state.cell_setup["salt_bridge_connected"] = True
        next_state.cell_setup["salt_bridge_type"] = component or next_state.cell_setup.get("salt_bridge_type")
    else:
        raise ValueError(f"未知组装操作: {action}")
    next_state.completed_steps += 1
    return next_state, {"step_id": step_id, "action": action, "component": component}

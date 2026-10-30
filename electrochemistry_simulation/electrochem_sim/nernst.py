"""能斯特平衡电势计算组件。"""

from __future__ import annotations

import math

R_J_PER_MOL_K = 8.314462618
F_C_PER_MOL = 96485.33212


def concentration_cell_voltage_v(c_left_m: float, c_right_m: float, temperature_k: float, electrons: int = 2) -> float:
    """浓度近似活度时的能斯特电压；保留为理想基线兼容接口。"""
    return activity_cell_voltage_v(c_left_m, c_right_m, temperature_k, electrons)


def activity_cell_voltage_v(a_left: float, a_right: float, temperature_k: float, electrons: int = 2) -> float:
    """按右侧减左侧计算浓差电池电压：RT/(nF) ln(a_right/a_left)。

    a_left/a_right 为无量纲活度；同一标准态因取比值而抵消。
    """
    values = (a_left, a_right, temperature_k, float(electrons))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("两侧活度、温度和电子转移数必须有限且大于零")
    return R_J_PER_MOL_K * temperature_k / (electrons * F_C_PER_MOL) * (math.log(a_right) - math.log(a_left))

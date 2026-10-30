"""物料守恒核验：只读比较库存，不修改实验状态。"""

from __future__ import annotations

import math


def check_conservation(initial: dict, current: dict) -> dict:
    """比较初始与当前总库存（库存应包含容器、枪头和废液）。"""
    species = initial["amounts_mol"].keys() | current["amounts_mol"].keys()
    differences = {
        key: current["amounts_mol"].get(key, 0.0) - initial["amounts_mol"].get(key, 0.0)
        for key in sorted(species)
    }
    volume_delta = current["volume_l"] - initial["volume_l"]
    passed = math.isclose(initial["volume_l"], current["volume_l"], rel_tol=1e-12, abs_tol=1e-15)
    passed = passed and all(
        math.isclose(initial["amounts_mol"].get(key, 0.0), current["amounts_mol"].get(key, 0.0), rel_tol=1e-12, abs_tol=1e-18)
        for key in species
    )
    return {"passed": passed, "volume_delta_l": volume_delta, "amount_deltas_mol": differences}

"""混匀操作处理器，与通用移液转移分离。"""

from __future__ import annotations

from copy import deepcopy

from .state import ExperimentState


def apply_mixing(state: ExperimentState, target_id: str, step_id: str, efficiency: float = 1.0) -> tuple[ExperimentState, dict]:
    next_state = deepcopy(state)
    target = next_state.containers[target_id]
    if target.volume_l <= 0:
        raise ValueError("空容器不能混匀")
    if not 0 <= efficiency <= 1:
        raise ValueError("混匀效率必须在 0 到 1 之间")
    if not target.mixing_components:
        target.mixing_components = [{
            "source_id": target_id,
            "volume_l": target.volume_l,
            "amounts_mol": dict(target.amounts_mol),
        }]
    for species, total_amount in target.amounts_mol.items():
        mean = total_amount / target.volume_l
        for component in target.mixing_components:
            volume = float(component["volume_l"])
            amount = float(component.get("amounts_mol", {}).get(species, 0.0))
            concentration = amount / volume
            mixed_concentration = mean + (concentration - mean) * (1.0 - efficiency)
            component.setdefault("amounts_mol", {})[species] = mixed_concentration * volume
    target.refresh_mixing_status()
    next_state.completed_steps += 1
    spreads = {species: target.concentration_spread_m(species) for species in target.amounts_mol}
    return next_state, {
        "step_id": step_id, "action": "mix", "target": target_id,
        "mixing_efficiency": efficiency,
        "resulting_uniformity": target.mixing_uniformity,
        "mixed": target.mixed,
        "remaining_concentration_spread_m": spreads,
        "local_concentration_ranges_m": {
            species: target.concentration_range_m(species) for species in target.amounts_mol
        },
    }


def apply_paired_mixing(
    state: ExperimentState,
    target_ids: tuple[str, str] | list[str],
    step_id: str,
    efficiencies: dict[str, float],
) -> tuple[ExperimentState, dict]:
    """Atomically apply one simultaneous two-cell mixing action.

    Each cell retains its own efficiency draw from the shared error model.
    This function changes concentration uniformity only; liquid amounts and
    total volume remain unchanged.
    """
    targets = tuple(target_ids)
    if len(targets) != 2 or targets[0] == targets[1]:
        raise ValueError("同步混匀必须指定两个不同的容器")
    if set(efficiencies) != set(targets):
        raise ValueError("双池混匀效率必须与目标容器一一对应")
    if any(not 0 <= float(efficiencies[cell_id]) <= 1 for cell_id in targets):
        raise ValueError("双池混匀效率必须位于 0 到 1 之间")

    next_state = deepcopy(state)
    cells = {}
    for cell_id in targets:
        target = next_state.containers[cell_id]
        if target.volume_l <= 0:
            raise ValueError(f"{cell_id} 空容器不能混匀")
        if not target.mixing_components:
            target.mixing_components = [{
                "source_id": cell_id,
                "volume_l": target.volume_l,
                "amounts_mol": dict(target.amounts_mol),
            }]
        efficiency = float(efficiencies[cell_id])
        for species, total_amount in target.amounts_mol.items():
            mean = total_amount / target.volume_l
            for component in target.mixing_components:
                volume = float(component["volume_l"])
                amount = float(component.get("amounts_mol", {}).get(species, 0.0))
                concentration = amount / volume
                component.setdefault("amounts_mol", {})[species] = (
                    mean + (concentration - mean) * (1.0 - efficiency)
                ) * volume
        target.refresh_mixing_status()
        cells[cell_id] = {
            "mixing_efficiency": efficiency,
            "resulting_uniformity": target.mixing_uniformity,
            "mixed": target.mixed,
            "remaining_concentration_spread_m": {
                species: target.concentration_spread_m(species)
                for species in target.amounts_mol
            },
            "local_concentration_ranges_m": {
                species: target.concentration_range_m(species)
                for species in target.amounts_mol
            },
        }
    next_state.completed_steps += 1
    return next_state, {
        "step_id": step_id,
        "action": "mix_pair",
        "targets": list(targets),
        "cells": cells,
        "liquid_volume_change_ul": 0.0,
    }

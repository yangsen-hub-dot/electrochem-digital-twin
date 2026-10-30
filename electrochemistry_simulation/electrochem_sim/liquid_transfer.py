"""通用液体转移操作：装枪头、吸液、分液/排液和弃枪头。"""

from __future__ import annotations

import math
from copy import deepcopy

from .error_model import RealizedAction
from .state import ExperimentState, LiquidState


def _move_liquid(source: LiquidState, target: LiquidState, volume_l: float) -> dict[str, float]:
    if not math.isfinite(volume_l) or volume_l <= 0:
        raise ValueError("转移体积必须有限且大于零")
    if volume_l > source.volume_l:
        raise ValueError(f"{source.identifier}: 液量不足")
    # A whole-container transfer (notably discarding a tip) does not sample
    # at an assumed uniform concentration: each conceptual parcel is moved
    # with its own amount record, so it remains valid even when unmixed.
    full_transfer = math.isclose(volume_l, source.volume_l, rel_tol=1e-12, abs_tol=1e-18)
    if full_transfer:
        volume_l = source.volume_l
    if not source.mixed and not full_transfer:
        raise ValueError(f"{source.identifier}: 尚未混匀，不能按均匀浓度取样")
    if target.capacity_l is not None and target.volume_l + volume_l > target.capacity_l:
        raise ValueError(f"{target.identifier}: 加液后超过容量")
    fraction = volume_l / source.volume_l
    transferred = {species: amount * fraction for species, amount in source.amounts_mol.items()}
    source_components = source.mixing_components or ([{
        "source_id": source.identifier,
        "volume_l": source.volume_l,
        "amounts_mol": dict(source.amounts_mol),
    }] if source.volume_l > 0 else [])
    moved_components = []
    remaining_components = []
    for component in source_components:
        component_volume = float(component["volume_l"])
        moved_volume = component_volume * fraction
        component_amounts = component.get("amounts_mol", {})
        moved_amounts = {name: float(amount) * fraction for name, amount in component_amounts.items()}
        remaining_amounts = {name: float(amount) - moved_amounts[name] for name, amount in component_amounts.items()}
        if moved_volume > 0:
            moved_components.append({
                "source_id": component.get("source_id", source.identifier),
                "volume_l": moved_volume,
                "amounts_mol": moved_amounts,
            })
        if moved_volume < component_volume:
            remaining_components.append({
                "source_id": component.get("source_id", source.identifier),
                "volume_l": component_volume - moved_volume,
                "amounts_mol": remaining_amounts,
            })
    if full_transfer:
        source.volume_l = 0.0
        source.amounts_mol = {}
        source.mixing_components = []
    else:
        source.volume_l -= volume_l
        for species, amount in transferred.items():
            source.amounts_mol[species] -= amount
        source.mixing_components = remaining_components
    target.volume_l += volume_l
    for species, amount in transferred.items():
        target.amounts_mol[species] = target.amounts_mol.get(species, 0.0) + amount
    target.mixing_components.extend(moved_components)
    source.refresh_mixing_status()
    target.refresh_mixing_status()
    source.validate()
    target.validate()
    return transferred


def apply_action(state: ExperimentState, realized: RealizedAction) -> tuple[ExperimentState, dict]:
    """对旧状态做深拷贝并应用液体转移；失败时不修改输入状态。"""
    next_state = deepcopy(state)
    command = realized.command
    event = {
        "step_id": command.step_id,
        "action": command.kind,
        "pipette_id": realized.pipette_id or command.pipette_id,
        "pipette_nominal_range_ul": (None if realized.pipette_nominal_range_ul is None
                                     else list(realized.pipette_nominal_range_ul)),
        "pipette_evidence_status": realized.pipette_evidence_status,
        "pipette_systematic_limit_ul": realized.pipette_systematic_limit_ul,
        "pipette_repeatability_sd_ul": realized.pipette_repeatability_sd_ul,
        "source": command.source,
        "target": command.target,
        "requested_volume_ul": None if command.volume_l is None else command.volume_l * 1_000_000,
        "simulated_volume_ul": None,
        "systematic_offset_ul": realized.systematic_offset_l * 1_000_000,
        "random_offset_ul": realized.random_offset_l * 1_000_000,
        "error_application_status": realized.error_status,
        "transferred_amounts_mol": {},
    }
    if command.kind == "load_tip":
        if next_state.tip.loaded:
            raise ValueError("已有枪头，请先弃置")
        if next_state.tip.liquid.volume_l > 0:
            raise ValueError("枪头中仍有液体，弃置后才能更换")
        next_state.tip.loaded = True
    elif command.kind == "discard_tip":
        if not next_state.tip.loaded:
            raise ValueError("没有可弃置的枪头")
        volume = next_state.tip.liquid.volume_l
        if volume:
            event["transferred_amounts_mol"] = _move_liquid(next_state.tip.liquid, next_state.waste, volume)
            event["simulated_volume_ul"] = volume * 1_000_000
        next_state.tip.loaded = False
    elif command.kind == "aspirate":
        if not next_state.tip.loaded:
            raise ValueError("吸液前必须装枪头")
        source = next_state.containers[command.source]
        volume = realized.volume_l
        assert volume is not None
        event["transferred_amounts_mol"] = _move_liquid(source, next_state.tip.liquid, volume)
        next_state.tip.liquid.mixed = True
        event["simulated_volume_ul"] = volume * 1_000_000
    elif command.kind == "dispense":
        if not next_state.tip.loaded:
            raise ValueError("排液前必须装枪头")
        target = next_state.containers[command.target]
        tip_volume = next_state.tip.liquid.volume_l
        residual_l = min(
            tip_volume,
            realized.tip_residual_l + tip_volume * realized.tip_residual_fraction,
        )
        # Keep the commanded aliquot as the denominator. The residual prior is
        # an absolute amount; reporting it as a fraction of aspirated volume
        # incorrectly makes the UI look independent of the requested dose.
        event["residual_volume_ul"] = residual_l * 1_000_000
        aliquot_l = command.volume_l if command.volume_l is not None else tip_volume
        event["residual_fraction_of_aliquot"] = residual_l / aliquot_l if aliquot_l > 0 else 0.0
        if realized.volume_l is None:
            volume = max(0.0, tip_volume - residual_l)
        else:
            available = max(0.0, tip_volume - residual_l)
            volume = min(realized.volume_l, available)
        if volume <= 0:
            raise ValueError("枪头内没有可排出的液体")
        event["transferred_amounts_mol"] = _move_liquid(next_state.tip.liquid, target, volume)
        for species in target.amounts_mol:
            spread = target.concentration_spread_m(species)
            target.mixing_reference_spread_m[species] = max(
                target.mixing_reference_spread_m.get(species, 0.0), spread
            )
        target.refresh_mixing_status()
        event["simulated_volume_ul"] = volume * 1_000_000
        event["retained_tip_volume_ul"] = next_state.tip.liquid.volume_l * 1_000_000
        event["retained_tip_fraction"] = event["residual_fraction_of_aliquot"]
        event["mixing_uniformity"] = target.mixing_uniformity
        event["local_concentration_ranges_m"] = {
            species: target.concentration_range_m(species) for species in target.amounts_mol
        }
    else:
        raise ValueError(f"液体转移模块不支持操作: {command.kind}")
    next_state.completed_steps += 1
    return next_state, event

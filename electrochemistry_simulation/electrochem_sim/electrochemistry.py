"""电化学计算入口：配置化半反应商、活度修正与液接电势。"""

from __future__ import annotations

import itertools
import math

from .activity import (
    activity_coefficients_for_reaction,
    dimensionless_activity,
    ionic_strength_molal,
)
from .liquid_junction import evaluate_liquid_junction_potential
from .nernst import activity_cell_voltage_v, concentration_cell_voltage_v
from .state import ExperimentState


SUPPORTED_NERNST_MODELS = {
    "ideal_concentration_cell_nernst",
    "reaction_quotient_concentration_cell_nernst",
}


def nernst_voltage_v(c_left_m: float, c_right_m: float, temperature_k: float) -> float:
    """兼容入口：两电子转移的理想浓度能斯特模型。"""
    return concentration_cell_voltage_v(c_left_m, c_right_m, temperature_k, electrons=2)


def _reaction_definition(cell: dict) -> tuple[dict[str, float], dict]:
    """读取通用反应商项；旧配置 active_species 对应固体金属电极的 Mⁿ⁺/M。"""
    redox = cell.get("redox_reaction") or {}
    configured_terms = redox.get("reaction_quotient_terms", cell.get("reaction_quotient_terms"))
    if configured_terms is None:
        active_species = cell.get("active_species", cell.get("copper_species"))
        configured_terms = {active_species: -1.0} if active_species else {}
    if not isinstance(configured_terms, dict):
        raise ValueError("reaction_quotient_terms 必须是物种 ID 到指数的对象")
    terms: dict[str, float] = {}
    for species, exponent_value in configured_terms.items():
        exponent = float(exponent_value)
        if not species or not math.isfinite(exponent) or exponent == 0:
            raise ValueError("反应商物种 ID 必须非空，指数必须有限且非零")
        terms[str(species)] = exponent
    return terms, redox


def _log_reaction_quotient(
    concentrations: dict[str, float],
    terms: dict[str, float],
    gammas: dict[str, float] | None = None,
) -> float:
    """以 1 mol/L 为标准态计算 ln(Q)；纯固体和电子不应列入 terms。"""
    result = 0.0
    gammas = gammas or {}
    for species, exponent in terms.items():
        concentration = float(concentrations[species])
        gamma = float(gammas.get(species, 1.0))
        if not math.isfinite(concentration) or concentration <= 0:
            raise ValueError(f"反应商物种 {species} 在两侧都必须有正浓度")
        if not math.isfinite(gamma) or gamma <= 0:
            raise ValueError(f"物种 {species} 的活度系数必须有限且大于零")
        result += exponent * math.log(concentration * gamma)
    return result


def _predict_concentration_cell_nernst(state: ExperimentState, cell: dict,
                                       species_definitions: dict | None = None) -> dict:
    """计算同一半反应在左右两侧形成的浓差电池电压。

    配置约定：
      - ``redox_reaction.electrons_transferred`` 是每个半反应的电子数 n；
      - ``reaction_quotient_terms`` 中产物指数为正、反应物指数为负；
      - 纯固体、纯液体、电子不写入反应商。
    因左右两侧使用相同半反应，标准电势相消，
    E_cell = E_right - E_left = -RT/(nF) ln(Q_right/Q_left)。
    """
    left = state.containers[cell["left_container"]]
    right = state.containers[cell["right_container"]]
    definitions = species_definitions or {}
    model_id = (cell.get("chemical_model_id") or cell.get("equilibrium_model")
                or getattr(state, "chemical_model_id", None)
                or "ideal_concentration_cell_nernst")
    redox = cell.get("redox_reaction") or {}
    try:
        terms, redox = _reaction_definition(cell)
    except (TypeError, ValueError) as exc:
        terms, redox = {}, cell.get("redox_reaction") or {}
        definition_error = str(exc)
    else:
        definition_error = None

    active_species = cell.get("active_species") or redox.get("display_species")
    if active_species not in terms:
        active_species = next(
            (species for species, exponent in terms.items() if exponent < 0),
            next(iter(terms), None),
        )
    electrons_value = redox.get("electrons_transferred", cell.get("electrons_transferred", 2))
    try:
        electrons = int(electrons_value)
        if electrons <= 0:
            raise ValueError("电子转移数必须大于零")
    except (TypeError, ValueError):
        electrons = 2
        definition_error = definition_error or "电子转移数必须是正整数"

    concentrations_by_side = {
        "left": {species: left.concentration_m(species) for species in terms},
        "right": {species: right.concentration_m(species) for species in terms},
    }
    local_ranges_by_side = {
        "left": {species: left.concentration_range_m(species) for species in terms},
        "right": {species: right.concentration_range_m(species) for species in terms},
    }
    reason = definition_error
    if not terms:
        reason = reason or "未配置半反应商物种；请配置 active_species 或 redox_reaction.reaction_quotient_terms"
    elif any(species not in definitions for species in terms):
        missing = sorted(set(terms) - set(definitions))
        reason = reason or f"反应商物种缺少 species 定义: {', '.join(missing)}"
    elif any(value is None or value <= 0 for side in concentrations_by_side.values() for value in side.values()):
        reason = reason or "半反应商中列出的物种在左右两侧都必须有正浓度"
    elif not state.cell_setup.get("electrodes_placed") or not state.cell_setup.get("salt_bridge_connected"):
        reason = reason or "电池尚未完成电极就位和盐桥连接"
    if model_id not in SUPPORTED_NERNST_MODELS:
        reason = reason or f"当前尚未注册电化学模型: {model_id}"

    activity_settings = cell.get("activity_correction", {})
    try:
        effective_activity_settings = dict(activity_settings)
        if activity_settings.get("enabled", False) and activity_settings.get("ionic_strength_source") == "tracked_species":
            ionic_species = activity_settings.get("ionic_strength_species", [])
            if not ionic_species:
                raise ValueError("离子强度来源设为 tracked_species，但未列出溶液中的离子物种")
            ionic_charges = {
                ion: int(definitions[ion]["charge"])
                for ion in ionic_species
                if ion in definitions and "charge" in definitions[ion]
            }
            if set(ionic_charges) != set(ionic_species):
                missing = sorted(set(ionic_species) - set(ionic_charges))
                raise ValueError(f"离子强度物种缺少电荷数配置: {', '.join(missing)}")
            effective_activity_settings["ionic_strength_molal_by_side"] = {
                "left": ionic_strength_molal(
                    {ion: left.concentration_m(ion) or 0.0 for ion in ionic_species}, ionic_charges
                ),
                "right": ionic_strength_molal(
                    {ion: right.concentration_m(ion) or 0.0 for ion in ionic_species}, ionic_charges
                ),
            }
        charges = {
            species: int(definitions[species]["charge"])
            for species in terms
            if species in definitions and "charge" in definitions[species]
        }
        if activity_settings.get("enabled", False) and set(charges) != set(terms):
            raise ValueError("反应商中的所有离子都需要配置电荷数，才能计算活度修正")
        activity_info = activity_coefficients_for_reaction(
            effective_activity_settings,
            charges=charges,
            reaction_quotient_terms=terms,
            temperature_k=state.temperature_k,
            electrons_transferred=electrons,
        )
    except (TypeError, ValueError, KeyError) as exc:
        activity_info = {
            "enabled": bool(activity_settings.get("enabled", False)),
            "model": activity_settings.get("model"),
            "status": "invalid_parameters",
            "gamma_left": None,
            "gamma_right": None,
            "gamma_by_species": {},
            "ionic_strength_molal_by_side": activity_settings.get("ionic_strength_molal_by_side"),
            "voltage_correction_mv": None,
            "assumptions": [],
        }
        reason = reason or f"活度修正参数无效：{exc}"

    junction_settings = cell.get("liquid_junction_potential")
    junction_concentrations = {
        side: {
            ion: state.containers[cell[f"{side}_container"]].concentration_m(ion) or 0.0
            for ion in definitions
        }
        for side in ("left", "right")
    }
    junction_info = evaluate_liquid_junction_potential(
        junction_settings,
        temperature_k=state.temperature_k,
        concentrations_by_side_m=junction_concentrations,
        species_definitions=definitions,
    )
    if junction_info["enabled"] and junction_info["correction_mv"] is None:
        reason = reason or junction_info["reason"]

    ideal_voltage = activity_voltage = voltage = None
    ideal_activity_left = ideal_activity_right = None
    activity_delta_mv = None
    try:
        if reason is None:
            log_q_left_ideal = _log_reaction_quotient(concentrations_by_side["left"], terms)
            log_q_right_ideal = _log_reaction_quotient(concentrations_by_side["right"], terms)
            ideal_voltage = -8.314462618 * state.temperature_k / (electrons * 96485.33212) * (
                log_q_right_ideal - log_q_left_ideal
            )
            gamma_by_species = activity_info.get("gamma_by_species", {})
            gamma_left = {ion: gamma_by_species.get(ion, {}).get("left", 1.0) for ion in terms}
            gamma_right = {ion: gamma_by_species.get(ion, {}).get("right", 1.0) for ion in terms}
            log_q_left_activity = _log_reaction_quotient(concentrations_by_side["left"], terms, gamma_left)
            log_q_right_activity = _log_reaction_quotient(concentrations_by_side["right"], terms, gamma_right)
            activity_voltage = -8.314462618 * state.temperature_k / (electrons * 96485.33212) * (
                log_q_right_activity - log_q_left_activity
            )
            activity_delta_mv = (activity_voltage - ideal_voltage) * 1000
            activity_info["voltage_correction_mv"] = activity_delta_mv
            junction_delta_v = (junction_info["correction_mv"] or 0.0) / 1000
            voltage = activity_voltage + junction_delta_v
            if active_species:
                primary_gamma_left = gamma_left.get(active_species, 1.0)
                primary_gamma_right = gamma_right.get(active_species, 1.0)
                ideal_activity_left = dimensionless_activity(
                    concentrations_by_side["left"][active_species], 1.0
                )
                ideal_activity_right = dimensionless_activity(
                    concentrations_by_side["right"][active_species], 1.0
                )
                activity_info["active_species_gamma_left"] = primary_gamma_left
                activity_info["active_species_gamma_right"] = primary_gamma_right
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        reason = reason or f"能斯特反应商计算失败：{exc}"
        ideal_voltage = activity_voltage = voltage = None
        activity_delta_mv = None

    local_voltage_range = None
    local_voltage_range_reason = None
    if voltage is None:
        local_voltage_range_reason = "理论电压尚不可计算"
    elif not terms or any(
        low is None or high is None or low <= 0
        for side in local_ranges_by_side.values()
        for low, high in side.values()
    ):
        local_voltage_range_reason = "反应商中至少一种物种的概念分区浓度含零，无法计算局部情景范围"
    elif len(terms) > 8:
        local_voltage_range_reason = "反应商物种较多；为避免组合爆炸，暂不枚举局部情景范围"
    else:
        gamma_by_species = activity_info.get("gamma_by_species", {})
        local_values = []
        endpoint_choices = []
        endpoint_keys = []
        for side in ("left", "right"):
            for species in terms:
                endpoint_keys.append((side, species))
                endpoint_choices.append(local_ranges_by_side[side][species])
        for endpoints in itertools.product(*endpoint_choices):
            endpoint_concentrations = {"left": {}, "right": {}}
            for (side, species), concentration in zip(endpoint_keys, endpoints):
                endpoint_concentrations[side][species] = concentration
            log_left = _log_reaction_quotient(
                endpoint_concentrations["left"], terms,
                {ion: gamma_by_species.get(ion, {}).get("left", 1.0) for ion in terms},
            )
            log_right = _log_reaction_quotient(
                endpoint_concentrations["right"], terms,
                {ion: gamma_by_species.get(ion, {}).get("right", 1.0) for ion in terms},
            )
            local_values.append(
                -8.314462618 * state.temperature_k / (electrons * 96485.33212)
                * (log_right - log_left) * 1000
                + (junction_info["correction_mv"] or 0.0)
            )
        local_voltage_range = {
            "minimum_mv": min(local_values),
            "maximum_mv": max(local_values),
        }

    assumptions = ["左右两侧按相同半反应建模；标准电势相消", "无负载；当前电压为开路理论值"]
    if activity_info["enabled"]:
        assumptions.extend(activity_info["assumptions"])
    else:
        assumptions.extend(["浓度近似活度", "活度系数取 1（理想基线）"])
    if junction_info["enabled"]:
        assumptions.extend(junction_info.get("assumptions", []))
    else:
        assumptions.append("液接电势未计入")

    primary_definition = definitions.get(active_species, {}) if active_species else {}
    return {
        "model": model_id,
        "valid": reason is None,
        "reason": reason,
        "polarity": "right_minus_left",
        "c_left_m": concentrations_by_side["left"].get(active_species),
        "c_right_m": concentrations_by_side["right"].get(active_species),
        "voltage_v": voltage,
        "voltage_mv": None if voltage is None else voltage * 1000,
        "voltage_basis": "相同半反应的能斯特反应商；按配置叠加活度修正及液接电势估算",
        "voltage_components_mv": {
            "ideal_nernst": None if ideal_voltage is None else ideal_voltage * 1000,
            "activity_correction": activity_delta_mv,
            "liquid_junction_potential": junction_info["correction_mv"],
            "reported_without_liquid_junction": voltage is not None and not junction_info["enabled"],
            "reported_includes_liquid_junction": voltage is not None and junction_info["enabled"],
        },
        "activity_correction": {
            **activity_info,
            "reaction_quotient_terms": terms,
            "active_species_charge": primary_definition.get("charge"),
            "activity_left": ideal_activity_left,
            "activity_right": ideal_activity_right,
        },
        "liquid_junction_potential": junction_info,
        "local_concentration_ranges_m": {
            "left": {
                "minimum": local_ranges_by_side["left"].get(active_species, (None, None))[0],
                "maximum": local_ranges_by_side["left"].get(active_species, (None, None))[1],
            },
            "right": {
                "minimum": local_ranges_by_side["right"].get(active_species, (None, None))[0],
                "maximum": local_ranges_by_side["right"].get(active_species, (None, None))[1],
            },
        },
        "local_concentration_spread_m": {
            "left": None if active_species is None else left.concentration_spread_m(active_species),
            "right": None if active_species is None else right.concentration_spread_m(active_species),
        },
        "local_voltage_range_mv": local_voltage_range,
        "local_voltage_range_reason": local_voltage_range_reason,
        "local_range_basis": "概念液体分区端点组合形成的能斯特情景边界；活度系数与按主体溶液计算的液接电势修正固定不变，不代表真实空间分布或统计区间",
        "data_origin": "simulated",
        "active_species": active_species,
        "active_species_name": primary_definition.get("name", active_species),
        "active_species_formula": primary_definition.get("formula", active_species),
        "redox_couple_name_zh": redox.get("name_zh", cell.get("redox_couple_name_zh")),
        "reduction_reaction_zh": redox.get("reduction_reaction_zh"),
        "reaction_quotient_terms": terms,
        "electrons_transferred": electrons,
        "assumptions": assumptions,
    }


def predict_voltage(state: ExperimentState, cell: dict,
                    species_definitions: dict | None = None) -> dict:
    """Dispatch voltage prediction through the registered chemical model.

    Existing Nernst configurations keep their original IDs and outputs. A
    Registered models may expose voltage, current or other structured
    observables through ``state.chemical_model_state``.
    """
    from .chemical_models import get_chemical_model

    model_id = (cell.get("chemical_model_id") or cell.get("equilibrium_model")
                or getattr(state, "chemical_model_id", None)
                or "ideal_concentration_cell_nernst")
    model = get_chemical_model(model_id)
    if model is None:
        # Preserve the legacy invalid-result response for unknown model IDs.
        return _predict_concentration_cell_nernst(state, cell, species_definitions)
    return model.observe(
        getattr(state, "chemical_model_state", {}), state, cell,
        species_definitions or {},
    )

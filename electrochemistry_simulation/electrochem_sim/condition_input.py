"""Build a single-run concentration-cell config from explicit UI conditions."""

from __future__ import annotations

import math
import json
from copy import deepcopy
from pathlib import Path

from .liquid_junction import NERNST_PLANCK_MODEL, SUPPORTED_MODEL

from .activity_temperature import davies_A_at_temperature
from .pipette_reference_model import aliquots_for_volume


TRANSFER_EPSILON_UL = 1e-7
ERROR_FACTOR_NAMES = ("pipetting", "tip_carryover", "mixing")
ERROR_LEVELS = {"low", "medium", "high"}
LIQUID_JUNCTION_MODEL_OPTIONS = {
    SUPPORTED_MODEL: {
        "name_zh": "Henderson 近似",
        "scope_zh": "适合快速估算连续/自由扩散液接；假设浓度近似线性变化、迁移率近似恒定、溶液近似理想，并按稳态开路（零电流）处理。",
    },
    NERNST_PLANCK_MODEL: {
        "name_zh": "Nernst–Planck 线性剖面数值积分（简化）",
        "scope_zh": "当前沿预设线性浓度剖面，对一维零电流 N–P 电场关系做数值积分；假设局部电中性，扩散系数由迁移率参考值换算。该版未求完整稳态各离子通量守恒，也未解 Poisson 空间电荷方程；相同线性剖面假设下应与 Henderson 结果一致。",
    },
}

BRIDGE_PRESETS = {
    "KNO3": {"formula": "KNO3", "ion_stoichiometry": {"K": 1, "NO3": 1}},
    "KCl": {"formula": "KCl", "ion_stoichiometry": {"K": 1, "Cl": 1}},
    "NaNO3": {"formula": "NaNO3", "ion_stoichiometry": {"Na": 1, "NO3": 1}},
}

BRIDGE_ION_DEFINITIONS = {
    "Cl": {"name": "Cl⁻", "formula": "Cl", "charge": -1},
    "Na": {"name": "Na⁺", "formula": "Na", "charge": 1},
}


def _finite_number(payload: dict, key: str, label: str, minimum: float) -> float:
    raw_value = payload.get(key)
    if isinstance(raw_value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        value = float(raw_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label}必须有限且不小于 {minimum:g}")
    return value


def _required_bool(payload: dict, key: str, label: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{label}必须是开/关值")
    return value


def _apply_operation_error_levels(config: dict, payload: dict) -> dict[str, str]:
    """Select factor-specific levels and load their parameters from the configured profile."""
    error_settings = config.setdefault("error_model", {})
    factors = error_settings.setdefault("factors", {})
    submitted = payload.get("operation_error_levels")
    if submitted is None:
        submitted = {
            name: factors.get(name, {}).get("level", "low")
            for name in ERROR_FACTOR_NAMES
        }
    if not isinstance(submitted, dict):
        raise ValueError("操作误差档位必须是对象")

    levels = {}
    for factor_name in ERROR_FACTOR_NAMES:
        level = submitted.get(factor_name, factors.get(factor_name, {}).get("level", "low"))
        if level not in ERROR_LEVELS:
            raise ValueError(f"{factor_name}误差档位必须选择低、中或高")
        levels[factor_name] = level

    profile_file = error_settings.get("profile_file")
    if not profile_file:
        if "operation_error_levels" in payload:
            raise ValueError("当前节点没有配置误差参数档案，暂不能切换误差档位")
        return levels
    profile_path = Path(__file__).resolve().parents[1] / "config" / profile_file
    if not profile_path.is_file():
        raise ValueError(f"找不到当前误差参数档案：{profile_file}")
    with profile_path.open(encoding="utf-8") as stream:
        catalog = json.load(stream)

    models = catalog.get("models", {})
    for factor_name, level in levels.items():
        selection = factors.setdefault(factor_name, {})
        model_name = selection.get("model")
        try:
            model_levels = models[factor_name][model_name]
            parameters = model_levels[level]
            if isinstance(parameters, dict) and parameters.get("inherits"):
                parent = model_levels[parameters["inherits"]]
                parameters = {**deepcopy(parent), **deepcopy(parameters)}
                parameters.pop("inherits", None)
        except (KeyError, TypeError) as exc:
            raise ValueError(f"当前误差档案没有该参数：{factor_name}/{model_name}/{level}") from exc
        selection["level"] = level
        selection["parameters"] = deepcopy(parameters)
        selection["parameter_source"] = (
            parameters.get("parameter_source")
            or catalog.get("factor_evidence", {}).get(factor_name, {}).get("status")
            or catalog.get("source", "unspecified")
        )
        selection.pop("status", None)
    error_settings["profile_set_id"] = catalog.get("profile_id")
    error_settings["profile_note"] = catalog.get("applicability", {}).get("evidence_scope_note")
    error_settings["applicability"] = deepcopy(catalog.get("applicability", {}))
    error_settings["factor_evidence"] = deepcopy(catalog.get("factor_evidence", {}))
    return levels


def apply_operation_error_levels(config: dict, levels: dict[str, str]) -> dict[str, str]:
    """Load profile-backed parameters for explicit operation-error tiers."""
    return _apply_operation_error_levels(config, {"operation_error_levels": levels})


def _find_preparation_sources(config: dict, species: str) -> tuple[str, str]:
    sources = {
        step.get("source")
        for step in config.get("protocol", [])
        if step.get("action") == "aspirate" and step.get("source") in config["containers"]
    }
    stock_candidates = []
    water_candidates = []
    for identifier in sources:
        concentrations = config["containers"][identifier].get("concentrations_m", {})
        active_concentration = float(concentrations.get(species, 0.0))
        if active_concentration > 0:
            stock_candidates.append((active_concentration, identifier))
        elif all(float(value) == 0 for value in concentrations.values()):
            water_candidates.append(identifier)
    if not stock_candidates:
        raise ValueError(f"当前配液步骤中找不到含有活性物种 {species} 的母液")
    if not water_candidates:
        raise ValueError("当前配液步骤中找不到纯水来源，暂不能按目标浓度自动配液")
    stock_candidates.sort(reverse=True)
    water_candidates.sort(key=lambda identifier: ("water" not in identifier.lower(), identifier))
    return stock_candidates[0][1], water_candidates[0]


def _append_transfer(
    protocol: list[dict],
    *,
    prefix: str,
    source: str,
    target: str,
    volume_ul: float,
) -> None:
    if volume_ul <= TRANSFER_EPSILON_UL:
        return
    aliquots = aliquots_for_volume(volume_ul)
    pipette_id = aliquots[0]["pipette_id"]
    protocol.append({"step_id": f"{prefix}_load_tip", "action": "load_tip",
                     "pipette_id": pipette_id})
    for index, aliquot in enumerate(aliquots, start=1):
        protocol.append({
            "step_id": f"{prefix}_aspirate_{index:02d}",
            "action": "aspirate",
            "source": source,
            "volume_ul": aliquot["volume_ul"],
            "pipette_id": aliquot["pipette_id"],
        })
        protocol.append({
            "step_id": f"{prefix}_dispense_{index:02d}",
            "action": "dispense",
            "target": target,
            "pipette_id": aliquot["pipette_id"],
        })
    protocol.append({"step_id": f"{prefix}_discard_tip", "action": "discard_tip",
                     "pipette_id": pipette_id})


def build_condition_config(base_config: dict, payload: dict) -> dict:
    """Return a validated, isolated config; never mutate the source config."""
    if not isinstance(payload, dict):
        raise ValueError("实验条件必须是 JSON 对象")
    required = {
        "left_concentration_m", "right_concentration_m",
        "left_volume_ml", "right_volume_ml", "temperature_c",
        "salt_bridge_type", "salt_bridge_concentration_m",
        "activity_correction_enabled", "liquid_junction_enabled", "errors_enabled",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"缺少实验条件：{', '.join(missing)}")

    config = deepcopy(base_config)
    cell = config["cell"]
    active_species = cell.get("active_species") or (cell.get("redox_reaction") or {}).get("display_species")
    if not active_species:
        raise ValueError("当前配置没有指定活性物种")
    stock_id, water_id = _find_preparation_sources(config, active_species)
    stock = config["containers"][stock_id]
    water = config["containers"][water_id]
    stock_concentration = float(stock.get("concentrations_m", {}).get(active_species, 0.0))
    if stock_concentration <= 0:
        raise ValueError("母液中的活性物种浓度必须大于零")

    concentrations = {
        "left": _finite_number(payload, "left_concentration_m", "左侧目标浓度", 0.0),
        "right": _finite_number(payload, "right_concentration_m", "右侧目标浓度", 0.0),
    }
    volumes = {
        "left": _finite_number(payload, "left_volume_ml", "左侧体积", 0.0),
        "right": _finite_number(payload, "right_volume_ml", "右侧体积", 0.0),
    }
    if any(value <= 0 for value in concentrations.values()):
        raise ValueError("两侧活性物种浓度都必须大于零")
    if any(value <= 0 for value in volumes.values()):
        raise ValueError("两侧最终体积都必须大于零")
    if any(value > stock_concentration for value in concentrations.values()):
        raise ValueError(f"目标浓度不能高于母液浓度 {stock_concentration:g} M")
    temperature_c = _finite_number(payload, "temperature_c", "温度", -273.15)
    temperature_k = temperature_c + 273.15
    if temperature_k <= 0:
        raise ValueError("温度必须高于绝对零度")

    bridge_type = payload.get("salt_bridge_type")
    if bridge_type not in BRIDGE_PRESETS:
        raise ValueError(f"暂不支持该盐桥类型：{bridge_type}")
    bridge_concentration = _finite_number(
        payload, "salt_bridge_concentration_m", "盐桥浓度", 0.0
    )
    if bridge_concentration <= 0:
        raise ValueError("盐桥浓度必须大于零")
    duration_payload = {**payload, "mixing_duration_s": payload.get("mixing_duration_s", 10.0)}
    mixing_duration_s = _finite_number(duration_payload, "mixing_duration_s", "混匀时间", 0.1)

    activity_enabled = _required_bool(payload, "activity_correction_enabled", "活度修正开关")
    junction_enabled = _required_bool(payload, "liquid_junction_enabled", "液接电势修正开关")
    junction_model = payload.get(
        "liquid_junction_model",
        config.get("cell", {}).get("liquid_junction_potential", {}).get("model", SUPPORTED_MODEL),
    )
    if junction_model not in LIQUID_JUNCTION_MODEL_OPTIONS:
        raise ValueError("液接电势模型请选择 Henderson 近似或 Nernst–Planck 数值积分")
    errors_enabled = _required_bool(payload, "errors_enabled", "操作误差开关")
    operation_error_levels = _apply_operation_error_levels(config, payload)

    cell_ids = {
        "left": cell["left_container"],
        "right": cell["right_container"],
    }
    stock_required_ml = 0.0
    water_required_ml = 0.0
    transfers = {}
    for side, target_id in cell_ids.items():
        target = config["containers"][target_id]
        if float(target.get("volume_ml", 0.0)) > 0:
            raise ValueError(f"{target_id} 初始容器非空，当前网页配液只支持空半电池")
        capacity_ml = target.get("capacity_ml")
        if capacity_ml is not None and volumes[side] > float(capacity_ml):
            raise ValueError(f"{side}侧目标体积超过容器容量 {float(capacity_ml):g} mL")
        stock_ml = concentrations[side] * volumes[side] / stock_concentration
        water_ml = volumes[side] - stock_ml
        stock_required_ml += stock_ml
        water_required_ml += water_ml
        transfers[side] = (stock_ml * 1000.0, water_ml * 1000.0)
        target["volume_ml"] = 0.0

    available_stock_ml = float(stock.get("volume_ml", 0.0))
    available_water_ml = float(water.get("volume_ml", 0.0))
    if stock_required_ml > available_stock_ml + 1e-9:
        raise ValueError(
            f"母液不足：需要 {stock_required_ml:.4g} mL，当前配置有 {available_stock_ml:.4g} mL"
        )
    if water_required_ml > available_water_ml + 1e-9:
        raise ValueError(
            f"纯水不足：需要 {water_required_ml:.4g} mL，当前配置有 {available_water_ml:.4g} mL"
        )

    protocol: list[dict] = []
    for side, target_id in cell_ids.items():
        stock_ul, water_ul = transfers[side]
        _append_transfer(
            protocol, prefix=f"ui_{side}_stock", source=stock_id,
            target=target_id, volume_ul=stock_ul,
        )
        _append_transfer(
            protocol, prefix=f"ui_{side}_water", source=water_id,
            target=target_id, volume_ul=water_ul,
        )
    protocol.append({
        "step_id": "ui_simultaneous_mix",
        "action": "mix_pair",
        "targets": [cell_ids["left"], cell_ids["right"]],
        "duration_s": mixing_duration_s,
    })
    protocol.extend(
        deepcopy(step)
        for step in base_config.get("protocol", [])
        if step.get("action") in {"place_electrodes", "connect_salt_bridge"}
    )
    if not any(step["action"] == "place_electrodes" for step in protocol):
        protocol.append({"step_id": "ui_place_electrodes", "action": "place_electrodes"})
    if not any(step["action"] == "connect_salt_bridge" for step in protocol):
        protocol.append({"step_id": "ui_connect_salt_bridge", "action": "connect_salt_bridge"})
    for step in protocol:
        if step.get("action") == "connect_salt_bridge":
            step["component"] = bridge_type
    config["protocol"] = protocol
    config["temperature_k"] = temperature_k

    activity_settings = config.setdefault("cell", {}).setdefault("activity_correction", {})
    activity_settings["enabled"] = activity_enabled
    if activity_enabled:
        temperature_parameter = davies_A_at_temperature(temperature_k)
        activity_settings["A"] = temperature_parameter["A"]
        activity_settings["A_reference_temperature_k"] = temperature_k
        activity_settings["condition_temperature_parameter_selection"] = temperature_parameter
    junction_settings = config["cell"].setdefault("liquid_junction_potential", {})
    junction_settings["enabled"] = junction_enabled
    junction_settings["model"] = junction_model
    # Persist an explicitly selected policy so archives do not rely on a later default.
    junction_policy = payload.get("liquid_junction_parameter_use_policy",
                                  junction_settings.get("parameter_use_policy", "condition_matched_only"))
    if "liquid_junction_parameter_use_policy" in payload and junction_policy not in {
            "allow_reference_estimate", "condition_matched_only",
            "reference_only_no_concentration_interpolation_or_extrapolation"}:
        raise ValueError("未知液接参数使用策略")
    junction_settings["parameter_use_policy"] = junction_policy
    bridge = BRIDGE_PRESETS[bridge_type]
    junction_settings["bridge_electrolyte"] = {
        "electrolytes": [{
            "formula": bridge["formula"],
            "concentration_m": bridge_concentration,
            "ion_stoichiometry": deepcopy(bridge["ion_stoichiometry"]),
        }]
    }
    for ion_id, definition in BRIDGE_ION_DEFINITIONS.items():
        if ion_id in bridge["ion_stoichiometry"]:
            config.setdefault("species", {}).setdefault(ion_id, deepcopy(definition))
    cell_setup = config.setdefault("cell_setup", {}).setdefault("salt_bridge", {})
    cell_setup.update({
        "type": bridge_type,
        "concentration_m": bridge_concentration,
        "status": "assumed_for_simulation",
    })
    config.setdefault("error_model", {})["enabled"] = errors_enabled
    config["condition_inputs"] = {
        "left_concentration_m": concentrations["left"],
        "right_concentration_m": concentrations["right"],
        "left_volume_ml": volumes["left"],
        "right_volume_ml": volumes["right"],
        "temperature_c": temperature_c,
        "salt_bridge_type": bridge_type,
        "salt_bridge_concentration_m": bridge_concentration,
        "mixing_duration_s": mixing_duration_s,
        "activity_correction_enabled": activity_enabled,
        "liquid_junction_enabled": junction_enabled,
        "liquid_junction_model": junction_model,
        "liquid_junction_parameter_use_policy": junction_policy,
        "errors_enabled": errors_enabled,
        "operation_error_levels": operation_error_levels,
    }
    short_name = config.get("experiment_name_short_zh", "浓差电池")
    concentration_tag = f"左{concentrations['left']:g}M_右{concentrations['right']:g}M"
    temperature_tag = f"{temperature_c:g}C"
    config["experiment_name_short_zh"] = f"{short_name}_{concentration_tag}_{temperature_tag}"
    return config


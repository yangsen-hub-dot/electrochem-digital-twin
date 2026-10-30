"""Configurable batch matrix for temperature, solution, bridge and error factors."""

from __future__ import annotations

import argparse
import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .activity_temperature import davies_A_at_temperature
from .condition_input import (
    BRIDGE_ION_DEFINITIONS,
    BRIDGE_PRESETS,
    LIQUID_JUNCTION_MODEL_OPTIONS,
    apply_operation_error_levels,
)
from .result_recorder import build_result_filename, result_date_directory, save_result
from .simulator import PROJECT_ROOT, load_config, run_experiment
from .temperature_concentration_batch import (
    _active_species,
    _nominal_target_solution,
    make_scenario_config,
    summarize_result,
)

ERROR_FACTOR_NAMES = ("pipetting", "tip_carryover", "mixing")
ERROR_LEVELS = {"low", "medium", "high"}
DEFAULT_MAX_SCENARIOS = 500


def _finite_positive(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正数")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须是有限正数")
    return number


def _values(settings: dict, key: str, default: list, label: str) -> list:
    value = settings.get(key, default)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label}必须是非空列表")
    return value


def _number_values(settings: dict, key: str, default: list, label: str) -> list[float]:
    result = [_finite_positive(value, label) for value in _values(settings, key, default, label)]
    if len(set(result)) != len(result):
        raise ValueError(f"{label}不能重复")
    return result


def _boolean_values(settings: dict, key: str, default: list[bool], label: str) -> list[bool]:
    result = _values(settings, key, default, label)
    if any(not isinstance(value, bool) for value in result):
        raise ValueError(f"{label}只能填写 true 或 false")
    if len(set(result)) != len(result):
        raise ValueError(f"{label}不能重复")
    return result


def _unique_values(values: list, label: str) -> list:
    if len(set(values)) != len(values):
        raise ValueError(f"{label}不能重复")
    return values


def _base_condition_values(base_config: dict) -> dict:
    active_species = _active_species(base_config)
    left_id = base_config["cell"]["left_container"]
    right_id = base_config["cell"]["right_container"]
    left_nominal = _nominal_target_solution(base_config, left_id, active_species)
    right_nominal = _nominal_target_solution(base_config, right_id, active_species)
    if left_nominal is None or right_nominal is None:
        raise ValueError("无法从基础配液流程读取两侧目标浓度和体积")
    bridge = base_config.get("cell_setup", {}).get("salt_bridge", {})
    cell = base_config.get("cell", {})
    activity = cell.get("activity_correction", {})
    junction = cell.get("liquid_junction_potential", {})
    errors = base_config.get("error_model", {})
    factors = errors.get("factors", {})
    return {
        "temperature_k": float(base_config.get("temperature_k", 298.15)),
        "left_concentration_m": left_nominal[0],
        "right_concentration_m": right_nominal[0],
        "left_total_volume_ml": left_nominal[1],
        "right_total_volume_ml": right_nominal[1],
        "salt_bridge_type": bridge.get("type", "KNO3"),
        "salt_bridge_concentration_m": float(bridge.get("concentration_m", 0.1)),
        "activity_correction_enabled": bool(activity.get("enabled", False)),
        "liquid_junction_enabled": bool(junction.get("enabled", False)),
        "liquid_junction_model": junction.get("model", "henderson_approximation"),
        "errors_enabled": bool(errors.get("enabled", False)),
        "operation_error_levels": {
            name: factors.get(name, {}).get("level", "low")
            for name in ERROR_FACTOR_NAMES
        },
    }


def _default_preparation_step_ids(base_config: dict) -> tuple[str, str]:
    """Find left-side stock and water aspiration steps in the supplied protocol."""
    active_species = _active_species(base_config)
    left_container = base_config["cell"]["left_container"]
    stock_candidates: list[tuple[float, str]] = []
    water_candidates: list[str] = []
    pending_aspiration = None

    for step in base_config.get("protocol", []):
        if step.get("action") == "aspirate":
            pending_aspiration = step
        elif step.get("action") == "dispense":
            if pending_aspiration is not None and step.get("target") == left_container:
                source_id = pending_aspiration.get("source")
                source = base_config.get("containers", {}).get(source_id, {})
                concentrations = source.get("concentrations_m", {})
                active_concentration = float(concentrations.get(active_species, 0.0))
                step_id = pending_aspiration.get("step_id")
                if step_id:
                    if active_concentration > 0:
                        stock_candidates.append((active_concentration, str(step_id)))
                    elif all(float(value) == 0 for value in concentrations.values()):
                        water_candidates.append(str(step_id))
            pending_aspiration = None

    if not stock_candidates or not water_candidates:
        raise ValueError("无法从左侧配液流程识别母液和纯水吸液步骤")
    # Prefer the most concentrated active-species source and a water-named source.
    stock_candidates.sort(key=lambda item: (-item[0], item[1]))
    water_candidates.sort(key=lambda step_id: ("water" not in step_id.lower(), step_id))
    return stock_candidates[0][1], water_candidates[0]


def _scan_axes(base_config: dict, settings: dict) -> tuple[list[tuple], dict]:
    base = _base_condition_values(base_config)
    temperatures = _number_values(settings, "temperatures_k", [base["temperature_k"]], "扫描温度")
    left_concentrations = _number_values(
        settings, "left_concentrations_m", [base["left_concentration_m"]], "左侧扫描浓度"
    )
    right_values = settings.get("right_concentrations_m")
    right_concentrations = (
        _number_values(settings, "right_concentrations_m", [], "右侧扫描浓度")
        if right_values is not None else [None]
    )
    bridge_types = _unique_values(
        _values(settings, "salt_bridge_types", [base["salt_bridge_type"]], "盐桥种类"), "盐桥种类"
    )
    if any(value not in BRIDGE_PRESETS for value in bridge_types):
        raise ValueError(f"盐桥种类只能选择：{', '.join(BRIDGE_PRESETS)}")
    bridge_concentrations = _number_values(
        settings, "salt_bridge_concentrations_m", [base["salt_bridge_concentration_m"]], "盐桥浓度"
    )
    activity_enabled = _boolean_values(
        settings, "activity_correction_enabled", [base["activity_correction_enabled"]], "活度修正开关"
    )
    junction_enabled = _boolean_values(
        settings, "liquid_junction_enabled", [base["liquid_junction_enabled"]], "液接电势修正开关"
    )
    junction_models = _unique_values(
        _values(settings, "liquid_junction_models", [base["liquid_junction_model"]], "液接电势模型"),
        "液接电势模型",
    )
    if any(value not in LIQUID_JUNCTION_MODEL_OPTIONS for value in junction_models):
        raise ValueError("液接电势模型包含未知选项")
    errors_enabled = _boolean_values(
        settings, "errors_enabled", [base["errors_enabled"]], "操作误差开关"
    )

    error_axes = {}
    raw_levels = settings.get("operation_error_levels", {})
    if not isinstance(raw_levels, dict):
        raise ValueError("operation_error_levels 必须是对象")
    unknown_factors = set(raw_levels) - set(ERROR_FACTOR_NAMES)
    if unknown_factors:
        raise ValueError(f"未知操作误差项：{', '.join(sorted(unknown_factors))}")
    for name in ERROR_FACTOR_NAMES:
        values = raw_levels.get(name, [base["operation_error_levels"][name]])
        if not isinstance(values, list) or not values or any(value not in ERROR_LEVELS for value in values):
            raise ValueError(f"{name}档位必须从低、中、高中选择")
        error_axes[name] = _unique_values(values, f"{name}档位")
    if any(len(values) > 1 for values in error_axes.values()) and True not in errors_enabled:
        raise ValueError("扫描误差档位时，errors_enabled 必须包含 true")

    if settings.get("stock_aspirate_step_id") and settings.get("water_aspirate_step_id"):
        default_stock_step_id = str(settings["stock_aspirate_step_id"])
        default_water_step_id = str(settings["water_aspirate_step_id"])
    else:
        inferred_stock_step_id, inferred_water_step_id = _default_preparation_step_ids(base_config)
        default_stock_step_id = str(settings.get("stock_aspirate_step_id", inferred_stock_step_id))
        default_water_step_id = str(settings.get("water_aspirate_step_id", inferred_water_step_id))

    replicates = settings.get("replicates", 1)
    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
        raise ValueError("replicates 必须是大于或等于 1 的整数")
    maximum = settings.get("max_scenarios", DEFAULT_MAX_SCENARIOS)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 5000:
        raise ValueError("max_scenarios 必须是 1–5000 的整数")

    axes = [
        temperatures, left_concentrations, right_concentrations,
        bridge_types, bridge_concentrations, activity_enabled,
        junction_enabled, junction_models, errors_enabled,
        error_axes["pipetting"], error_axes["tip_carryover"], error_axes["mixing"],
    ]
    matrix_size = math.prod(len(axis) for axis in axes) * replicates
    if matrix_size > maximum:
        raise ValueError(
            f"当前条件组合为 {matrix_size} 组，超过 max_scenarios={maximum}；"
            "请减少扫描维度/取值，或明确提高上限（最多 5000）"
        )
    return list(product(*axes)), {
        "base": base,
        "replicates": replicates,
        "matrix_size": matrix_size,
        "left_total_volume_ml": _finite_positive(
            settings.get("left_total_volume_ml", base["left_total_volume_ml"]), "左侧最终总体积"
        ),
        "right_total_volume_ml": settings.get("right_total_volume_ml", base["right_total_volume_ml"]),
        "stock_step_id": default_stock_step_id,
        "water_step_id": default_water_step_id,
        "batch_short_name": settings.get("batch_name_short_zh", "浓差电池_条件扫描"),
    }


def estimate_experimental_design_scenario_count(base_config: dict, settings: dict) -> int:
    """Validate the selected factors and return the number of runs before starting."""
    _, metadata = _scan_axes(base_config, settings)
    return metadata["matrix_size"]


def _apply_factor_settings(
    case: dict,
    *,
    temperature_k: float,
    bridge_type: str,
    bridge_concentration_m: float,
    activity_enabled: bool,
    junction_enabled: bool,
    junction_model: str,
    errors_enabled: bool,
    error_levels: dict[str, str],
) -> None:
    cell = case.setdefault("cell", {})
    activity = cell.setdefault("activity_correction", {})
    activity["enabled"] = activity_enabled
    if activity_enabled:
        try:
            selection = davies_A_at_temperature(temperature_k)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            selection = {
                "status": "unsupported_temperature",
                "method": "not_available_no_extrapolation",
                "temperature_k": temperature_k,
                "reason": str(exc),
                "A": None,
            }
        activity["batch_temperature_parameter_selection"] = selection
        if selection.get("A") is not None:
            activity["A"] = selection["A"]
            activity["A_reference_temperature_k"] = temperature_k

    junction = cell.setdefault("liquid_junction_potential", {})
    junction["enabled"] = junction_enabled
    junction["model"] = junction_model
    bridge = BRIDGE_PRESETS[bridge_type]
    junction["bridge_electrolyte"] = {
        "electrolytes": [{
            "formula": bridge["formula"],
            "concentration_m": bridge_concentration_m,
            "ion_stoichiometry": deepcopy(bridge["ion_stoichiometry"]),
        }]
    }
    for ion_id, definition in BRIDGE_ION_DEFINITIONS.items():
        if ion_id in bridge["ion_stoichiometry"]:
            case.setdefault("species", {}).setdefault(ion_id, deepcopy(definition))

    cell_setup = case.setdefault("cell_setup", {}).setdefault("salt_bridge", {})
    cell_setup.update({
        "type": bridge_type,
        "concentration_m": bridge_concentration_m,
        "status": "assumed_for_simulation",
    })
    for step in case.get("protocol", []):
        if step.get("action") == "connect_salt_bridge":
            step["component"] = bridge_type

    case.setdefault("error_model", {})["enabled"] = errors_enabled
    if case.get("error_model", {}).get("profile_file"):
        apply_operation_error_levels(case, error_levels)
    else:
        configured_levels = {
            name: case.get("error_model", {}).get("factors", {}).get(name, {}).get("level", "low")
            for name in ERROR_FACTOR_NAMES
        }
        if error_levels != configured_levels:
            raise ValueError("当前基础配置没有可切换档位的操作误差参数档案")

    case["condition_inputs"] = {
        **case.get("condition_inputs", {}),
        "temperature_c": temperature_k - 273.15,
        "salt_bridge_type": bridge_type,
        "salt_bridge_concentration_m": bridge_concentration_m,
        "activity_correction_enabled": activity_enabled,
        "liquid_junction_enabled": junction_enabled,
        "liquid_junction_model": junction_model,
        "errors_enabled": errors_enabled,
        "operation_error_levels": deepcopy(error_levels),
    }


def run_experimental_design_batch(
    base_config: dict,
    settings: dict,
    output_dir: Path,
    seed: int = 0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[Path, list[dict]]:
    """Run a selected Cartesian matrix; values with one item remain fixed conditions."""
    if not isinstance(settings, dict):
        raise ValueError("条件组合扫描配置顶层必须是对象")
    combinations, metadata = _scan_axes(base_config, settings)
    right_total_volume = metadata["right_total_volume_ml"]
    if settings.get("right_concentrations_m") is not None:
        right_total_volume = _finite_positive(right_total_volume, "右侧最终总体积")

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    replicate_count = metadata["replicates"]
    scenario_index = 0
    for combination in combinations:
        (
            temperature_k, left_m, right_m, bridge_type, bridge_m,
            activity_enabled, junction_enabled, junction_model, errors_enabled,
            pipetting_level, carryover_level, mixing_level,
        ) = combination
        error_levels = {
            "pipetting": pipetting_level,
            "tip_carryover": carryover_level,
            "mixing": mixing_level,
        }
        case = make_scenario_config(
            base_config,
            temperature_k=temperature_k,
            left_concentration_m=left_m,
            left_total_volume_ml=metadata["left_total_volume_ml"],
            stock_step_id=metadata["stock_step_id"],
            water_step_id=metadata["water_step_id"],
            right_concentration_m=right_m,
            right_total_volume_ml=right_total_volume if right_m is not None else None,
        )
        _apply_factor_settings(
            case,
            temperature_k=temperature_k,
            bridge_type=bridge_type,
            bridge_concentration_m=bridge_m,
            activity_enabled=activity_enabled,
            junction_enabled=junction_enabled,
            junction_model=junction_model,
            errors_enabled=errors_enabled,
            error_levels=error_levels,
        )
        for replicate in range(1, replicate_count + 1):
            scenario_index += 1
            replicate_case = deepcopy(case)
            suffix = f"_桥{bridge_type}_{bridge_m:g}M_活度{'开' if activity_enabled else '关'}_液接{'开' if junction_enabled else '关'}_误差{'开' if errors_enabled else '关'}_R{replicate:02d}"
            replicate_case["experiment_id"] = f"{case['experiment_id']}__design_{scenario_index:04d}_r{replicate:02d}"
            replicate_case["experiment_name_zh"] = f"{case.get('experiment_name_zh', '浓差电池仿真')}{suffix}"
            replicate_case["experiment_name_short_zh"] = (
                f"{case.get('experiment_name_short_zh', '浓差电池')}_矩阵{scenario_index:04d}_R{replicate:02d}"
            )
            replicate_case["result_category_zh"] = "实验条件组合批量仿真情景"
            replicate_case["result_category_short_zh"] = "批"
            replicate_case["description"] = (
                f"条件组合批量仿真：{temperature_k:g} K，左侧 {left_m:g} M，"
                f"盐桥 {bridge_type} {bridge_m:g} M，活度修正{'开' if activity_enabled else '关'}，"
                f"液接修正{'开' if junction_enabled else '关'}（{junction_model}），"
                f"操作误差{'开' if errors_enabled else '关'}，重复 {replicate}/{replicate_count}。"
                "情景由基础配置副本独立生成；纯仿真，非实测。"
            )
            scenario_seed = seed + scenario_index - 1
            result = run_experiment(replicate_case, seed=scenario_seed)
            detail_path = save_result(result, output_dir)
            row = summarize_result(
                replicate_case, result, detail_path, output_dir,
                scenario_seed, left_m, right_m,
            )
            row.update({
                "matrix_index": scenario_index,
                "replicate": replicate,
                "replicate_count": replicate_count,
                "temperature_k": temperature_k,
                "target_c_left_m": left_m,
                "target_c_right_m": right_m,
                "salt_bridge_type": bridge_type,
                "salt_bridge_concentration_m": bridge_m,
                "activity_correction_enabled": activity_enabled,
                "liquid_junction_enabled": junction_enabled,
                "liquid_junction_model": junction_model,
                "errors_enabled": errors_enabled,
                "pipetting_level": pipetting_level,
                "tip_carryover_level": carryover_level,
                "mixing_level": mixing_level,
            })
            rows.append(row)
            if progress_callback is not None:
                progress_callback(scenario_index, metadata["matrix_size"])

    created_at = datetime.now(timezone.utc).isoformat()
    summary_path = result_date_directory(output_dir, created_at) / build_result_filename(
        "批", metadata["batch_short_name"], created_at, uuid4().hex, "csv"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return summary_path, rows


def _load_settings(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        settings = json.load(stream)
    if not isinstance(settings, dict):
        raise ValueError("条件组合扫描配置 JSON 顶层必须是对象")
    return settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按配置选择扫描温度、浓度、盐桥、修正项和操作误差档位；未选因子沿用基础配置。"
    )
    parser.add_argument("--base-config", type=Path, default=PROJECT_ROOT / "config" / "copper_concentration_cell.json")
    parser.add_argument("--batch-config", type=Path, default=PROJECT_ROOT / "config" / "copper_experimental_design_sweep.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    try:
        base_config = load_config(args.base_config)
        settings = _load_settings(args.batch_config)
        summary_path, rows = run_experimental_design_batch(
            base_config, settings, args.output_dir, seed=args.seed
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"条件组合批量仿真未完成：{exc}\n")
    completed = sum(row["scenario_status"] == "completed" for row in rows)
    print(f"条件组合批量仿真：{len(rows)} 组（成功 {completed} 组）")
    print(f"CSV 汇总：{summary_path}")
    print("CSV 保存了每组条件、修正项、误差档位和重复编号；图形界面可选择“自定义条件组合”。")


if __name__ == "__main__":
    main()

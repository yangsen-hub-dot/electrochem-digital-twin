"""本地温度与左右浓度组合扫描；不修改基础配置或现有浓度扫描入口。"""

from __future__ import annotations

import argparse
import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from uuid import uuid4

from .activity_temperature import davies_A_at_temperature
from .batch import find_aspirate_step, make_case, positive_finite
from .condition_input import LIQUID_JUNCTION_MODEL_OPTIONS
from .result_recorder import build_result_filename, result_date_directory, save_result
from .simulator import PROJECT_ROOT, load_config, run_experiment


def _number_list(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} 必须是非空列表")
    numbers = [positive_finite(item, label) for item in value]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"{label}不能重复")
    return numbers


def _active_species(config: dict) -> str:
    species = config["cell"].get("active_species") or config["cell"].get("redox_reaction", {}).get("display_species")
    if not species:
        raise ValueError("批量扫描需要配置 active_species 或 redox_reaction.display_species")
    return species


def _nominal_target_solution(config: dict, container_id: str, species_id: str) -> tuple[float, float] | None:
    """Calculate configured concentration and volume from nominal transfer steps."""
    volume_ul = 0.0
    amount_umol = 0.0
    pending: tuple[str, float] | None = None
    for step in config.get("protocol", []):
        action = step.get("action")
        if action == "aspirate":
            pending = (step["source"], float(step["volume_ul"]))
        elif action == "dispense" and step.get("target") == container_id and pending is not None:
            source, transfer_ul = pending
            concentration_m = float(config["containers"][source]["concentrations_m"].get(species_id, 0.0))
            volume_ul += transfer_ul
            amount_umol += concentration_m * transfer_ul
            pending = None
    if volume_ul <= 0:
        return None
    return amount_umol / volume_ul, volume_ul / 1000.0


def _nominal_target_concentration(config: dict, container_id: str, species_id: str) -> float | None:
    solution = _nominal_target_solution(config, container_id, species_id)
    return None if solution is None else solution[0]


def _right_transfer_steps(
    *,
    stock_source: str,
    water_source: str,
    right_container: str,
    stock_volume_ul: float,
    water_volume_ul: float,
) -> list[dict]:
    steps = [
        {"step_id": "batch_right_stock_load_tip", "action": "load_tip"},
        {
            "step_id": "batch_right_stock_aspirate",
            "action": "aspirate",
            "source": stock_source,
            "volume_ul": stock_volume_ul,
        },
        {"step_id": "batch_right_stock_dispense", "action": "dispense", "target": right_container},
        {"step_id": "batch_right_stock_discard_tip", "action": "discard_tip"},
    ]
    if water_volume_ul > 1e-9:
        steps.extend([
            {"step_id": "batch_right_water_load_tip", "action": "load_tip"},
            {
                "step_id": "batch_right_water_aspirate",
                "action": "aspirate",
                "source": water_source,
                "volume_ul": water_volume_ul,
            },
            {"step_id": "batch_right_water_dispense", "action": "dispense", "target": right_container},
            {"step_id": "batch_right_water_discard_tip", "action": "discard_tip"},
        ])
    return steps


def set_right_target_concentration(
    case: dict,
    concentration_m: float,
    total_volume_ml: float,
    stock_source: str,
    water_source: str,
) -> None:
    """Replace the right-side preparation steps in one scenario copy."""
    if stock_source == water_source:
        raise ValueError("母液步骤和水步骤必须使用不同来源容器")

    species_id = _active_species(case)
    stock_concentration = positive_finite(
        case["containers"][stock_source]["concentrations_m"][species_id], "母液浓度"
    )
    total_volume_ul = positive_finite(total_volume_ml, "右侧目标总体积") * 1000.0
    stock_volume_ul = concentration_m / stock_concentration * total_volume_ul
    water_volume_ul = total_volume_ul - stock_volume_ul
    if water_volume_ul < -1e-9:
        raise ValueError(
            f"右侧目标浓度 {concentration_m:g} M 高于母液浓度 {stock_concentration:g} M"
        )
    if water_volume_ul < 0:
        water_volume_ul = 0.0

    left_container = case["cell"]["left_container"]
    right_container = case["cell"]["right_container"]
    protocol = case["protocol"]
    left_mix = [i for i, step in enumerate(protocol) if step.get("action") == "mix" and step.get("target") == left_container]
    right_mix = [i for i, step in enumerate(protocol) if step.get("action") == "mix" and step.get("target") == right_container]
    if len(left_mix) != 1 or len(right_mix) != 1 or left_mix[0] >= right_mix[0]:
        raise ValueError("无法在基础 protocol 中唯一定位左右侧混匀步骤")

    protocol[left_mix[0] + 1:right_mix[0]] = _right_transfer_steps(
        stock_source=stock_source,
        water_source=water_source,
        right_container=right_container,
        stock_volume_ul=stock_volume_ul,
        water_volume_ul=water_volume_ul,
    )


def make_scenario_config(
    base_config: dict,
    *,
    temperature_k: float,
    left_concentration_m: float,
    left_total_volume_ml: float,
    stock_step_id: str,
    water_step_id: str,
    right_concentration_m: float | None = None,
    right_total_volume_ml: float | None = None,
) -> dict:
    """Create a fresh, isolated case for one temperature/concentration combination."""
    base_stock_step = find_aspirate_step(base_config, stock_step_id)
    base_water_step = find_aspirate_step(base_config, water_step_id)
    case = make_case(
        base_config,
        concentration_m=left_concentration_m,
        total_volume_ml=left_total_volume_ml,
        stock_step_id=stock_step_id,
        water_step_id=water_step_id,
    )
    case["temperature_k"] = temperature_k
    activity_settings = case.get("cell", {}).get("activity_correction")
    if isinstance(activity_settings, dict) and activity_settings.get("enabled", False):
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
        if selection["A"] is not None:
            activity_settings["A"] = selection["A"]
            activity_settings["A_reference_temperature_k"] = temperature_k
        activity_settings["batch_temperature_parameter_selection"] = selection
    if right_concentration_m is not None:
        if right_total_volume_ml is None:
            raise ValueError("扫描右侧浓度时必须配置 right_total_volume_ml")
        base_right = _nominal_target_solution(
            base_config, base_config["cell"]["right_container"], _active_species(base_config)
        )
        same_as_base = (
            base_right is not None
            and math.isclose(right_concentration_m, base_right[0], rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(right_total_volume_ml, base_right[1], rel_tol=0.0, abs_tol=1e-9)
        )
        if not same_as_base:
            set_right_target_concentration(
                case,
                concentration_m=right_concentration_m,
                total_volume_ml=right_total_volume_ml,
                stock_source=base_stock_step["source"],
                water_source=base_water_step["source"],
            )

    suffix = f"T{temperature_k:g}K_L{left_concentration_m:g}M"
    if right_concentration_m is not None:
        suffix += f"_R{right_concentration_m:g}M"
    case["experiment_id"] = f"{base_config['experiment_id']}__{suffix}"
    case["experiment_name_zh"] = f"{base_config.get('experiment_name_zh', '浓差电池仿真')}_{suffix}"
    base_short_name = base_config.get("experiment_name_short_zh", "浓差电池")
    case["experiment_name_short_zh"] = f"{base_short_name}_{suffix}"
    case["result_category_zh"] = "温度浓度批量仿真情景"
    case["result_category_short_zh"] = "批"
    condition_inputs = case.get("condition_inputs")
    if not isinstance(condition_inputs, dict):
        condition_inputs = {}
        case["condition_inputs"] = condition_inputs
    condition_inputs.update({
        "left_concentration_m": left_concentration_m,
        "left_volume_ml": left_total_volume_ml,
        "temperature_c": temperature_k - 273.15,
    })
    if right_concentration_m is not None:
        condition_inputs.update({
            "right_concentration_m": right_concentration_m,
            "right_volume_ml": right_total_volume_ml,
        })
    else:
        base_right = _nominal_target_solution(
            base_config,
            base_config["cell"]["right_container"],
            _active_species(base_config),
        )
        if base_right is not None:
            condition_inputs.update({
                "right_concentration_m": base_right[0],
                "right_volume_ml": base_right[1],
            })
    case["description"] = (
        f"温度×浓度批量扫描：T={temperature_k:g} K，左侧目标浓度 {left_concentration_m:g} M；"
        + (f"右侧目标浓度 {right_concentration_m:g} M。" if right_concentration_m is not None
           else "右侧配液步骤及浓度沿用基础配置。")
        + "每个情景由基础配置副本独立生成。纯仿真情景。"
    )
    return case


def _ideal_baseline(case: dict, seed: int) -> dict:
    baseline_config = deepcopy(case)
    activity = baseline_config.get("cell", {}).get("activity_correction")
    if isinstance(activity, dict):
        activity["enabled"] = False
    junction = baseline_config.get("cell", {}).get("liquid_junction_potential")
    if isinstance(junction, dict):
        junction["enabled"] = False
    return run_experiment(baseline_config, seed=seed)


def _temperature_parameter_note(case: dict, prediction: dict) -> str:
    notes: list[str] = []
    temperature = float(case["temperature_k"])
    activity = case.get("cell", {}).get("activity_correction", {})
    if activity.get("enabled", False):
        selection = activity.get("batch_temperature_parameter_selection", {})
        if selection.get("status") == "supported_exact":
            notes.append(
                f"Davies A={selection['A']:g} 使用来源表值（{temperature:g} K）"
            )
        elif selection.get("status") == "supported_interpolated":
            notes.append(
                f"Davies A={selection['A']:g} 使用来源表范围内线性插值（{temperature:g} K）"
            )
        elif selection.get("status") == "unsupported_temperature":
            notes.append(f"Davies A 温度参数不适用：{selection.get('reason', '来源数据未覆盖')}")
        else:
            reference = float(activity.get("A_reference_temperature_k", 298.15))
            if not math.isclose(temperature, reference, rel_tol=0.0, abs_tol=1e-6):
                notes.append(f"Davies A 参考温度为 {reference:g} K，当前 {temperature:g} K，不适用")
            else:
                notes.append(f"Davies A 温度匹配 {reference:g} K")
    else:
        notes.append("活度修正关闭")

    junction_settings = case.get("cell", {}).get("liquid_junction_potential", {})
    junction = prediction.get("liquid_junction_potential", {})
    if not junction_settings.get("enabled", False):
        notes.append("液接电势修正关闭")
    else:
        status = junction.get("status", "unknown")
        reason = junction.get("reason")
        if status == "estimated_from_reference_parameters":
            selection = junction.get("temperature_parameter_selection") or {}
            if selection.get("status") == "supported_reference_estimate":
                low_c, high_c = selection.get("valid_temperature_range_c", [None, None])
                notes.append(
                    "液接电势使用非条件匹配的温度参考估算"
                    f"（{selection.get('temperature_c', temperature):g} °C；"
                    f"水黏度比例近似范围 {low_c:g}–{high_c:g} °C）"
                )
            else:
                notes.append("液接电势使用非条件匹配参考参数估算")
        elif status in {"calculated"} and junction.get("condition_matched"):
            notes.append("液接电势参数条件匹配")
        else:
            notes.append(f"液接电势状态 {status}" + (f"：{reason}" if reason else ""))
    return "；".join(notes)


def summarize_result(case: dict, result: dict, detail_path: Path, output_dir: Path,
                     seed: int, target_left_m: float, target_right_m: float | None) -> dict:
    prediction = result["prediction"]
    components = prediction.get("voltage_components_mv", {})
    final_state = result["final_state"]
    active_species = _active_species(case)
    left = final_state["containers"][case["cell"]["left_container"]]
    right = final_state["containers"][case["cell"]["right_container"]]

    ideal_voltage = components.get("ideal_nernst")
    ideal_source = "scenario_prediction"
    total_steps = len(case.get("protocol", []))
    protocol_completed = final_state.get("completed_steps", 0) == total_steps
    if ideal_voltage is None and protocol_completed:
        baseline = _ideal_baseline(case, seed)
        ideal_voltage = baseline["prediction"].get("voltage_components_mv", {}).get("ideal_nernst")
        if ideal_voltage is not None:
            ideal_source = "correction_disabled_config_copy"
    if ideal_voltage is None:
        ideal_source = "unavailable"

    actual_left = prediction.get("c_left_m")
    if actual_left is None:
        actual_left = left.get("concentrations_m", {}).get(active_species)
    actual_right = prediction.get("c_right_m")
    if actual_right is None:
        actual_right = right.get("concentrations_m", {}).get(active_species)

    if result["status"] == "completed":
        scenario_status = "completed"
    elif protocol_completed:
        scenario_status = "protocol_completed_prediction_rejected"
    else:
        scenario_status = "protocol_failed_before_completion"

    activity = prediction.get("activity_correction", {})
    activity_settings = case.get("cell", {}).get("activity_correction", {})
    activity_parameter = activity_settings.get("batch_temperature_parameter_selection", {})
    junction = prediction.get("liquid_junction_potential", {})
    junction_comparison = junction.get("model_comparison") or {}
    junction_settings = case.get("cell", {}).get("liquid_junction_potential", {})
    junction_model = junction.get("model") or junction_settings.get("model", "henderson_approximation")
    junction_model_option = LIQUID_JUNCTION_MODEL_OPTIONS.get(junction_model) or {}
    henderson_comparison = junction_comparison.get("henderson_approximation") or {}
    nernst_planck_comparison = junction_comparison.get(
        "nernst_planck_zero_current_linear_profile"
    ) or {}
    return {
        "scenario": case["experiment_id"],
        "temperature_k": case["temperature_k"],
        "target_c_left_m": target_left_m,
        "actual_c_left_m": actual_left,
        "target_c_right_m": target_right_m,
        "actual_c_right_m": actual_right,
        "left_volume_ml": left.get("volume_l", 0.0) * 1000.0,
        "right_volume_ml": right.get("volume_l", 0.0) * 1000.0,
        "ideal_nernst_mv": ideal_voltage,
        "ideal_nernst_source": ideal_source,
        "activity_correction_mv": components.get("activity_correction"),
        "activity_status": activity.get("status"),
        "activity_parameter_a": activity.get("A", activity_parameter.get("A")),
        "activity_parameter_status": activity_parameter.get("status"),
        "activity_parameter_method": activity_parameter.get("method"),
        "activity_parameter_source": activity_parameter.get("source_citation"),
        "liquid_junction_enabled": bool(junction.get("enabled", False)),
        "liquid_junction_model": junction_model,
        "liquid_junction_model_name_zh": junction_model_option.get("name_zh") or junction.get("model_name_zh"),
        "liquid_junction_model_scope_zh": junction_model_option.get("scope_zh"),
        "liquid_junction_henderson_scope_zh": (
            LIQUID_JUNCTION_MODEL_OPTIONS.get("henderson_approximation") or {}
        ).get("scope_zh"),
        "liquid_junction_nernst_planck_scope_zh": (
            LIQUID_JUNCTION_MODEL_OPTIONS.get("nernst_planck_zero_current_linear_profile") or {}
        ).get("scope_zh"),
        "liquid_junction_correction_mv": junction.get("correction_mv"),
        "liquid_junction_status": junction.get("status"),
        "liquid_junction_condition_matched": junction.get("condition_matched"),
        "liquid_junction_henderson_mv": henderson_comparison.get("correction_mv"),
        "liquid_junction_nernst_planck_mv": nernst_planck_comparison.get("correction_mv"),
        "liquid_junction_model_difference_mv": junction_comparison.get(
            "difference_nernst_planck_minus_henderson_mv"
        ),
        "liquid_junction_model_comparison_note": junction_comparison.get("comparison_note"),
        "liquid_junction_temperature_parameter_method": (
            junction.get("temperature_parameter_selection") or {}
        ).get("method"),
        "liquid_junction_temperature_parameter_range_c": (
            ";".join(str(value) for value in (
                (junction.get("temperature_parameter_selection") or {}).get("valid_temperature_range_c") or []
            ))
        ),
        "liquid_junction_temperature_parameter_source": ";".join(
            (junction.get("temperature_parameter_selection") or {}).get("source_urls") or []
        ),
        "final_theoretical_voltage_mv": prediction.get("voltage_mv"),
        "prediction_valid": prediction.get("valid", False),
        "run_status": result["status"],
        "scenario_status": scenario_status,
        "failure_reason": prediction.get("reason") or (result.get("failure") or {}).get("reason"),
        "temperature_parameter_status": _temperature_parameter_note(case, prediction),
        "completed_steps": final_state.get("completed_steps", 0),
        "total_steps": total_steps,
        "conservation_passed": result["conservation"]["passed"],
        "run_id": result["run_id"],
        "detail_file": detail_path.relative_to(output_dir).as_posix(),
    }


def _load_batch_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        settings = json.load(stream)
    if not isinstance(settings, dict):
        raise ValueError("温度浓度批量配置 JSON 顶层必须是对象")
    return settings


def run_batch(base_config: dict, settings: dict, output_dir: Path, seed: int = 0) -> tuple[Path, list[dict]]:
    temperatures = _number_list(settings.get("temperatures_k"), "扫描温度")
    left_concentrations = _number_list(settings.get("left_concentrations_m"), "左侧扫描浓度")
    right_values = settings.get("right_concentrations_m")
    right_concentrations = (
        _number_list(right_values, "右侧扫描浓度") if right_values is not None else [None]
    )
    left_total_volume_ml = positive_finite(settings["left_total_volume_ml"], "左侧目标总体积")
    right_total_volume_ml = settings.get("right_total_volume_ml")
    if right_values is not None:
        if right_total_volume_ml is None:
            raise ValueError("配置 right_concentrations_m 时必须同时配置 right_total_volume_ml")
        right_total_volume_ml = positive_finite(right_total_volume_ml, "右侧目标总体积")
    stock_step_id = str(settings["stock_aspirate_step_id"])
    water_step_id = str(settings["water_aspirate_step_id"])

    scenarios: list[tuple[dict, float, float, float | None]] = []
    for temperature_k, left_m, right_m in product(temperatures, left_concentrations, right_concentrations):
        case = make_scenario_config(
            base_config,
            temperature_k=temperature_k,
            left_concentration_m=left_m,
            left_total_volume_ml=left_total_volume_ml,
            stock_step_id=stock_step_id,
            water_step_id=water_step_id,
            right_concentration_m=right_m,
            right_total_volume_ml=right_total_volume_ml,
        )
        nominal_right = right_m if right_m is not None else _nominal_target_concentration(
            case, case["cell"]["right_container"], _active_species(case)
        )
        scenarios.append((case, temperature_k, left_m, nominal_right))

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for index, (case, _temperature_k, target_left_m, target_right_m) in enumerate(scenarios):
        scenario_seed = seed + index
        result = run_experiment(case, seed=scenario_seed)
        result["temperature_parameter_selection"] = {
            "davies_A": case.get("cell", {}).get("activity_correction", {}).get(
                "batch_temperature_parameter_selection"
            ),
            "liquid_junction": result.get("prediction", {}).get("liquid_junction_potential", {}),
        }
        detail_path = save_result(result, output_dir)
        rows.append(summarize_result(
            case, result, detail_path, output_dir, scenario_seed, target_left_m, target_right_m
        ))

    created_at = datetime.now(timezone.utc).isoformat()
    group_id = uuid4().hex
    summary_name = settings.get(
        "batch_name_short_zh",
        f"{base_config.get('experiment_name_short_zh', '浓差电池')}_温度浓度扫描",
    )
    summary_path = result_date_directory(output_dir, created_at) / build_result_filename(
        "批", summary_name, created_at, group_id, "csv"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return summary_path, rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="本地批量扫描温度与左右浓度；默认固定右侧配置值，不控制外部设备。"
    )
    parser.add_argument("--base-config", type=Path,
                        default=PROJECT_ROOT / "config" / "copper_concentration_cell.json")
    parser.add_argument("--batch-config", type=Path,
                        default=PROJECT_ROOT / "config" / "copper_temperature_concentration_sweep.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    try:
        base_config = load_config(args.base_config)
        settings = _load_batch_config(args.batch_config)
        summary_path, rows = run_batch(base_config, settings, args.output_dir, seed=args.seed)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"温度浓度批量仿真未完成：{exc}\n")

    completed = sum(row["scenario_status"] == "completed" for row in rows)
    print(f"批量仿真情景：{len(rows)} 组（成功 {completed} 组，带拒绝/失败状态 {len(rows) - completed} 组）")
    print(f"CSV 汇总：{summary_path}")
    for row in rows:
        voltage = row["final_theoretical_voltage_mv"]
        voltage_text = "不可用" if voltage is None else f"{voltage:.4f} mV"
        print(
            f"T={row['temperature_k']:g} K, 左={row['target_c_left_m']:g} M, "
            f"右={row['target_c_right_m'] if row['target_c_right_m'] is not None else '配置值'} M: "
            f"{voltage_text}; {row['scenario_status']}; 守恒={'通过' if row['conservation_passed'] else '未通过'}"
        )


if __name__ == "__main__":
    main()

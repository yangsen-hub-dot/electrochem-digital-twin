"""批量扫描左侧浓度，复用单次仿真与结果保存逻辑。"""

from __future__ import annotations

import argparse
import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .result_recorder import build_result_filename, result_date_directory
from .simulator import PROJECT_ROOT, load_config, run_experiment, save_result


def positive_finite(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} 必须是有限正数")
    return number


def find_aspirate_step(config: dict, step_id: str) -> dict:
    matches = [step for step in config["protocol"] if step.get("step_id") == step_id]
    if len(matches) != 1 or matches[0].get("action") != "aspirate":
        raise ValueError(f"吸液步骤 {step_id!r} 必须在基础 protocol 中恰好出现一次")
    return matches[0]


def make_case(base_config: dict, concentration_m: float, total_volume_ml: float,
              stock_step_id: str, water_step_id: str) -> dict:
    case = deepcopy(base_config)
    stock_step = find_aspirate_step(case, stock_step_id)
    water_step = find_aspirate_step(case, water_step_id)
    species = case["cell"].get("active_species") or case["cell"].get("redox_reaction", {}).get("display_species")
    if not species:
        species = next(
            (ion for ion, exponent in case["cell"].get("redox_reaction", {}).get("reaction_quotient_terms", {}).items()
             if float(exponent) < 0),
            None,
        )
    if not species:
        raise ValueError("批量浓度扫描需要配置 active_species 或可识别的反应物物种")
    stock_source = stock_step["source"]
    stock_concentration = positive_finite(
        case["containers"][stock_source]["concentrations_m"][species], "母液浓度"
    )
    if water_step["source"] == stock_source:
        raise ValueError("母液步骤和水步骤必须使用不同来源容器")

    total_volume_ul = positive_finite(total_volume_ml, "左侧目标总体积") * 1000.0
    stock_volume_ul = concentration_m / stock_concentration * total_volume_ul
    water_volume_ul = total_volume_ul - stock_volume_ul
    if water_volume_ul < -1e-9:
        raise ValueError(
            f"左侧目标浓度 {concentration_m:g} M 高于母液浓度 {stock_concentration:g} M"
        )

    if water_volume_ul > 1e-9:
        stock_step["volume_ul"] = stock_volume_ul
        water_step["volume_ul"] = water_volume_ul
    else:
        # 目标浓度等于母液浓度时，不执行低侧加水动作。
        stock_step["volume_ul"] = total_volume_ul
        water_index = case["protocol"].index(water_step)
        load_index = next(
            (i for i in range(water_index - 1, -1, -1)
             if case["protocol"][i].get("action") == "load_tip"), None
        )
        left_name = case["cell"]["left_container"]
        dispense_index = next(
            (i for i in range(water_index + 1, len(case["protocol"]))
             if case["protocol"][i].get("action") == "dispense"
             and case["protocol"][i].get("target") == left_name), None
        )
        discard_index = next(
            (i for i in range((dispense_index if dispense_index is not None else water_index) + 1,
                              len(case["protocol"]))
             if case["protocol"][i].get("action") == "discard_tip"), None
        )
        if load_index is None or dispense_index is None or discard_index is None:
            raise ValueError("无法定位水转移对应的装枪头、排液和弃枪头步骤")
        removed = {load_index, water_index, dispense_index, discard_index}
        case["protocol"] = [
            step for i, step in enumerate(case["protocol"]) if i not in removed
        ]

    case["experiment_id"] = f"{base_config['experiment_id']}__left_{concentration_m:g}M"
    base_name = base_config.get("experiment_name_zh", "浓差电池仿真")
    case["experiment_name_zh"] = f"{base_name}_左侧浓度{concentration_m:g}M"
    case["result_category_zh"] = "批量仿真情景"
    base_short_name = base_config.get("experiment_name_short_zh", "浓差电池")
    case["experiment_name_short_zh"] = f"{base_short_name}_左浓{concentration_m:g}M"
    case["result_category_short_zh"] = "批"
    case["description"] = (
        f"批量浓度扫描：左侧目标浓度 {concentration_m:g} M；"
        "右侧及其他条件沿用基础配置。纯仿真情景。"
    )
    return case


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量扫描左侧配置活性物种浓度并保存 CSV 汇总及完整运行记录。"
    )
    parser.add_argument("--base-config", type=Path,
                        default=PROJECT_ROOT / "config" / "copper_concentration_cell.json")
    parser.add_argument("--batch-config", type=Path,
                        default=PROJECT_ROOT / "config" / "copper_concentration_sweep.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument("--seed", type=int, default=0,
                        help="随机种子；误差模块关闭时不影响结果")
    args = parser.parse_args()

    try:
        base_config = load_config(args.base_config)
        with args.batch_config.open(encoding="utf-8") as stream:
            sweep = json.load(stream)
        if not isinstance(sweep, dict):
            raise ValueError("批量配置 JSON 顶层必须是对象")
        concentrations = [
            positive_finite(value, "扫描浓度")
            for value in sweep["low_concentrations_m"]
        ]
        if not concentrations:
            raise ValueError("low_concentrations_m 至少要包含一组浓度")
        if len(set(concentrations)) != len(concentrations):
            raise ValueError("扫描浓度不能重复")

        total_volume_ml = positive_finite(sweep["left_total_volume_ml"], "左侧目标总体积")
        stock_step_id = str(sweep["stock_aspirate_step_id"])
        water_step_id = str(sweep["water_aspirate_step_id"])
        args.output_dir.mkdir(parents=True, exist_ok=True)

        group_id = uuid4().hex
        rows: list[dict] = []
        for index, concentration_m in enumerate(concentrations):
            case = make_case(
                base_config, concentration_m, total_volume_ml,
                stock_step_id, water_step_id,
            )
            result = run_experiment(case, seed=args.seed + index)
            detail_path = save_result(result, args.output_dir)
            prediction = result["prediction"]
            state = result["final_state"]
            left = state["containers"][case["cell"]["left_container"]]
            right = state["containers"][case["cell"]["right_container"]]
            rows.append({
                "scenario": f"left_{concentration_m:g}M",
                "target_c_left_m": concentration_m,
                "actual_c_left_m": prediction.get("c_left_m"),
                "actual_c_right_m": prediction.get("c_right_m"),
                "left_volume_ml": left["volume_l"] * 1000.0,
                "right_volume_ml": right["volume_l"] * 1000.0,
                "temperature_k": state["temperature_k"],
                "voltage_mv": prediction.get("voltage_mv"),
                "local_voltage_min_mv": (
                    None if prediction.get("local_voltage_range_mv") is None
                    else prediction["local_voltage_range_mv"]["minimum_mv"]
                ),
                "local_voltage_max_mv": (
                    None if prediction.get("local_voltage_range_mv") is None
                    else prediction["local_voltage_range_mv"]["maximum_mv"]
                ),
                "status": result["status"],
                "valid": prediction.get("valid", False),
                "conservation_passed": result["conservation"]["passed"],
                "completed_steps": state["completed_steps"],
                "run_id": result["run_id"],
                "detail_file": detail_path.relative_to(args.output_dir).as_posix(),
            })

        summary_time = datetime.now(timezone.utc).isoformat()
        summary_path = result_date_directory(args.output_dir, summary_time) / build_result_filename(
            "批",
            sweep.get("batch_name_short_zh", f"{base_config.get('experiment_name_short_zh', '浓差电池')}_左浓度扫描"),
            summary_time,
            group_id,
            "csv",
        )
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        print(f"批量仿真完成：{len(rows)} 组")
        print(f"CSV 汇总：{summary_path}")
        print("左侧目标浓度 (M) | 实际浓度 (M) | 理论电压 (mV) | 状态 | 守恒")
        for row in rows:
            actual = row["actual_c_left_m"]
            voltage = row["voltage_mv"]
            print(
                f"{row['target_c_left_m']:.6g} | "
                f"{'无' if actual is None else f'{actual:.6g}'} | "
                f"{'无有效预测' if voltage is None else f'{voltage:.4f}'} | "
                f"{row['status']} | {'通过' if row['conservation_passed'] else '未通过'}"
            )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(2, f"批量仿真未完成：{exc}\n")


if __name__ == "__main__":
    main()

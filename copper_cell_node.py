#!/usr/bin/env python3
"""节点数据服务：提供仿真结果查询和网页实验条件仿真，不控制真实设备。"""

from __future__ import annotations

import argparse
import csv
import hmac
import json
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

MODULE_ROOT = Path(__file__).resolve().parent / "electrochemistry_simulation"
if MODULE_ROOT.is_dir():
    sys.path.insert(0, str(MODULE_ROOT))

try:
    from electrochem_sim.condition_input import (
        BRIDGE_PRESETS,
        LIQUID_JUNCTION_MODEL_OPTIONS,
        build_condition_config,
    )
    from electrochem_sim.experimental_design_batch import (
        estimate_experimental_design_scenario_count,
        run_experimental_design_batch,
    )
    from electrochem_sim.temperature_concentration_batch import _nominal_target_solution
    from electrochem_sim.simulator import load_config, run_experiment, save_result
except ModuleNotFoundError as exc:
    raise SystemExit(
        "缺少仿真模块。请把 electrochemistry_simulation 整个文件夹与 "
        "当前节点服务脚本放在同一目录；只上传节点脚本不够。"
    ) from exc


class NodeService:
    """启动时运行一次；查询只读，网页提交新条件时另起一轮仿真。"""

    def __init__(self, config: dict, seed: int = 0, output_dir: Path | None = None):
        self.started = time.monotonic()
        self.output_dir = Path(output_dir) if output_dir is not None else MODULE_ROOT / "results"
        self.persist_results = output_dir is not None
        self.seed = seed
        self.run_sequence = 0
        self.run_lock = threading.Lock()
        self.batch_job_lock = threading.Lock()
        self.batch_job = {"status": "idle", "message": "尚未启动批量仿真"}
        self.result = run_experiment(config, seed=seed)
        self.base_config = deepcopy(self.result["config"])
        self.baseline_result = self.result
        if config.get("error_model", {}).get("enabled", False):
            baseline_config = deepcopy(config)
            baseline_config.setdefault("error_model", {})["enabled"] = False
            self.baseline_result = run_experiment(baseline_config, seed=seed)
        self.result_path = None if output_dir is None else save_result(self.result, self.output_dir)

    def state_payload(self) -> dict:
        result = self.result
        prediction = result["prediction"]
        config = result["config"]
        state = result["final_state"]
        left = state["containers"][config["cell"]["left_container"]]
        right = state["containers"][config["cell"]["right_container"]]
        redox = config["cell"].get("redox_reaction", {})
        bridge_setup = config.get("cell_setup", {}).get("salt_bridge", {})
        junction_settings = config.get("cell", {}).get("liquid_junction_potential", {})
        bridge_electrolytes = junction_settings.get("bridge_electrolyte", {}).get("electrolytes", [])
        last_step = result["steps"][-1]["event"]["step_id"] if result["steps"] else None
        return {
            "schema_version": "2.0",
            "model": prediction["model"],
            "mode": "protocol_simulation",
            "data_origin": "simulated",
            "run_id": result["run_id"],
            "experiment_id": result["experiment_id"],
            "experiment_name_zh": config.get("experiment_name_zh"),
            "container_names": {
                identifier: definition.get("name_zh", identifier)
                for identifier, definition in config.get("containers", {}).items()
            },
            "status": result["status"],
            "valid": result["status"] == "completed" and prediction["valid"],
            "failure": deepcopy(result["failure"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "elapsed_kind": "service_uptime_not_physical_time",
            "temperature_k": state["temperature_k"],
            # 旧网页使用 low/high 字段；其含义固定为左/右，不能自动排序。
            "c_low_m": prediction["c_left_m"],
            "c_high_m": prediction["c_right_m"],
            "c_left_m": prediction["c_left_m"],
            "c_right_m": prediction["c_right_m"],
            "volume_low_ml": left["volume_l"] * 1000,
            "volume_high_ml": right["volume_l"] * 1000,
            "voltage_mv": prediction["voltage_mv"],
            "active_species": prediction.get("active_species"),
            "active_species_name": prediction.get("active_species_name"),
            "active_species_formula": prediction.get("active_species_formula"),
            "redox_couple_name_zh": prediction.get("redox_couple_name_zh") or redox.get("name_zh"),
            "reduction_reaction_zh": prediction.get("reduction_reaction_zh") or redox.get("reduction_reaction_zh"),
            "reaction_quotient_terms": deepcopy(prediction.get("reaction_quotient_terms", {})),
            "salt_bridge": {
                "type": bridge_setup.get("type"),
                "concentration_m": bridge_setup.get("concentration_m"),
                "status": bridge_setup.get("status"),
                "electrolytes": deepcopy(bridge_electrolytes),
                "liquid_junction_enabled": bool(junction_settings.get("enabled", False)),
                "liquid_junction_model": junction_settings.get("model"),
            },
            "voltage_basis": prediction["voltage_basis"],
            "voltage_components_mv": deepcopy(prediction["voltage_components_mv"]),
            "activity_correction": deepcopy(prediction["activity_correction"]),
            "liquid_junction_potential": deepcopy(prediction["liquid_junction_potential"]),
            "local_voltage_range_mv": deepcopy(prediction["local_voltage_range_mv"]),
            "local_voltage_range_reason": prediction["local_voltage_range_reason"],
            "local_concentration_ranges_m": deepcopy(prediction["local_concentration_ranges_m"]),
            "local_concentration_spread_m": deepcopy(prediction["local_concentration_spread_m"]),
            "mixing": {
                "left_uniformity": left["mixing_uniformity"],
                "right_uniformity": right["mixing_uniformity"],
                "left_concentration_spread_m": prediction["local_concentration_spread_m"]["left"],
                "right_concentration_spread_m": prediction["local_concentration_spread_m"]["right"],
            },
            "polarity": prediction["polarity"],
            "completed_steps": state["completed_steps"],
            "total_steps": len(config["protocol"]),
            "step_id": last_step,
            "errors_enabled": bool(config.get("error_model", {}).get("enabled", False)),
            "conservation": deepcopy(result["conservation"]),
            "condition_editor": self.condition_editor_payload(),
        }

    def condition_editor_payload(self) -> dict:
        """Expose current editable inputs and conservative bounds to the web UI."""
        result = self.result
        config = result["config"]
        state = result["final_state"]
        prediction = result["prediction"]
        active_species = config.get("cell", {}).get("active_species")
        left_id = config["cell"]["left_container"]
        right_id = config["cell"]["right_container"]
        left = state["containers"][left_id]
        right = state["containers"][right_id]
        saved_inputs = config.get("condition_inputs", {})
        bridge = config.get("cell_setup", {}).get("salt_bridge", {})
        junction = config.get("cell", {}).get("liquid_junction_potential", {})
        activity = config.get("cell", {}).get("activity_correction", {})
        stock_sources = [
            step.get("source") for step in config.get("protocol", [])
            if step.get("action") == "aspirate" and step.get("source") in config.get("containers", {})
        ]
        stock_concentrations = [
            float(config["containers"][source].get("concentrations_m", {}).get(active_species, 0.0))
            for source in stock_sources if active_species
        ]
        stock_concentration = max(stock_concentrations, default=None)
        error_settings = config.get("error_model", {})
        error_factors = error_settings.get("factors", {})
        error_levels = {
            name: error_factors.get(name, {}).get("level", "low")
            for name in ("pipetting", "tip_carryover", "mixing")
        }
        error_profile = self._operation_error_profile(error_settings)
        capacity_by_side = {
            "left": config.get("containers", {}).get(left_id, {}).get("capacity_ml"),
            "right": config.get("containers", {}).get(right_id, {}).get("capacity_ml"),
        }
        return {
            "current": {
                "left_concentration_m": saved_inputs.get("left_concentration_m", prediction.get("c_left_m")),
                "right_concentration_m": saved_inputs.get("right_concentration_m", prediction.get("c_right_m")),
                "left_volume_ml": saved_inputs.get("left_volume_ml", left["volume_l"] * 1000),
                "right_volume_ml": saved_inputs.get("right_volume_ml", right["volume_l"] * 1000),
                "temperature_c": saved_inputs.get("temperature_c", state["temperature_k"] - 273.15),
                "salt_bridge_type": saved_inputs.get("salt_bridge_type", bridge.get("type") or "KNO3"),
                "salt_bridge_concentration_m": saved_inputs.get(
                    "salt_bridge_concentration_m", bridge.get("concentration_m", 0.1)
                ),
                "activity_correction_enabled": saved_inputs.get(
                    "activity_correction_enabled", bool(activity.get("enabled", False))
                ),
                "liquid_junction_enabled": saved_inputs.get(
                    "liquid_junction_enabled", bool(junction.get("enabled", False))
                ),
                "liquid_junction_model": saved_inputs.get(
                    "liquid_junction_model", junction.get("model", "henderson_approximation")
                ),
                "errors_enabled": saved_inputs.get(
                    "errors_enabled", bool(config.get("error_model", {}).get("enabled", False))
                ),
                "operation_error_levels": saved_inputs.get("operation_error_levels", error_levels),
            },
            "active_species_name": prediction.get("active_species_name") or active_species,
            "redox_couple_name_zh": prediction.get("redox_couple_name_zh"),
            "stock_concentration_m": stock_concentration,
            "measurable_ranges": {
                "concentration_m": {
                    "minimum_exclusive": 0.0,
                    "maximum_inclusive": stock_concentration,
                },
                "volume_ml": {
                    "minimum_exclusive": 0.0,
                    "capacity_by_side_ml": capacity_by_side,
                    "constraint": "同时受容器容量、母液库存和纯水库存约束；准确上限随目标浓度变化，提交时校验。",
                },
                "temperature_c": {
                    "activity_reference_range": [0.0, 50.0],
                    "davies_ionic_strength_range_mol_kg": [0.0, 0.5],
                    "liquid_junction_reference_range": [0.0, 95.0],
                    "liquid_junction_policy": junction.get("parameter_use_policy"),
                    "liquid_junction_note": "0–95 °C 是离子迁移率参考库范围；只有显式允许参考估算且盐桥/溶液所需离子均有参数时，才会进入修正结果。",
                },
            },
            "operation_error_profile": error_profile,
            "salt_bridge_options": [
                {"type": bridge_type, "formula": definition["formula"]}
                for bridge_type, definition in BRIDGE_PRESETS.items()
            ],
            "liquid_junction_model_options": deepcopy(LIQUID_JUNCTION_MODEL_OPTIONS),
            "max_single_transfer_ul": 1000.0,
            "min_single_transfer_ul": 20.0,
            "notes": [
                "两侧目标溶液按当前母液与纯水自动配制；不修改磁盘上的源配置文件。",
                "移液步骤支持 20–1000 μL 任意设定值：20–200 μL 用 P200，>200–1000 μL 用 P1000；更大总量拆分为量程内等份。",
                "误差参考为厂家规格锚点间的线性插值，并非本项目设备校准；小于 20 μL 的单次移液会明确拒绝。",
                "活度修正与液接电势受参数适用温度和离子组成限制；节点会按模型状态报告。",
            ],
        }

    @staticmethod
    def _operation_error_profile(error_settings: dict) -> dict | None:
        """Summarize currently configured factor levels and their explicit scopes for the UI."""
        profile_file = error_settings.get("profile_file")
        if not profile_file:
            return None
        profile_path = MODULE_ROOT / "config" / profile_file
        if not profile_path.is_file():
            return None
        try:
            with profile_path.open(encoding="utf-8") as stream:
                catalog = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return None

        models = catalog.get("models", {})
        factors = error_settings.get("factors", {})
        result = {
            "profile_name": catalog.get("profile_name"),
            "tested_volume_bands_ul": deepcopy(
                catalog.get("applicability", {}).get("tested_volume_bands_ul", [])
            ),
            "applicability": deepcopy(catalog.get("applicability", {})),
        }
        for factor_name in ("pipetting", "tip_carryover", "mixing"):
            selection = factors.get(factor_name, {})
            model_name = selection.get("model")
            model = models.get(factor_name, {}).get(model_name, {})
            if factor_name == "pipetting":
                result[factor_name] = {
                    level: {
                        **deepcopy(model.get(parameters.get("inherits"), {})),
                        **deepcopy(parameters),
                    }
                    for level, parameters in model.items()
                    if isinstance(parameters, dict)
                }
            elif factor_name == "tip_carryover":
                result[factor_name] = {
                    level: {
                        "range": deepcopy(parameters.get("residual_volume_ul_range")),
                        "unit": "μL",
                        "status": parameters.get("status"),
                    }
                    for level, parameters in model.items()
                    if isinstance(parameters, dict)
                }
            elif factor_name == "mixing":
                result[factor_name] = {
                    level: {
                        "range": deepcopy(parameters.get("efficiency_range")),
                        "unit": "fraction",
                        "status": parameters.get("status"),
                    }
                    for level, parameters in model.items()
                    if isinstance(parameters, dict)
                }
        return result

    def simulate_with_conditions(self, payload: dict) -> dict:
        """Run and persist one fresh protocol using a validated UI condition set."""
        with self.run_lock:
            config = build_condition_config(self.base_config, payload)
            # Keep runs reproducible as a sequence, but draw a fresh error
            # realization for each newly submitted experiment.
            run_seed = self.seed + self.run_sequence + 1
            result = run_experiment(config, seed=run_seed)
            if result["status"] != "completed":
                failure = result.get("failure") or {}
                raise ValueError(f"新条件下仿真未完成：{failure.get('reason', '未知原因')}")
            baseline_result = result
            if config.get("error_model", {}).get("enabled", False):
                baseline_config = deepcopy(config)
                baseline_config.setdefault("error_model", {})["enabled"] = False
                baseline_result = run_experiment(baseline_config, seed=run_seed)
            result_path = save_result(result, self.output_dir) if self.persist_results else None
            self.result = result
            self.baseline_result = baseline_result
            self.result_path = result_path
            self.run_sequence += 1
            self.started = time.monotonic()
            return self.state_payload()

    def start_experimental_design_job(self, settings: dict) -> dict:
        """Validate and start one asynchronous selectable-factor batch job."""
        if not isinstance(settings, dict):
            raise ValueError("条件组合扫描配置顶层必须是对象")
        with self.run_lock:
            # Batch scenarios must start from the untouched source protocol. A
            # previous web-edited single run rebuilds its aspiration step IDs,
            # which are not the stable template expected by the batch builder.
            batch_base_config = deepcopy(self.base_config)
            current = self.condition_editor_payload()["current"]
        batch_settings = deepcopy(settings)
        current_defaults = {
            "temperatures_k": [float(current["temperature_c"]) + 273.15],
            "left_concentrations_m": [float(current["left_concentration_m"])],
            "right_concentrations_m": [float(current["right_concentration_m"])],
            "left_total_volume_ml": float(current["left_volume_ml"]),
            "right_total_volume_ml": float(current["right_volume_ml"]),
            "salt_bridge_types": [current["salt_bridge_type"]],
            "salt_bridge_concentrations_m": [float(current["salt_bridge_concentration_m"])],
            "activity_correction_enabled": [bool(current["activity_correction_enabled"])],
            "liquid_junction_enabled": [bool(current["liquid_junction_enabled"])],
            "liquid_junction_models": [current["liquid_junction_model"]],
            "errors_enabled": [bool(current["errors_enabled"])],
            "operation_error_levels": {
                name: [level]
                for name, level in current["operation_error_levels"].items()
            },
        }
        for key, value in current_defaults.items():
            batch_settings.setdefault(key, value)
        scenario_count = estimate_experimental_design_scenario_count(batch_base_config, batch_settings)
        with self.batch_job_lock:
            if self.batch_job.get("status") == "running":
                raise RuntimeError("已有一项批量仿真正在运行，请等待完成后再启动")
            job_id = uuid4().hex
            self.batch_job = {
                "job_id": job_id,
                "status": "running",
                "scenario_count": scenario_count,
                "completed_scenarios": 0,
                "message": f"已开始 {scenario_count} 组条件仿真",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        worker = threading.Thread(
            target=self._run_experimental_design_job,
            args=(job_id, batch_base_config, batch_settings, scenario_count),
            name=f"experimental-design-{job_id[:8]}",
            daemon=True,
        )
        worker.start()
        with self.batch_job_lock:
            return deepcopy(self.batch_job)

    def _run_experimental_design_job(self, job_id: str, base_config: dict, settings: dict, scenario_count: int) -> None:
        def update_progress(completed: int, total: int) -> None:
            with self.batch_job_lock:
                if self.batch_job.get("job_id") == job_id:
                    self.batch_job["completed_scenarios"] = completed
                    self.batch_job["scenario_count"] = total
                    self.batch_job["message"] = f"正在运行：{completed}/{total} 组"

        try:
            summary_path, rows = run_experimental_design_batch(
                base_config,
                settings,
                self.output_dir,
                seed=self.seed + self.run_sequence + 1,
                progress_callback=update_progress,
            )
            completed_count = sum(row.get("scenario_status") == "completed" for row in rows)
            job_result = {
                "status": "completed",
                "completed_scenarios": len(rows),
                "successful_scenarios": completed_count,
                "scenario_count": scenario_count,
                "summary_file": summary_path.relative_to(self.output_dir).as_posix(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": f"批量仿真完成：{len(rows)} 组（有效预测 {completed_count} 组）",
            }
        except Exception as exc:  # retain failure for the polling UI
            job_result = {
                "status": "failed",
                "completed_scenarios": self.batch_job.get("completed_scenarios", 0),
                "scenario_count": scenario_count,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": f"批量仿真失败：{exc}",
            }
        with self.batch_job_lock:
            if self.batch_job.get("job_id") == job_id:
                self.batch_job.update(job_result)

    def experimental_design_job_status(self, job_id: str | None = None) -> dict | None:
        with self.batch_job_lock:
            if job_id and self.batch_job.get("job_id") != job_id:
                return None
            return deepcopy(self.batch_job)

    def health_payload(self) -> dict:
        return {
            "ok": True,
            "service": "modular_electrochemistry_node",
            "mode": "protocol_simulation",
            "simulation_status": self.result["status"],
            "run_id": self.result["run_id"],
            "results_saved": self.result_path is not None,
        }

    def comparison_payload(self) -> dict:
        """Return compact, read-only diagnostics for the current run and its ideal baseline."""
        result = self.result
        realized = result.get("error_model_realized", {})
        prediction = result["prediction"]
        baseline_prediction = self.baseline_result["prediction"]

        def factor_summary(name: str, fields: tuple[str, ...]) -> dict:
            factor = realized.get(name, {})
            return {key: deepcopy(factor.get(key)) for key in fields}

        events = [step.get("event", {}) for step in result.get("steps", [])]
        baseline = {
            "status": self.baseline_result["status"],
            "valid": baseline_prediction["valid"],
            "temperature_k": self.baseline_result["final_state"]["temperature_k"],
            "c_left_m": baseline_prediction["c_left_m"],
            "c_right_m": baseline_prediction["c_right_m"],
            "voltage_mv": baseline_prediction["voltage_mv"],
        }
        actual = {
            "status": result["status"],
            "valid": prediction["valid"],
            "c_left_m": prediction["c_left_m"],
            "c_right_m": prediction["c_right_m"],
            "voltage_mv": prediction["voltage_mv"],
        }
        return {
            "run_id": result["run_id"],
            "data_origin": "simulated",
            "container_names": {
                identifier: definition.get("name_zh", identifier)
                for identifier, definition in result["config"].get("containers", {}).items()
            },
            "errors_enabled": bool(realized.get("enabled", False)),
            "baseline_is_current_run": not bool(realized.get("enabled", False)),
            "baseline": baseline,
            "actual": actual,
            "operation_errors": {
                "pipetting": factor_summary("pipetting", (
                    "status", "model", "level", "applicable_volume_ul_range",
                    "systematic_relative_bias_range", "repeatability_cv_range",
                    "sampled_systematic_relative_bias", "sampled_repeatability_cv",
                    "sampled_by_volume_band",
                )),
                "tip_carryover": factor_summary("tip_carryover", (
                    "status", "model", "level", "residual_volume_ul_range",
                    "sampling_distribution", "sampled_residual_volume_ul",
                    "residual_fraction_range", "sampled_residual_fraction",
                )),
                "mixing": factor_summary("mixing", (
                    "status", "model", "level", "efficiency_range",
                    "sampling_distribution", "sampled_efficiency_per_action",
                )),
            },
            "events": {
                "aspirations": [event for event in events if event.get("action") == "aspirate"],
                "dispenses": [event for event in events if event.get("action") == "dispense"],
                "mixing": [event for event in events if event.get("action") == "mix"],
            },
        }

    def sweep_payload(self, batch_type: str = "concentration") -> dict:
        """读取最近一次批量扫描 CSV；不会触发新的仿真。"""
        if batch_type == "temperature_concentration":
            return self.temperature_concentration_sweep_payload()
        if batch_type == "experimental_design":
            return self.experimental_design_sweep_payload()

        files = sorted(
            [
                *self.output_dir.rglob("concentration_sweep__*.csv"),
                *self.output_dir.rglob("批_*浓度扫描_*.csv"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return {
                "available": False,
                "batch_type": "concentration",
                "data_origin": "simulated",
                "message": "尚无批量扫描结果，请先运行 python -m electrochem_sim.batch",
                "scenarios": [],
            }

        latest = files[0]
        numeric_fields = {
            "target_c_left_m", "actual_c_left_m", "actual_c_right_m",
            "left_volume_ml", "right_volume_ml", "temperature_k", "voltage_mv",
            "local_voltage_min_mv", "local_voltage_max_mv",
        }
        boolean_fields = {"valid", "conservation_passed"}
        scenarios = []
        with latest.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                parsed = dict(row)
                for key in numeric_fields:
                    value = parsed.get(key, "")
                    parsed[key] = None if value == "" else float(value)
                if parsed.get("voltage_mv") is not None and abs(parsed["voltage_mv"]) < 1e-10:
                    parsed["voltage_mv"] = 0.0
                for key in boolean_fields:
                    parsed[key] = parsed.get(key, "").strip().lower() == "true"
                if parsed.get("completed_steps", "") != "":
                    parsed["completed_steps"] = int(parsed["completed_steps"])
                scenarios.append(parsed)

        return {
            "available": bool(scenarios),
            "batch_type": "concentration",
            "data_origin": "simulated",
            "file": latest.relative_to(self.output_dir).as_posix(),
            "updated_at": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(),
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        }

    def temperature_concentration_sweep_payload(self) -> dict:
        """读取最近一次温度×浓度批量汇总 CSV，供可视化页面浏览。"""
        files = sorted(
            [
                *self.output_dir.rglob("批_*温度浓度扫描_*.csv"),
                *self.output_dir.rglob("temperature_concentration_sweep__*.csv"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return {
                "available": False,
                "batch_type": "temperature_concentration",
                "data_origin": "simulated",
                "message": "尚无温度×浓度批量结果，请先运行温度浓度批量仿真",
                "scenarios": [],
            }

        latest = files[0]
        numeric_fields = {
            "temperature_k", "target_c_left_m", "actual_c_left_m",
            "target_c_right_m", "actual_c_right_m", "left_volume_ml",
            "right_volume_ml", "ideal_nernst_mv", "activity_correction_mv",
            "activity_parameter_a", "liquid_junction_correction_mv",
            "liquid_junction_henderson_mv", "liquid_junction_nernst_planck_mv",
            "liquid_junction_model_difference_mv", "final_theoretical_voltage_mv",
            "completed_steps", "total_steps",
        }
        boolean_fields = {
            "liquid_junction_enabled", "liquid_junction_condition_matched",
            "prediction_valid", "conservation_passed",
        }
        scenarios = []
        with latest.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                parsed = dict(row)
                for key in numeric_fields:
                    value = parsed.get(key, "")
                    parsed[key] = None if value == "" else float(value)
                for key in boolean_fields:
                    parsed[key] = parsed.get(key, "").strip().lower() == "true"
                scenarios.append(parsed)

        return {
            "available": bool(scenarios),
            "batch_type": "temperature_concentration",
            "data_origin": "simulated",
            "file": latest.relative_to(self.output_dir).as_posix(),
            "updated_at": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(),
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        }

    def experimental_design_sweep_payload(self) -> dict:
        """读取最近一次用户自选实验条件组合矩阵 CSV。"""
        files = sorted(
            [
                *self.output_dir.rglob("批_*条件扫描_*.csv"),
                *self.output_dir.rglob("experimental_design_sweep__*.csv"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return {
                "available": False,
                "batch_type": "experimental_design",
                "data_origin": "simulated",
                "message": "尚无自定义条件组合结果，请先运行条件组合批量仿真",
                "scenarios": [],
            }

        latest = files[0]
        numeric_fields = {
            "matrix_index", "replicate", "replicate_count", "temperature_k",
            "target_c_left_m", "actual_c_left_m", "target_c_right_m",
            "actual_c_right_m", "left_volume_ml", "right_volume_ml",
            "salt_bridge_concentration_m", "activity_correction_mv",
            "activity_parameter_a", "liquid_junction_correction_mv",
            "liquid_junction_henderson_mv", "liquid_junction_nernst_planck_mv",
            "liquid_junction_model_difference_mv", "final_theoretical_voltage_mv",
            "completed_steps", "total_steps",
        }
        boolean_fields = {
            "activity_correction_enabled", "liquid_junction_enabled",
            "liquid_junction_condition_matched", "errors_enabled",
            "prediction_valid", "conservation_passed",
        }
        scenarios = []
        with latest.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                parsed = dict(row)
                for key in numeric_fields:
                    value = parsed.get(key, "")
                    parsed[key] = None if value == "" else float(value)
                for key in boolean_fields:
                    parsed[key] = parsed.get(key, "").strip().lower() == "true"
                scenarios.append(parsed)

        return {
            "available": bool(scenarios),
            "batch_type": "experimental_design",
            "data_origin": "simulated",
            "file": latest.relative_to(self.output_dir).as_posix(),
            "updated_at": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(),
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        }

    def history_payload(self, limit: int = 200) -> dict:
        """Return compact summaries of saved runs for browsing and multi-run comparison."""
        root = self.output_dir.resolve()
        candidates = []
        try:
            candidates = [path for path in root.rglob("*.json") if path.is_file()]
        except OSError:
            candidates = []
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

        records = []
        for path in candidates[:limit]:
            try:
                resolved = path.resolve()
                if root not in resolved.parents or path.stat().st_size > 2_000_000:
                    continue
                with path.open(encoding="utf-8") as stream:
                    result = json.load(stream)
                if not isinstance(result, dict) or not result.get("run_id"):
                    continue
                records.append(self._history_record_summary(result, path.relative_to(root).as_posix()))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                continue
        records.sort(key=lambda item: item.get("created_at_utc") or "", reverse=True)
        return {
            "data_origin": "simulated",
            "count": len(records),
            "limit": limit,
            "liquid_junction_model_options": deepcopy(LIQUID_JUNCTION_MODEL_OPTIONS),
            "records": records,
        }

    @staticmethod
    def _history_record_summary(result: dict, relative_path: str) -> dict:
        config = result.get("config") or {}
        prediction = result.get("prediction") or {}
        state = result.get("final_state") or {}
        cell = config.get("cell") or {}
        containers = state.get("containers") or {}
        left_id, right_id = cell.get("left_container"), cell.get("right_container")
        left, right = containers.get(left_id, {}), containers.get(right_id, {})
        inputs = config.get("condition_inputs") or {}
        active_species = (
            cell.get("active_species")
            or (cell.get("redox_reaction") or {}).get("display_species")
            or cell.get("copper_species")
        )

        def nominal_target(container_id: str | None):
            if not container_id or not active_species:
                return None
            try:
                return _nominal_target_solution(config, container_id, active_species)
            except (KeyError, TypeError, ValueError):
                return None

        left_nominal = nominal_target(left_id)
        right_nominal = nominal_target(right_id)
        target_left_concentration = inputs.get("left_concentration_m")
        target_right_concentration = inputs.get("right_concentration_m")
        target_left_volume = inputs.get("left_volume_ml")
        target_right_volume = inputs.get("right_volume_ml")
        if left_nominal is not None:
            target_left_concentration = left_nominal[0] if target_left_concentration is None else target_left_concentration
            target_left_volume = left_nominal[1] if target_left_volume is None else target_left_volume
        if right_nominal is not None:
            target_right_concentration = right_nominal[0] if target_right_concentration is None else target_right_concentration
            target_right_volume = right_nominal[1] if target_right_volume is None else target_right_volume
        bridge = (config.get("cell_setup") or {}).get("salt_bridge") or {}
        realized = result.get("error_model_realized") or {}
        activity = prediction.get("activity_correction") or {}
        junction = prediction.get("liquid_junction_potential") or {}
        junction_comparison = junction.get("model_comparison") or {}
        junction_settings = cell.get("liquid_junction_potential") or {}
        junction_model = junction.get("model") or junction_settings.get("model", "henderson_approximation")
        junction_model_option = LIQUID_JUNCTION_MODEL_OPTIONS.get(junction_model) or {}
        components = prediction.get("voltage_components_mv") or {}

        def factor_level(name: str):
            factor = realized.get(name) or {}
            return factor.get("level")

        def volume_ml(vessel: dict):
            value = vessel.get("volume_l")
            return None if value is None else value * 1000

        temperature_c = inputs.get("temperature_c")
        if temperature_c is None and state.get("temperature_k") is not None:
            temperature_c = state["temperature_k"] - 273.15

        return {
            "run_id": result.get("run_id"),
            "file": relative_path,
            "created_at_utc": result.get("created_at_utc"),
            "experiment_name": config.get("experiment_name_short_zh")
                or config.get("experiment_name_zh") or config.get("experiment_id") or "未命名实验",
            "experiment_id": config.get("experiment_id"),
            "status": result.get("status"),
            "valid": bool(prediction.get("valid", False)),
            "redox_couple": prediction.get("redox_couple_name_zh")
                or (cell.get("redox_reaction") or {}).get("name_zh")
                or prediction.get("active_species_name") or cell.get("active_species"),
            "active_species_name": prediction.get("active_species_name")
                or (config.get("species", {}).get(cell.get("active_species"), {}) or {}).get("name"),
            "c_left_m": prediction.get("c_left_m"),
            "c_right_m": prediction.get("c_right_m"),
            "target_c_left_m": target_left_concentration,
            "target_c_right_m": target_right_concentration,
            "left_volume_ml": volume_ml(left),
            "right_volume_ml": volume_ml(right),
            "target_left_volume_ml": target_left_volume,
            "target_right_volume_ml": target_right_volume,
            "temperature_c": temperature_c,
            "salt_bridge_type": inputs.get("salt_bridge_type", bridge.get("type")),
            "salt_bridge_concentration_m": inputs.get("salt_bridge_concentration_m", bridge.get("concentration_m")),
            "voltage_mv": prediction.get("voltage_mv"),
            "ideal_nernst_mv": components.get("ideal_nernst"),
            "activity_correction_mv": components.get("activity_correction"),
            "activity_status": activity.get("status"),
            "liquid_junction_mv": components.get("liquid_junction_potential"),
            "liquid_junction_status": junction.get("status"),
            "activity_enabled": bool(activity.get("enabled", False)),
            "liquid_junction_enabled": bool(junction.get("enabled", False)),
            "liquid_junction_model": junction_model,
            "liquid_junction_model_name_zh": junction_model_option.get("name_zh") or junction.get("model_name_zh"),
            "liquid_junction_henderson_mv": (junction_comparison.get("henderson_approximation") or {}).get("correction_mv"),
            "liquid_junction_nernst_planck_mv": (junction_comparison.get("nernst_planck_zero_current_linear_profile") or {}).get("correction_mv"),
            "liquid_junction_model_difference_mv": junction_comparison.get("difference_nernst_planck_minus_henderson_mv"),
            "errors_enabled": bool(realized.get("enabled", False)),
            "error_levels": {
                "pipetting": factor_level("pipetting"),
                "tip_carryover": factor_level("tip_carryover"),
                "mixing": factor_level("mixing"),
            },
            "conservation_passed": bool((result.get("conservation") or {}).get("passed", False)),
        }


def create_handler(service: NodeService, allowed_origins=("null",), token="") -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if self.headers.get("Origin") in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            if self.headers.get("Origin") in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path != "/health" and token and not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.send_json(401, {"message": "请填写实验访问码"})
                return
            if path == "/health":
                self.send_json(200, {**service.health_payload(), "auth_required":bool(token)})
            elif path == "/state":
                payload = service.state_payload()
                # 防止旧网页把缺液时的 null 误当成正常的 0 mV。
                self.send_json(200 if payload["valid"] else 409, payload)
            elif path == "/steps":
                self.send_json(200, {
                    "run_id": service.result["run_id"],
                    "data_origin": "simulated",
                    "status": service.result["status"],
                    "steps": service.result["steps"],
                })
            elif path == "/run":
                self.send_json(200, service.result)
            elif path == "/comparison":
                self.send_json(200, service.comparison_payload())
            elif path == "/sweep":
                batch_type = parse_qs(urlsplit(self.path).query).get("type", ["concentration"])[0]
                if batch_type not in {"concentration", "temperature_concentration", "experimental_design"}:
                    self.send_json(400, {"error": "invalid_batch_type", "available": ["concentration", "temperature_concentration", "experimental_design"]})
                    return
                self.send_json(200, service.sweep_payload(batch_type))
            elif path == "/batch/status":
                job_id = parse_qs(urlsplit(self.path).query).get("job_id", [None])[0]
                job = service.experimental_design_job_status(job_id)
                if job is None:
                    self.send_json(404, {"error": "job_not_found", "message": "找不到这项批量任务"})
                    return
                self.send_json(200, job)
            elif path == "/history":
                self.send_json(200, service.history_payload())
            else:
                self.send_json(404, {"error": "not_found", "available": ["/health", "/state", "/steps", "/run", "/comparison", "/sweep", "/history", "/batch/status", "POST /simulate", "POST /batch/start"]})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in {"/simulate", "/batch/start"}:
                self.send_json(404, {"error": "not_found", "available": ["POST /simulate", "POST /batch/start"]})
                return
            # The page is opened as a local file (Origin: null). Reject browser
            # requests initiated by unrelated websites; this endpoint mutates
            # the node's current in-memory run and writes a result record.
            if self.headers.get("Origin") not in allowed_origins:
                self.send_json(403, {"error": "origin_not_allowed", "message": "当前网站地址未在节点允许列表中"})
                return
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.send_json(401, {"message": "请填写实验访问码"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                max_length = 65_536 if path == "/batch/start" else 16_384
                if content_length <= 0 or content_length > max_length:
                    raise ValueError(f"请求内容为空或超过 {max_length // 1024} KB")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求格式必须是 application/json")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                if path == "/batch/start":
                    job = service.start_experimental_design_job(payload)
                    self.send_json(202, {"ok": True, "job": job})
                    return
                state = service.simulate_with_conditions(payload)
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_json(422, {"error": "invalid_conditions", "message": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(409, {"error": "batch_already_running", "message": str(exc)})
                return
            except OSError as exc:
                self.send_json(500, {"error": "result_save_failed", "message": str(exc)})
                return
            self.send_json(200, {"ok": True, "state": state})

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    return Handler


def create_server(host: str, port: int, service: NodeService, allowed_origins=("null",), token="") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), create_handler(service, allowed_origins, token))


def main() -> None:
    parser = argparse.ArgumentParser(description="启动模块化浓差电池仿真节点，不控制真实设备")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=50005)
    parser.add_argument("--config", type=Path, default=MODULE_ROOT / "config" / "copper_concentration_cell.json")
    parser.add_argument("--output-dir", type=Path, default=MODULE_ROOT / "results")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else ""
    if args.token_file and len(token) < 24:
        parser.error("访问码至少需要 24 个字符")
    try:
        # 先占用端口，避免端口冲突时无意义地执行一次仿真。
        server = ThreadingHTTPServer((args.host, args.port), BaseHTTPRequestHandler)
    except OSError as exc:
        parser.exit(2, f"无法监听端口：{exc}；请确认旧服务是否仍在运行。\n")
    try:
        service = NodeService(load_config(args.config), args.seed, args.output_dir)
        # 尚未启动接收请求；准备好结果后再设置处理器。
        server.RequestHandlerClass = create_handler(service, tuple(args.allowed_origin or ["null"]), token)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        server.server_close()
        parser.exit(2, f"无法初始化仿真：{exc}\n")
    print(f"模块化节点服务已启动，端口 {server.server_address[1]}", flush=True)
    print(f"配置：{args.config}", flush=True)
    print(f"已执行步骤：{service.result['final_state']['completed_steps']}", flush=True)
    prediction = service.result["prediction"]
    if service.result["status"] == "completed":
        print(f"理论开路电压：{prediction['voltage_mv']:.4f} mV", flush=True)
    else:
        print(f"仿真失败：{service.result['failure']['reason']}；可查询 /run 排查。", flush=True)
    print(f"运行记录：{service.result_path}", flush=True)
    print("接口：GET /health /state /steps /run /comparison /sweep /history；POST /simulate 按网页条件重新仿真。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

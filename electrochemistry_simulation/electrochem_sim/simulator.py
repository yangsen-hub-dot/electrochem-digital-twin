"""总控：依次执行步骤、检查守恒、计算电压并保存运行记录。"""

from __future__ import annotations

import argparse
import json
import random
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .electrochemistry import predict_voltage
from .error_model import ErrorModel
from .cell_assembly import apply_assembly
from .mixing import apply_mixing, apply_paired_mixing
from .liquid_transfer import apply_action
from .protocol import build_protocol
from .result_recorder import save_result
from .state import build_initial_state
from .conservation import check_conservation
from .chemical_models import resolve_chemical_model

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path) -> dict:
    """Load conditions and attach their separately stored protocol document."""
    config_path = Path(path)
    with Path(path).open(encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    protocol_file = config.get("protocol_file")
    if protocol_file and "protocol" not in config:
        protocol_path = config_path.parent / protocol_file
        with protocol_path.open(encoding="utf-8") as stream:
            protocol_document = json.load(stream)
        config["protocol"] = protocol_document.get("steps", protocol_document)
    error_model = config.get("error_model", {})
    profile_file = error_model.get("profile_file")
    factors = error_model.get("factors", {})
    if profile_file and factors:
        profile_path = config_path.parent / profile_file
        with profile_path.open(encoding="utf-8") as stream:
            catalog = json.load(stream)
        models = catalog.get("models", {})
        for factor_name, selection in factors.items():
            if selection.get("status") in {"not_parameterized", "insufficient_evidence", "insufficient_generalizable_evidence"}:
                continue
            model_name = selection.get("model")
            level = selection.get("level")
            try:
                parameters = models[factor_name][model_name][level]
                if isinstance(parameters, dict) and parameters.get("inherits"):
                    parent = models[factor_name][model_name][parameters["inherits"]]
                    parameters = {**deepcopy(parent), **deepcopy(parameters)}
                    parameters.pop("inherits", None)
            except (KeyError, TypeError) as exc:
                raise ValueError(f"误差档位无效: {factor_name}/{model_name}/{level}") from exc
            selection["parameters"] = deepcopy(parameters)
            factor_source = catalog.get("factor_evidence", {}).get(factor_name, {}).get("status")
            selection["parameter_source"] = parameters.get(
                "parameter_source", factor_source or catalog.get("source", "unspecified")
            )
        error_model["profile_set_id"] = catalog.get("profile_id", catalog.get("profile_set_id"))
        error_model["profile_note"] = catalog.get("applicability", {}).get("evidence_scope_note", catalog.get("note"))
        error_model["applicability"] = deepcopy(catalog.get("applicability", {}))
        error_model["factor_evidence"] = deepcopy(catalog.get("factor_evidence", {}))
    return config


def run_experiment(config: dict, seed: int = 0) -> dict:
    config = deepcopy(config)
    chemical_model = resolve_chemical_model(config, strict=False)
    config.setdefault("cell", {})["chemical_model_id"] = chemical_model.model_id
    state = build_initial_state(config)
    state.chemical_model_id = chemical_model.model_id
    state.chemical_model_state = chemical_model.initialize_state(config, state)
    state.ledger.set_chemical_model(chemical_model.model_id, chemical_model.model_family,
                                    chemical_model.state_schema_version)
    state.ledger.workflow["status"] = "running"
    protocol = build_protocol(config)
    for name in ("left_container", "right_container"):
        if config["cell"][name] not in state.containers:
            raise ValueError("电池映射的容器不存在")
    if config["cell"]["left_container"] == config["cell"]["right_container"]:
        raise ValueError("电池两侧必须为不同容器")
    rng = random.Random(seed)
    errors = ErrorModel(config.get("error_model"), rng=rng)
    initial_inventory = state.inventory()
    initial_state = state.snapshot()
    steps: list[dict] = []
    failure = None
    for command in protocol:
        before = state.snapshot()
        try:
            realized = errors.realize(command, rng)
            if command.kind == "mix":
                candidate, event = apply_mixing(state, command.target, command.step_id, realized.mixing_efficiency)
                event["error_application_status"] = realized.error_status
            elif command.kind == "mix_pair":
                efficiencies = errors.realize_paired_mixing(rng, command.duration_s)
                candidate, event = apply_paired_mixing(state, command.targets, command.step_id, efficiencies)
                event["error_application_status"] = "applied" if errors.enabled else "disabled"
                event["paired_cell_correlation"] = errors.paired_mixing_correlation
                event["duration_s"] = command.duration_s
            elif command.kind in {"place_electrodes", "connect_salt_bridge"}:
                candidate, event = apply_assembly(state, command.kind, command.step_id, command.component)
            else:
                candidate, event = apply_action(state, realized)
            elapsed_time_s = float(event.get("duration_s", command.duration_s or 0.0))
            prior_model_state = deepcopy(state.chemical_model_state)
            candidate.chemical_model_id = chemical_model.model_id
            candidate.chemical_model_state = chemical_model.advance(
                candidate.chemical_model_state, candidate,
                step={"step_id": command.step_id, "action": command.kind},
                event=event, elapsed_time_s=elapsed_time_s, config=config,
            )
            model_check = chemical_model.validate_transition(
                state, candidate, prior_model_state, candidate.chemical_model_state,
                config=config,
            )
            if not isinstance(model_check, dict) or model_check.get("passed") is not True:
                raise ValueError("化学模型状态转移校验未通过")
            event["chemical_model_transition_check"] = model_check
            check = check_conservation(initial_inventory, candidate.inventory())
            if not check["passed"]:
                raise ValueError("守恒检查未通过，本步骤未提交")
        except ValueError as exc:
            failure = {"step_id": command.step_id, "action": command.kind, "reason": str(exc)}
            state.ledger.record_step(
                state_revision=state.ledger.revision, step_id=command.step_id,
                action=command.kind, outcome="failed", source="numerical_simulator",
                simulation_time_s=None, command={"step_id": command.step_id, "action": command.kind},
                receipt={"reason": str(exc)},
            )
            state.ledger.workflow["status"] = "failed"
            steps.append({"status": "failed", "event": failure, "before": before, "after": state.snapshot()})
            break
        state = candidate
        next_revision = state.ledger.revision + 1
        state.ledger.record_step(
            state_revision=next_revision, step_id=command.step_id, action=command.kind,
            outcome="succeeded", source="numerical_simulator", simulation_time_s=None,
            command={"step_id": command.step_id, "action": command.kind},
                receipt={"actual_source": "chemical_model", "event": event,
                         "model_elapsed_time_s": elapsed_time_s,
                         "chemical_model_transition_check": model_check},
            terminal=len(steps) + 1 == len(protocol),
        )
        steps.append({
            "status": "completed", "event": event,
            "before": before, "after": state.snapshot(),
            "prediction": predict_voltage(state, config["cell"], config.get("species", {})),
            "conservation": check,
        })
    prediction = predict_voltage(state, config["cell"], config.get("species", {}))
    if failure is None and not prediction["valid"]:
        failure = {"step_id": "final_prediction", "reason": prediction["reason"]}
        state.ledger.record_step(
            state_revision=state.ledger.revision, step_id="final_prediction",
            action="observe", outcome="failed", source="chemical_model",
            simulation_time_s=None, receipt={"reason": prediction["reason"]},
        )
        state.ledger.workflow["status"] = "failed"
    elif failure is None:
        state.ledger.workflow["status"] = "completed"
    return {
        "schema_version": "1.0", "software_version": __version__,
        "run_id": uuid4().hex, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_id": config["experiment_id"], "data_origin": "simulated",
        "status": "failed" if failure else "completed", "failure": failure,
        "seed": seed, "config": config, "initial_state": initial_state,
        "error_model_realized": errors.snapshot(),
        "steps": steps, "final_state": state.snapshot(), "prediction": prediction,
        "chemical_model": {"model_id": chemical_model.model_id,
                           "model_family": chemical_model.model_family,
                           "state_schema_version": chemical_model.state_schema_version},
        "conservation": check_conservation(initial_inventory, state.inventory()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行模块化浓差电池流程仿真，不控制真实设备")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "copper_concentration_cell.json")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "results")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    try:
        result = run_experiment(load_config(args.config), args.seed)
        path = save_result(result, args.output_dir)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"无法运行：{exc}\n")
    if result["status"] == "failed":
        print(f"仿真失败：{result['failure']['reason']}")
        print(f"运行记录：{path}")
        raise SystemExit(1)
    print(f"已完成 {result['final_state']['completed_steps']} 个模拟步骤")
    print(f"理论开路电压：{result['prediction']['voltage_mv']:.4f} mV")
    print(f"物料守恒：{'通过' if result['conservation']['passed'] else '未通过'}")
    print(f"运行记录：{path}")


if __name__ == "__main__":
    main()

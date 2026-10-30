"""Configurable, transactional numerical workflow; independent of reaction model."""
from __future__ import annotations

import random
from copy import deepcopy
from uuid import uuid4

from .chemical_models import resolve_chemical_model
from .conservation import check_conservation
from .error_model import ErrorModel
from .state import build_initial_state
from .workflow_actions import get_action
from .workflow_checks import check_transition


def run_workflow(config, seed=0, *, on_event=None):
    config = deepcopy(config)
    steps = config.get("workflow")
    if not isinstance(steps, list) or not steps:
        raise ValueError("workflow must contain steps")
    ids = set()
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), str) or not step["step_id"] or step["step_id"] in ids:
            raise ValueError("workflow step ids must be nonempty and unique")
        get_action(step.get("action"))
        ids.add(step["step_id"])
    model = resolve_chemical_model(config)
    state = build_initial_state(config)
    state.chemical_model_id = model.model_id
    state.chemical_model_state = model.initialize_state(config, state)
    state.ledger.set_chemical_model(model.model_id, model.model_family, model.state_schema_version)
    state.ledger.workflow["status"] = "running"
    initial = state.snapshot()
    inventory = state.inventory()
    rng = random.Random(seed)
    errors = ErrorModel(config.get("error_model"), rng=rng)
    context = {"rng": rng, "errors": errors, "model": model,
               "species": config.get("species", {}), "locked_containers": set()}
    records = []
    failure = None
    run_id = uuid4().hex
    if on_event is not None:
        on_event({"type": "started", "run_id": run_id, "total_steps": len(steps), "state": deepcopy(initial)})
    for index, step in enumerate(steps):
        before = state.snapshot()
        try:
            candidate, event = get_action(step["action"])(state, step, context)
            elapsed = event.get("duration_s", 0.0)
            previous_model = deepcopy(state.chemical_model_state)
            candidate.chemical_model_state = model.advance(previous_model, candidate,
                step=deepcopy(step), event=deepcopy(event), elapsed_time_s=elapsed, config=config)
            if step["action"] == "electrolyze":
                event["prediction"] = model.observe(candidate.chemical_model_state, candidate,
                                                     event["conditions"], context["species"])
                if not isinstance(event["prediction"], dict):
                    raise ValueError("model observation must be an object")
            check = model.validate_transition(state, candidate, state.chemical_model_state,
                                               candidate.chemical_model_state, config=config)
            if not isinstance(check, dict) or check.get("passed") is not True:
                raise ValueError("chemical model transition rejected")
            conservation = check_transition(inventory, state, candidate, context["locked_containers"])
            candidate.ledger.record_step(state_revision=state.ledger.revision + 1,
                step_id=step["step_id"], action=step["action"], outcome="succeeded",
                source="local_workflow_simulation", simulation_time_s=None, command=step,
                receipt={"event": event, "actual_source": "numerical_simulation",
                         "model_elapsed_time_s": elapsed, "chemical_model_transition_check": check},
                terminal=index == len(steps) - 1)
            candidate.ledger.provenance[-1]["workflow_event"] = deepcopy(event)
            if step["action"] in {"grasp_electrode", "install_electrode", "connect_electrode"}:
                entity = "electrode:" + step["electrode_id"]
                candidate.ledger.register_entity(entity, "electrode")
                if step["action"] == "grasp_electrode":
                    relation, obj = "held_by", "robot_gripper"
                elif step["action"] == "install_electrode":
                    relation, obj = "installed_in", step["target"]
                else:
                    # Preserve the electrode's separate installation relation.
                    entity = "connection:" + step["electrode_id"]
                    candidate.ledger.register_entity(entity, "electrode_connection",
                                                     electrode_id=step["electrode_id"])
                    relation, obj = "connected_to", step["device_id"]
                if relation:
                    candidate.ledger._set_relation(entity, relation, obj,
                        revision=candidate.ledger.revision, command_id=step["step_id"],
                        source="semantic_simulation")
            if step["action"] == "electrolyze":
                context["locked_containers"].add(step["target"])
            if context["locked_containers"]:
                candidate.ledger.certainty["chemical"] = {"status": "uncertain",
                    "source": "post_electrolysis_inventory_unmodeled", "state_revision": candidate.ledger.revision}
            state = candidate
            records.append({"status": "completed", "event": event, "before": before,
                            "after": state.snapshot(), "conservation": conservation})
            if on_event is not None:
                on_event({"type": "step", "run_id": run_id, "index": index + 1,
                          "total_steps": len(steps), "record": deepcopy(records[-1])})
        except (ValueError, KeyError, TypeError) as exc:
            failure = {"step_id": step["step_id"], "action": step["action"], "reason": str(exc)}
            state.ledger.record_step(state_revision=state.ledger.revision, step_id=step["step_id"],
                action=step["action"], outcome="failed", source="local_workflow_simulation",
                simulation_time_s=None, command=step, receipt=failure)
            records.append({"status": "failed", "event": failure, "before": before,
                            "after": state.snapshot()})
            if on_event is not None:
                on_event({"type": "step", "run_id": run_id, "index": index + 1,
                          "total_steps": len(steps), "record": deepcopy(records[-1])})
            break
    unsupported = any(r.get("event", {}).get("action") == "electrolyze" and
                      not r["event"].get("prediction", {}).get("valid", False) for r in records)
    status = "failed" if failure else ("completed_prediction_unavailable" if unsupported else "completed")
    result = {"run_id": run_id, "status": status,
            "data_origin": "numerical_simulation", "seed": seed, "failure": failure,
            "config": deepcopy(config),
            "initial_state": initial, "final_state": state.snapshot(), "steps": records,
            "error_model_realized": errors.snapshot(), "model_id": model.model_id,
            "locked_containers": sorted(context["locked_containers"]),
            "conservation": check_conservation(inventory, state.inventory()),
            "conservation_scope": "recorded inventories only; reaction is not modeled"}
    if on_event is not None:
        on_event({"type": "finished", "run_id": run_id, "status": status, "failure": deepcopy(failure)})
    return result

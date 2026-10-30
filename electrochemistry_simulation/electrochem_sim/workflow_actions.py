"""Extensible numerical workflow actions; never controls physical equipment."""
from __future__ import annotations

import math
from copy import deepcopy

from .protocol import Action
from .liquid_transfer import apply_action
from .mixing import apply_mixing

_HANDLERS = {}


def register_action(name, handler):
    if not isinstance(name, str) or not name or name in _HANDLERS or not callable(handler):
        raise ValueError("invalid or duplicate workflow action")
    _HANDLERS[name] = handler


def get_action(name):
    if name not in _HANDLERS:
        raise ValueError(f"unsupported workflow action: {name}")
    return _HANDLERS[name]


def liquid(state, step, context):
    kind = step["action"]
    source, target = step.get("source"), step.get("target")
    if source in context["locked_containers"] or target in context["locked_containers"]:
        raise ValueError("post-electrolysis inventory is unresolved; liquid operation blocked")
    volume_ul = step.get("volume_ul")
    if kind == "aspirate":
        if source not in state.containers or isinstance(volume_ul, bool) or not isinstance(volume_ul, (int, float)) or not math.isfinite(volume_ul) or not 20 <= volume_ul <= 1000:
            raise ValueError("aspirate requires existing source and 20–1000 µL")
    if kind == "dispense" and (target not in state.containers or volume_ul is not None):
        raise ValueError("dispense requires existing target and transfers available tip liquid")
    if kind == "mix" and target not in state.containers:
        raise ValueError("mix requires existing target")
    duration = step.get("duration_s")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be positive")
    command = Action(step["step_id"], kind, source=source, target=target,
                     volume_l=None if volume_ul is None else volume_ul / 1e6,
                     pipette_id=step.get("pipette_id"), duration_s=duration)
    realized = context["errors"].realize(command, context["rng"])
    if kind == "mix":
        candidate, event = apply_mixing(state, target, command.step_id, realized.mixing_efficiency)
        event["error_application_status"] = realized.error_status
    else:
        candidate, event = apply_action(state, realized)
    event["duration_s"] = duration or 0.0
    return candidate, event


def electrode(state, step, context):
    electrode_id = step.get("electrode_id")
    if not isinstance(electrode_id, str) or not electrode_id:
        raise ValueError("electrode_id required")
    candidate = deepcopy(state)
    setup = candidate.cell_setup.setdefault("workflow_electrodes", {})
    arm = candidate.cell_setup.get("workflow_held_electrode")
    kind = step["action"]
    if kind == "grasp_electrode":
        if arm is not None or electrode_id in setup:
            raise ValueError("gripper occupied or electrode already installed")
        candidate.cell_setup["workflow_held_electrode"] = electrode_id
    elif kind == "install_electrode":
        if arm != electrode_id:
            raise ValueError("electrode must be grasped before installation")
        target, material, role = step.get("target"), step.get("material"), step.get("role")
        if target not in candidate.containers or target in context["locked_containers"]:
            raise ValueError("invalid or unresolved target")
        if not isinstance(material, str) or not material or role not in {"anode", "cathode", "reference"}:
            raise ValueError("electrode material and role required")
        if any(item["target"] == target and item["role"] == role for item in setup.values()):
            raise ValueError("electrode role already installed")
        setup[electrode_id] = {"target": target, "material": material, "role": role}
        candidate.cell_setup["workflow_held_electrode"] = None
    else:
        if electrode_id not in setup or setup[electrode_id]["target"] in context["locked_containers"]:
            raise ValueError("electrode must be installed before connection")
        device = step.get("device_id")
        if not isinstance(device, str) or not device:
            raise ValueError("device_id required")
        if setup[electrode_id].get("device_id"):
            raise ValueError("electrode already connected")
        setup[electrode_id]["device_id"] = device
    candidate.completed_steps += 1
    return candidate, {"step_id": step["step_id"], "action": kind,
                       "electrode_id": electrode_id, "target": step.get("target"),
                       "material": step.get("material"), "role": step.get("role"),
                       "evidence": "semantic_simulation_only"}


def electrolysis(state, step, context):
    target = step.get("target")
    if target not in state.containers or target in context["locked_containers"]:
        raise ValueError("invalid or already electrolyzed target")
    if state.tip.loaded or state.cell_setup.get("workflow_held_electrode") is not None:
        raise ValueError("tip or electrode still held")
    if not state.containers[target].mixed:
        raise ValueError("reactor is not homogeneous; no calibrated mixing correction")
    installed = [v for v in state.cell_setup.get("workflow_electrodes", {}).values() if v["target"] == target]
    roles = {v["role"]: v["material"] for v in installed}
    if not {"anode", "cathode"}.issubset(roles):
        raise ValueError("anode and cathode must be installed")
    devices = {v.get("device_id") for v in installed if v["role"] in {"anode", "cathode"}}
    if None in devices or len(devices) != 1:
        raise ValueError("both electrodes must be connected to the same instrument")
    current, duration = step.get("current_a"), step.get("duration_s")
    for value in (current, duration):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("current and duration must be finite and positive")
    if step.get("mode") != "constant_current":
        raise ValueError("only constant-current electrolysis is supported")
    if not all(isinstance(step.get(key), str) and step[key] for key in ("electrolyte", "cell_configuration")):
        raise ValueError("electrolyte and cell_configuration required")
    candidate = deepcopy(state)
    conditions = {"reaction_container": target, "current_a": current, "duration_s": duration,
                  "anode": roles["anode"], "cathode": roles["cathode"],
                  "mode": step.get("mode"), "electrolyte": step.get("electrolyte"),
                  "cell_configuration": step.get("cell_configuration")}
    candidate.completed_steps += 1
    event = {"step_id": step["step_id"], "action": "electrolyze", "target": target,
             "duration_s": duration, "charge_c": current * duration, "conditions": conditions,
             "execution": "semantic_simulation_only",
             "post_reaction_inventory": "unresolved"}
    return candidate, event


for _kind in ("load_tip", "aspirate", "dispense", "discard_tip", "mix"):
    register_action(_kind, liquid)
for _kind in ("grasp_electrode", "install_electrode", "connect_electrode"):
    register_action(_kind, electrode)
register_action("electrolyze", electrolysis)

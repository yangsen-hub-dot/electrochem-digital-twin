"""First-process bridge: conditions -> stock transfer -> executor receipts.

This module never claims that preparing a command executes Matterix. The
chemical ledger changes only after an explicit successful execution receipt.
"""

from __future__ import annotations

import importlib
import math
import random
import re
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4
from measurement_session import apply_measurement, measurement_steps, validate_measurement_receipt
from pipette_workflow import expand_pipette_tool_actions


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}必须有限且大于零")
    return float(value)


class TransferSession:
    """One left-solution-preparation session, with optional water addition.

    Optional stock-tip disposal closes this process; dilution, mixing and voltage
    belong to the later full experiment. Partial execution pauses the ledger
    for reconciliation rather than pretending that a physical action rolled back.
    """

    def __init__(self, engine_root, conditions, seed=0, tip_capacity_ul=1200.0,
                 executor="matterix", run_id=None, include_discard_tip=False,
                 include_left_water=False, include_left_mix=False,
                 include_paired_mix=False, include_left_electrode=False,
                 include_right_electrode=False, include_salt_bridge=False, include_measurement=False):
        if executor not in {"matterix", "test_stub"}:
            raise ValueError("执行器必须为 matterix 或 test_stub")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("随机种子必须是整数")
        self.lock = RLock()
        self.root = Path(engine_root).resolve()
        if not (self.root / "electrochem_sim" / "simulator.py").is_file():
            raise ValueError("找不到电化学引擎目录")
        sys.path.insert(0, str(self.root)) if str(self.root) not in sys.path else None
        simulator = importlib.import_module("electrochem_sim.simulator")
        condition_input = importlib.import_module("electrochem_sim.condition_input")
        chemical_models = importlib.import_module("electrochem_sim.chemical_models")
        protocol = importlib.import_module("electrochem_sim.protocol")
        state = importlib.import_module("electrochem_sim.state")
        errors = importlib.import_module("electrochem_sim.error_model")
        self.apply_action = importlib.import_module("electrochem_sim.liquid_transfer").apply_action
        self.apply_mixing = importlib.import_module("electrochem_sim.mixing").apply_mixing
        self.apply_paired_mixing = importlib.import_module("electrochem_sim.mixing").apply_paired_mixing
        self.apply_tool_assembly = importlib.import_module("electrochem_sim.tool_assembly").apply_tool_assembly
        self.check_conservation = importlib.import_module("electrochem_sim.conservation").check_conservation
        base = simulator.load_config(self.root / "config" / "copper_concentration_cell.json")
        self.config = condition_input.build_condition_config(base, conditions)
        self.chemical_model = chemical_models.resolve_chemical_model(self.config)
        self.config.setdefault("cell", {})["chemical_model_id"] = self.chemical_model.model_id
        self.conditions = deepcopy(self.config["condition_inputs"])
        include_salt_bridge = include_salt_bridge or include_measurement
        include_right_electrode = include_right_electrode or include_salt_bridge
        include_left_electrode = include_left_electrode or include_right_electrode
        if include_salt_bridge and not include_paired_mix:
            raise ValueError("盐桥连接需要先完成双侧配液、混匀与电极安装")
        definitions = []
        if include_paired_mix:
            for step in self.config["protocol"]:
                step_id = step["step_id"]
                if step_id.startswith(("ui_left_stock_", "ui_left_water_",
                                       "ui_right_stock_", "ui_right_water_")):
                    if step["action"] == "discard_tip" and not include_discard_tip:
                        continue
                    definitions.append(deepcopy(step))
            # A cell whose target matches the stock concentration needs no water.
            # Require actual preparation of both recipients, not dilution steps.
            prepared_targets = {step.get("target") for step in definitions
                                if step["action"] == "dispense"}
            if not {"left_half_cell", "right_half_cell"}.issubset(prepared_targets):
                raise ValueError("同步混匀流程需要完成左右两侧定量配液")
            definitions.append({"step_id": "ui_simultaneous_mix", "action": "mix_pair",
                                "targets": ["left_half_cell", "right_half_cell"],
                                "duration_s": self.conditions.get("mixing_duration_s", 10.0)})
            if include_left_electrode:
                if not include_discard_tip:
                    raise ValueError("左侧电极安装流程需要先完成弃头")
                definitions.extend([
                    {"step_id": "ui_park_pipette", "action": "park_pipette",
                     "target": "pipette_stand"},
                    {"step_id": "ui_pick_left_copper_electrode", "action": "pick_electrode",
                     "source": "electrode_rack", "target": "left_half_cell",
                     "component": "copper_electrode"},
                    {"step_id": "ui_place_left_copper_electrode", "action": "place_electrode",
                     "target": "left_half_cell", "component": "copper_electrode"},
                ])
            if include_right_electrode:
                definitions.extend([
                    {"step_id": "ui_pick_right_copper_electrode", "action": "pick_electrode",
                     "source": "electrode_rack", "target": "right_half_cell",
                     "component": "copper_electrode"},
                    {"step_id": "ui_place_right_copper_electrode", "action": "place_electrode",
                     "target": "right_half_cell", "component": "copper_electrode"},
                ])
            if include_salt_bridge:
                definitions.extend([
                    {"step_id": "ui_pick_salt_bridge", "action": "pick_salt_bridge",
                     "source": "salt_bridge_rack", "target": "salt_bridge_holder",
                     "targets": ["left_half_cell", "right_half_cell"], "component": "salt_bridge"},
                    {"step_id": "ui_connect_salt_bridge", "action": "connect_salt_bridge",
                     "target": "salt_bridge_holder", "targets": ["left_half_cell", "right_half_cell"],
                     "component": "salt_bridge"},
                ])
        else:
            water_started = False
            for step in self.config["protocol"]:
                step_id = step["step_id"]
                if step_id.startswith("ui_left_stock_"):
                    if water_started:
                        break
                    if step["action"] == "discard_tip" and not include_discard_tip:
                        break
                    definitions.append(deepcopy(step))
                    continue
                if step_id.startswith("ui_left_water_"):
                    if not include_left_water:
                        break
                    water_started = True
                    if step["action"] == "discard_tip":
                        if include_discard_tip:
                            definitions.append(deepcopy(step))
                        if not include_left_mix:
                            break
                        continue
                    definitions.append(deepcopy(step))
                    continue
                if step_id == "ui_left_mix":
                    if include_left_mix and water_started:
                        definitions.append(deepcopy(step))
                    break
                if water_started:
                    break
            # Keep the single-cell adapter available although the full protocol
            # now schedules its mixing only after both cells are prepared.
            if include_left_mix and water_started and not any(
                    step["action"] == "mix" for step in definitions):
                definitions.append({"step_id": "ui_left_mix", "action": "mix",
                                    "target": "left_half_cell"})
        if include_measurement:
            definitions.extend(measurement_steps())
        definitions = expand_pipette_tool_actions(definitions)
        if not definitions or definitions[0]["action"] != "pick_pipette":
            raise ValueError("实验条件没有生成左侧母液转移过程")
        process_config = deepcopy(self.config)
        process_config["protocol"] = definitions
        self.actions = protocol.build_protocol(process_config)
        self.includes_paired_mix = any(action.kind == "mix_pair" for action in self.actions)
        self.includes_left_electrode = any(action.kind == "place_electrode" and
                                            action.target == "left_half_cell" for action in self.actions)
        self.includes_right_electrode = any(action.kind == "place_electrode" and
                                             action.target == "right_half_cell" for action in self.actions)
        self.includes_left_mix = self.includes_paired_mix or any(action.kind == "mix" for action in self.actions)
        self.includes_salt_bridge = any(action.kind == "connect_salt_bridge" for action in self.actions)
        self.includes_measurement = include_measurement
        self.scope = ('complete_virtual_concentration_cell' if include_measurement else
                      "paired_preparation_electrodes_and_salt_bridge" if self.includes_salt_bridge else
                      "paired_preparation_mixing_and_both_electrodes_installation"
                      if self.includes_left_electrode and self.includes_right_electrode else
                      "paired_preparation_mixing_and_left_electrode_installation" if self.includes_left_electrode
                      else "paired_solution_preparation_and_simultaneous_mixing" if self.includes_paired_mix
                      else "left_solution_mixing" if self.includes_left_mix
                      else "left_solution_preparation" if any(a.step_id.startswith("ui_left_water_") for a in self.actions)
                      else "left_stock_transfer")
        self.includes_left_water = any(a.step_id.startswith("ui_left_water_") for a in self.actions)
        self.voltage_reason = ('须完成本次接线与虚拟测量回执，尚无已确认读数' if include_measurement else
                               "当前流程包含盐桥连接；本段不执行仪表接线或电压测量，电压保持未测"
                               if self.includes_salt_bridge else
                               "左右两侧已配液并同步混匀，左右铜电极均已安装；盐桥尚未连接，因此不计算电压"
                               if self.includes_left_electrode and self.includes_right_electrode else
                               "左右两侧已配液并同步混匀；当前只安装左侧铜电极，右侧电极和盐桥尚未连接，因此不计算电压"
                               if self.includes_left_electrode else
                               "左右两侧定量配液与同步混匀已完成；尚未组装电池或测量电压"
                               if self.includes_paired_mix else
                               "左侧定量配液与混匀已完成；右侧尚未配制，电池尚未组装或测量电压"
                               if self.includes_left_mix else
                               "左侧目标浓度与体积已由母液和定量加水形成；尚未混匀、组装电池或测量电压"
                               if self.includes_left_water else "当前只执行母液转移，尚未配液和组装电池")
        self.tip_capacity_ul = positive_number(tip_capacity_ul, "枪头容量")
        self.state = state.build_initial_state(self.config)
        self.state.chemical_model_id = self.chemical_model.model_id
        self.state.chemical_model_state = self.chemical_model.initialize_state(self.config, self.state)
        self.state.ledger.set_chemical_model(
            self.chemical_model.model_id, self.chemical_model.model_family,
            self.chemical_model.state_schema_version,
        )
        self.state.ledger.workflow["status"] = "running"
        self.state.cell_setup.update({
            "active_tool": "none", "pipette_parked": True,
            "active_pipette_id": None,
            "pipette_inventory": {"gilson_pipetman_p200": "parked",
                                  "gilson_pipetman_p1000": "parked"},
        })
        self.state.tip.liquid.capacity_l = self.tip_capacity_ul / 1_000_000
        self.initial_inventory = self.state.inventory()
        self.rng = random.Random(seed)
        self.errors = errors.ErrorModel(self.config.get("error_model"), rng=self.rng)
        self.seed = seed
        self.executor = executor
        if run_id is not None and not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("运行编号必须为 32 位小写十六进制字符串")
        self.run_id = run_id or uuid4().hex
        self.status = "running"
        self.index = 0
        self.revision = 0
        self.pending = None
        self.receipts = {}
        self.events = []
        self.uncertain = False
        self.simulation_time_s = None
        self.video = {"status": "not_connected", "recording_ref": None}
        self._emit("run.started", None, {
            "scope": self.scope, "executor": executor,
            "conditions": self.conditions, "seed": seed,
            "tip_capacity_ul": self.tip_capacity_ul,
        })

    def _emit(self, kind, action, payload):
        event = {
            "contract_version": "1.0.0", "event_id": uuid4().hex,
            "run_id": self.run_id, "seq": len(self.events) + 1,
            "type": kind, "step_id": action.step_id if action else None,
            "action": action.kind if action else None, "producer": "coordinator",
            "occurred_at_utc": utc_now(), "simulation_time_s": self.simulation_time_s,
            "payload": deepcopy(payload),
        }
        self.events.append(event)
        return event

    def _apply(self, realized, paired_efficiencies=None):
        if realized.command.kind in {'pick_lead', 'connect_lead', 'measure_voltage'}:
            candidate, event = apply_measurement(self.state, realized.command, self.config)
        elif realized.command.kind == "mix":
            candidate, event = self.apply_mixing(
                self.state, realized.command.target, realized.command.step_id,
                realized.mixing_efficiency)
            event["error_application_status"] = realized.error_status
        elif realized.command.kind == "mix_pair":
            candidate, event = self.apply_paired_mixing(
                self.state, realized.command.targets, realized.command.step_id,
                paired_efficiencies or {})
            event["duration_s"] = realized.command.duration_s
            event["error_application_status"] = realized.error_status
            event["paired_cell_correlation"] = self.errors.paired_mixing_correlation
            candidate.cell_setup["left_solution_mixed"] = True
            candidate.cell_setup["right_solution_mixed"] = True
        elif realized.command.kind in {"park_pipette", "pick_pipette", "pick_electrode", "place_electrode", "pick_salt_bridge", "connect_salt_bridge"}:
            candidate, event = self.apply_tool_assembly(
                self.state, realized.command.kind, realized.command.step_id,
                source=realized.command.source, target=realized.command.target,
                component=realized.command.component,
                pipette_id=realized.command.pipette_id)
        else:
            candidate, event = self.apply_action(self.state, realized)
        conservation = self.check_conservation(self.initial_inventory, candidate.inventory())
        if not conservation["passed"]:
            raise ValueError("物料守恒检查未通过")
        return candidate, event, conservation

    def prepare(self):
        with self.lock:
            if self.pending:
                return deepcopy(self.pending["command"])
            if self.status != "running":
                raise ValueError("当前运行不可准备下一动作")
            action = self.actions[self.index]
            # Transactional preparation also protects RNG/profile state if
            # validation fails. Repeated prepare returns the cached command.
            candidate_rng = deepcopy(self.rng)
            candidate_errors = deepcopy(self.errors)
            realized = candidate_errors.realize(action, candidate_rng)
            paired_efficiencies = (candidate_errors.realize_paired_mixing(candidate_rng, action.duration_s)
                                   if action.kind == "mix_pair" else None)
            if paired_efficiencies:
                mix_status = ("applied" if candidate_errors.enabled and
                              candidate_errors.snapshot()["mixing"]["status"] == "parameterized"
                              else "not_parameterized")
                realized = replace(realized, mixing_efficiency=paired_efficiencies[action.target],
                                   error_status=mix_status)
            candidate, event, check = self._apply(realized, paired_efficiencies)
            factor = {"aspirate": "pipetting", "dispense": "tip_carryover",
                      "mix": "mixing", "mix_pair": "mixing"}.get(action.kind)
            requirements = {
                "load_tip": ["attach_tip", "verify_tip_attached"],
                "aspirate": ["move_to_source", "verify_aspiration_position", "aspirate"],
                "dispense": ["move_to_target", "verify_dispense_position", "dispense"],
                "discard_tip": ["move_to_waste", "verify_waste_position", "release_tip", "verify_tip_detached"],
                "mix": ["stirrer_asset_present", "cell_on_stir_plate", "stirring_enabled", "mixing_error_applied"],
                "mix_pair": ["stirrer_asset_present", "both_cells_on_stir_plate", "simultaneous_stirring_enabled", "paired_mixing_errors_applied"],
                "park_pipette": ["tip_discarded", "pipette_parked_in_stand", "gripper_released"],
                "pick_pipette": ["pipette_stand_present", "pipette_retrieved", "gripper_holds_pipette"],
                "pick_electrode": ["pipette_parked", "electrode_rack_present", "gripper_holds_copper_electrode"],
                "place_electrode": ["target_cell_verified", "electrode_clear_of_stir_bar", "electrode_released_on_support"],
                "pick_salt_bridge": ["both_electrodes_installed", "bridge_rack_present", "bridge_grasp_verified"],
                "connect_salt_bridge": ["both_ends_immersed", "bridge_clear_of_electrodes", "bridge_supported", "gripper_released"],
                'pick_lead': ['gripper_free', 'lead_rack_supported', 'lead_grasp_verified'],
                'connect_lead': list(__import__('measurement_session').REQUIRED_GEOMETRY) + ['gripper_released'],
                'measure_voltage': ['both_leads_connected', 'cell_output_off', 'existing_solver_valid'],
            }[action.kind]
            planned = None if action.volume_l is None else action.volume_l * 1_000_000
            if action.kind == "dispense":
                planned = self.actions[self.index - 1].volume_l * 1_000_000
                event["requested_volume_ul"] = planned
                event["residual_fraction_of_aliquot"] = (
                    event.get("residual_volume_ul", 0.0) / planned if planned > 0 else 0.0)
                event["retained_tip_fraction"] = event["residual_fraction_of_aliquot"]
            material_id = ('measurement' if action.kind in {'pick_lead', 'connect_lead', 'measure_voltage'} else
                           "salt_bridge" if action.kind in {"pick_salt_bridge", "connect_salt_bridge"}
                           else "mixing" if action.kind in {"mix", "mix_pair"}
                           else "electrode" if action.kind in {"pick_electrode", "place_electrode"}
                           else "tool" if action.kind in {"park_pipette", "pick_pipette"}
                           else "water" if "_water_" in action.step_id else "copper_stock")
            command = {
                "contract_version": "1.0.0", "run_id": self.run_id,
                "command_id": uuid4().hex, "step_id": action.step_id,
                "action": action.kind, "executor": self.executor,
                "material_id": material_id,
                "state_revision": self.revision, "source": action.source,
                "target": action.target, "requested_volume_ul": planned,
                "pipette_id": action.pipette_id,
                "actual_volume_ul": (event.get("simulated_volume_ul") or 0.0) if action.kind == "discard_tip" else event.get("simulated_volume_ul"),
                "tip_residual_volume_ul": event.get("residual_volume_ul") if action.kind == "dispense" else None,
                "tip_residual_fraction_of_aliquot": event.get("residual_fraction_of_aliquot") if action.kind == "dispense" else None,
                "retained_tip_volume_ul": candidate.tip.liquid.volume_l * 1_000_000,
                "tip_capacity_ul": self.tip_capacity_ul,
                "execution_requirements": requirements,
                "actual_source": "simulated_error" if factor in {"pipetting", "tip_carryover"} else "command_assumed",
                "error": {
                    "factor": factor, "status": realized.error_status,
                    "systematic_offset_ul": realized.systematic_offset_l * 1_000_000,
                    "random_offset_ul": realized.random_offset_l * 1_000_000,
                    "profile": deepcopy(candidate_errors.snapshot().get(factor)) if factor else None,
                },
            }
            if action.kind in {'pick_lead', 'connect_lead', 'measure_voltage'}:
                command.update(component=action.component, liquid_volume_change_ul=0.,
                               actual_source='existing_engine_ideal_virtual_meter' if action.kind == 'measure_voltage'
                               else 'kinematic_visual_and_state_proxy', cell_output_enabled=False,
                               error={'factor': None, 'status': 'not_applicable'})
                if action.kind == 'measure_voltage':
                    command['voltage'] = deepcopy(event['voltage'])
                else:
                    command['lead_roles'] = deepcopy(event['lead_roles'])
            elif action.kind == "mix":
                command["mixing_efficiency"] = realized.mixing_efficiency
                command["mixing_method"] = "CHEM-008_IKA_Plate_RCT_digital"
                command["mixing_efficiency_source"] = "existing_electrochemistry_simulation_mixing_error_model"
                command["liquid_volume_change_ul"] = 0.0
            elif action.kind in {"pick_salt_bridge", "connect_salt_bridge"}:
                command.update(component="salt_bridge", targets=list(action.targets),
                               actual_source="kinematic_visual_and_state_proxy",
                               error={"factor": None, "status": "not_applicable"},
                               liquid_volume_change_ul=0.0,
                               salt_bridge_type=self.conditions['salt_bridge_type'],
                               salt_bridge_concentration_m=self.conditions['salt_bridge_concentration_m'])
            elif action.kind in {"park_pipette", "pick_pipette", "pick_electrode", "place_electrode"}:
                command["component"] = action.component
                command["actual_source"] = "kinematic_visual_and_state_proxy"
                command["error"] = {"factor": None, "status": "not_applicable"}
                if action.kind == "park_pipette":
                    command["support_id"] = f"pipette_stand_{action.pipette_id}"
                    command["pipette_display_name"] = "P200" if action.pipette_id.endswith("_p200") else "P1000"
                elif action.kind == "pick_pipette":
                    command.update(source=f"pipette_stand_{action.pipette_id}",
                                   support_id=f"pipette_stand_{action.pipette_id}",
                                   pipette_display_name="P200" if action.pipette_id.endswith("_p200") else "P1000",
                                   gripper_opening_m=.08)
                elif action.kind == "pick_electrode":
                    command["source"] = "electrode_rack"
                    command["gripper_opening_m"] = .010
                    command["catalog_asset_id"] = "CHEM-016"
                else:
                    command["insertion_depth_m"] = .042
                    command["electrode_tip_clearance_above_cell_floor_m"] = .003
                    command["reserved_salt_bridge_axis_x_m"] = .0025
            elif action.kind == "mix_pair":
                command["targets"] = list(action.targets)
                command["mixing_efficiencies"] = paired_efficiencies
                command["duration_s"] = action.duration_s or self.conditions.get("mixing_duration_s", 10.0)
                command["mixing_method"] = "CHEM-008_IKA_Plate_RCT_digital"
                command["mixing_efficiency_source"] = "existing_electrochemistry_simulation_mixing_error_model"
                command["actual_source"] = command["mixing_efficiency_source"]
                command["paired_cell_correlation"] = candidate_errors.paired_mixing_correlation
                command["liquid_volume_change_ul"] = 0.0
            self.rng = candidate_rng
            self.errors = candidate_errors
            self.pending = {"command": command, "realized": realized,
                            "candidate": candidate, "event": event, "check": check,
                            "paired_efficiencies": paired_efficiencies}
            self._emit("step.prepared", action, command)
            return deepcopy(command)

    def accept_receipt(self, receipt):
        with self.lock:
            if not isinstance(receipt, dict):
                raise ValueError("执行回执必须为对象")
            command_id = receipt.get("command_id")
            if not isinstance(command_id, str):
                raise ValueError("回执缺少 command_id")
            if command_id in self.receipts:
                saved = self.receipts[command_id]
                if saved["receipt"] != receipt:
                    raise ValueError("同一 command_id 的重复回执内容冲突")
                return deepcopy(saved["result"])
            if self.status != "running" or not self.pending:
                raise ValueError("没有等待执行回执的动作")
            command = self.pending["command"]
            for name in ("run_id", "command_id", "step_id", "executor"):
                if receipt.get(name) != command[name]:
                    raise ValueError(f"回执 {name} 与等待中的动作不一致")
            outcome = receipt.get("status")
            if outcome not in {"succeeded", "failed", "partial"}:
                raise ValueError("回执状态必须为 succeeded、failed 或 partial")
            sim_time = receipt.get("simulation_time_s")
            if (isinstance(sim_time, bool) or not isinstance(sim_time, (int, float))
                    or not math.isfinite(sim_time) or sim_time < 0):
                raise ValueError("回执需要非负有限的仿真时间")
            if self.simulation_time_s is not None and sim_time < self.simulation_time_s:
                raise ValueError("仿真时间不能倒退")
            if not isinstance(receipt.get("evidence"), dict) or not receipt["evidence"]:
                raise ValueError("回执需要执行证据引用")
            action = self.actions[self.index]
            candidate, event, check = (self.pending[name] for name in ("candidate", "event", "check"))
            source = command["actual_source"]
            actual = command["actual_volume_ul"]
            difference = 0.0
            if outcome == "succeeded":
                if receipt.get("preconditions_verified") is not True:
                    raise ValueError("执行器须确认设备与位置前提已验证")
                if action.kind == "load_tip" and receipt.get("tip_attached") is not True:
                    raise ValueError("装枪头成功回执必须确认 tip_attached")
                if action.kind in {'pick_lead', 'connect_lead', 'measure_voltage'}:
                    validate_measurement_receipt(command, receipt)
                if action.kind == "pick_pipette":
                    gap = receipt.get("gripper_gap_measured_m")
                    if (receipt.get("component_attached") != "pipette"
                            or receipt.get("pipette_id") != action.pipette_id
                            or receipt.get("support_id") != f"pipette_stand_{action.pipette_id}"
                            or isinstance(gap, bool) or not isinstance(gap, (int, float))
                            or not math.isfinite(gap) or not .007 <= gap <= .013):
                        raise ValueError("抓取移液器回执未确认型号、支架或夹持间距")
                if action.kind == "park_pipette":
                    if (receipt.get("pipette_parked") is not True
                            or receipt.get("gripper_released") is not True
                            or receipt.get("support_id") != f"pipette_stand_{action.pipette_id}"
                            or receipt.get("pipette_id") != action.pipette_id):
                        raise ValueError("停放移液器回执须确认已落在支架且夹爪已释放")
                if action.kind in {"pick_salt_bridge", "connect_salt_bridge"}:
                    if (receipt.get("actual_source") != command['actual_source']
                            or receipt.get("liquid_volume_change_ul") != 0.0):
                        raise ValueError("盐桥动作不得改变液量或重复注入误差")
                    if action.kind == "pick_salt_bridge":
                        gap = receipt.get('gripper_gap_measured_m')
                        if (receipt.get('component_attached') != 'salt_bridge'
                                or receipt.get('source') != 'salt_bridge_rack'
                                or receipt.get('target') != 'salt_bridge_holder'
                                or isinstance(gap, bool) or not isinstance(gap, (int, float))
                                or not math.isfinite(gap) or not .007 <= gap <= .013):
                            raise ValueError("盐桥抓取回执未确认来源或夹持间距")
                    else:
                        checks = receipt.get('geometry_checks', {})
                        required = ('left_end_inside_cell', 'right_end_inside_cell',
                                    'left_end_immersed', 'right_end_immersed',
                                    'electrodes_clear', 'stir_bars_clear', 'holder_contact')
                        if (receipt.get('component_installed') != 'salt_bridge'
                                or receipt.get('target') != 'salt_bridge_holder'
                                or receipt.get('gripper_released') is not True
                                or not isinstance(checks, dict)
                                or any(checks.get(key) is not True for key in required)):
                            raise ValueError("盐桥双端浸液、避让或承托回执未通过")
                if action.kind == "pick_electrode":
                    measured_gap = receipt.get("gripper_gap_measured_m")
                    if (receipt.get("component_attached") != "copper_electrode"
                            or receipt.get("source") != "electrode_rack"
                            or receipt.get("target") != command["target"]
                            or isinstance(measured_gap, bool)
                            or not isinstance(measured_gap, (int, float))
                            or not math.isfinite(measured_gap)
                            or not .007 <= measured_gap <= .013):
                        raise ValueError("抓取电极回执与电极架/夹持间距不一致")
                if action.kind == "place_electrode":
                    target = command["target"]
                    check_name = "axis_inside_right_cell" if target == "right_half_cell" else "axis_inside_left_cell"
                    geometry_checks = receipt.get("geometry_checks")
                    required_checks = {
                        check_name: True,
                        "stir_bar_clearance": True,
                        "working_plate_immersed_in_solution": True,
                        "working_plate_meets_cap_bottom": True,
                        "cap_bottom_resting_on_holder": True,
                        "salt_bridge_reserved": True,
                        "cap_supported": True,
                    }
                    if (receipt.get("component_installed") != "copper_electrode"
                            or receipt.get("target") != target
                            or not math.isclose(float(receipt.get("insertion_depth_m", -1)),
                                                command["insertion_depth_m"], abs_tol=1e-6)
                            or not isinstance(geometry_checks, dict)
                            or any(geometry_checks.get(name) is not True
                                   for name in required_checks)):
                        raise ValueError("目标铜电极位置或预留接口未通过回执检查")
                if action.kind == "discard_tip":
                    if receipt.get("tip_attached") is not False or receipt.get("waste_position_verified") is not True:
                        raise ValueError("弃头须确认枪头已脱离且位于废弃盒内")
                    disposed = receipt.get("actual_volume_ul")
                    if (isinstance(disposed, bool) or not isinstance(disposed, (int, float))
                            or not math.isfinite(disposed) or disposed < 0
                            or not math.isclose(disposed, actual, rel_tol=1e-12, abs_tol=1e-9)
                            or receipt.get("actual_source") != "command_assumed"):
                        raise ValueError("弃头只转移已有残液，不能重新抽样或改变液量")
                if action.kind == "mix":
                    actual_efficiency = receipt.get("mixing_efficiency")
                    if (isinstance(actual_efficiency, bool) or not isinstance(actual_efficiency, (int, float))
                            or not math.isfinite(actual_efficiency) or not 0 <= actual_efficiency <= 1):
                        raise ValueError("混匀成功回执须提供 0 到 1 之间的实际混匀效率")
                    if not math.isclose(actual_efficiency, command["mixing_efficiency"], rel_tol=1e-12, abs_tol=1e-12):
                        raise ValueError("执行回执混匀效率与本次已抽取误差值不一致")
                    if receipt.get("mixing_method") != command["mixing_method"]:
                        raise ValueError("混匀回执方法与本次执行命令不一致")
                    if (receipt.get('actual_source') != command['mixing_efficiency_source']
                            or receipt.get('liquid_volume_change_ul') != 0.0):
                        raise ValueError('混匀回执必须沿用既有模型效率且不改变液量')
                if action.kind == "mix_pair":
                    efficiencies = receipt.get("mixing_efficiencies")
                    if efficiencies != command["mixing_efficiencies"]:
                        raise ValueError("同步混匀回执的左右效率必须与本次独立抽样值一致")
                    if (receipt.get("mixing_method") != command["mixing_method"]
                            or receipt.get("actual_source") != command["mixing_efficiency_source"]
                            or receipt.get("liquid_volume_change_ul") != 0.0):
                        raise ValueError("同步混匀回执必须沿用既有误差模型且不改变液量")
                    source = command["mixing_efficiency_source"]
                if action.kind in {"aspirate", "dispense"}:
                    actual = positive_number(receipt.get("actual_volume_ul"), "实际执行体积")
                    source = receipt.get("actual_source")
                    if source not in {"simulated_error", "virtual_device_report"}:
                        raise ValueError("首版实际量来源必须为模拟误差或虚拟设备反馈")
                    difference = actual - command["actual_volume_ul"]
                    if source == "simulated_error" and not math.isclose(
                            actual, command["actual_volume_ul"], rel_tol=1e-12, abs_tol=1e-9):
                        raise ValueError("模拟执行量必须与已准备量一致，不可再次添加误差")
                    if source == "virtual_device_report" and not receipt.get("difference_reason"):
                        raise ValueError("设备反馈替代模型量时须提供 difference_reason")
                    if source == "virtual_device_report":
                        # Exact final volume; disable residual deduction here
                        # because the reported delivered amount already includes it.
                        exact = replace(self.pending["realized"], volume_l=actual / 1_000_000,
                                        tip_residual_l=0.0, tip_residual_fraction=0.0)
                        try:
                            if action.kind == "dispense" and exact.volume_l > self.state.tip.liquid.volume_l:
                                raise ValueError("设备反馈交付量超过枪头库存；须核对状态")
                            candidate, event, check = self._apply(exact)
                        except ValueError as exc:
                            # The executor says the action already happened.
                            # Invalid chemical reconciliation must halt dispatch.
                            self.uncertain = True
                            self.status = "paused"
                            self.simulation_time_s = float(sim_time)
                            self.state.ledger.record_step(
                                state_revision=self.revision, step_id=action.step_id,
                                action=action.kind, outcome="partial", source=self.executor,
                                simulation_time_s=self.simulation_time_s, command=command,
                                receipt={**receipt, "reason": f"执行后状态无法核对：{exc}"},
                                partial=True,
                            )
                            self._emit("step.failed", action, {
                                "partial_execution": True,
                                "reason": f"执行后状态无法核对：{exc}",
                                "executor_receipt": receipt,
                            })
                            self._emit("run.paused", None, {"state_uncertain": True})
                            self.pending = None
                            result = self.snapshot()
                            self.receipts[command_id] = {"receipt": deepcopy(receipt), "result": deepcopy(result)}
                            return result
            elif not isinstance(receipt.get("reason"), str) or not receipt["reason"].strip():
                raise ValueError("失败或部分执行必须记录原因")
            if outcome == "failed" and receipt.get("no_state_change") is not True:
                raise ValueError("无影响失败须确认 no_state_change；否则应报告 partial")
            self.simulation_time_s = float(sim_time)
            if outcome == "succeeded":
                # The first executor action has no prior clock baseline. When
                # present, use the duration since the last committed receipt.
                previous_time = getattr(self, "_last_model_time_s", 0.0)
                elapsed_time_s = receipt.get("model_elapsed_time_s")
                if (isinstance(elapsed_time_s, bool) or
                        not isinstance(elapsed_time_s, (int, float)) or
                        not math.isfinite(elapsed_time_s) or elapsed_time_s < 0):
                    elapsed_time_s = max(0.0, self.simulation_time_s - previous_time)
                receipt["model_elapsed_time_s"] = float(elapsed_time_s)
                prior_model_state = deepcopy(self.state.chemical_model_state)
                candidate.chemical_model_id = self.chemical_model.model_id
                try:
                    candidate.chemical_model_state = self.chemical_model.advance(
                        candidate.chemical_model_state, candidate,
                        step=command, event=event, elapsed_time_s=elapsed_time_s,
                        config=self.config,
                    )
                    model_check = self.chemical_model.validate_transition(
                        self.state, candidate, prior_model_state,
                        candidate.chemical_model_state, config=self.config,
                    )
                    if not isinstance(model_check, dict) or model_check.get("passed") is not True:
                        raise ValueError("化学模型状态转移校验未通过")
                except Exception as exc:
                    model_receipt = {**receipt, "status": "partial",
                                     "reason": f"执行回执成功，但化学模型状态无法核对：{exc}"}
                    self.uncertain = True
                    self.status = "paused"
                    self.state.ledger.record_step(
                        state_revision=self.revision, step_id=action.step_id,
                        action=action.kind, outcome="partial", source=self.executor,
                        simulation_time_s=self.simulation_time_s, command=command,
                        receipt=model_receipt, partial=True,
                    )
                    self._emit("step.failed", action, {
                        "partial_execution": True, "reason": model_receipt["reason"],
                        "executor_receipt": model_receipt,
                    })
                    self._emit("run.paused", None, {"state_uncertain": True})
                    self.pending = None
                    result = self.snapshot()
                    self.receipts[command_id] = {"receipt": deepcopy(model_receipt),
                                                 "result": deepcopy(result)}
                    return result
                receipt["chemical_model_transition_check"] = model_check
                self.state = candidate
                self.revision += 1
                self.index += 1
                self.state.ledger.record_step(
                    state_revision=self.revision, step_id=action.step_id, action=action.kind,
                    outcome="succeeded", source=self.executor,
                    simulation_time_s=self.simulation_time_s, command=command, receipt=receipt,
                    terminal=self.index == len(self.actions),
                )
                self._last_model_time_s = self.simulation_time_s
                payload = {**event, "requested_volume_ul": command["requested_volume_ul"],
                           "material_id": command["material_id"],
                           "actual_volume_ul": actual, "actual_source": source,
                           "error": command["error"], "command_id": command_id,
                           "prepared_actual_volume_ul": command["actual_volume_ul"],
                           "executor_difference_ul": difference,
                           "difference_reason": receipt.get("difference_reason"),
                           "state_revision": self.revision, "conservation": check,
                           "executor_receipt": deepcopy(receipt)}
                if action.kind == "mix":
                    payload["mixing_efficiency"] = command["mixing_efficiency"]
                    payload["mixing_method"] = command["mixing_method"]
                elif action.kind == "mix_pair":
                    payload["mixing_efficiencies"] = command["mixing_efficiencies"]
                    payload["mixing_method"] = command["mixing_method"]
                    payload["paired_cell_correlation"] = command["paired_cell_correlation"]
                elif action.kind in {"park_pipette", "pick_pipette", "pick_electrode", "place_electrode", "pick_salt_bridge", "connect_salt_bridge"}:
                    payload["component"] = command.get("component")
                    payload["support_id"] = receipt.get("support_id")
                    payload["geometry_checks"] = deepcopy(receipt.get("geometry_checks"))
                self._emit("step.completed", action, payload)
                if self.index == len(self.actions):
                    self.status = "completed"
                    self._emit("run.completed", None, {"scope": self.scope,
                                                        "full_experiment_completed": bool(self.state.cell_setup.get('voltage_measurement_completed'))})
            else:
                self.uncertain = outcome == "partial"
                self.status = "paused" if self.uncertain else "failed"
                self.state.ledger.record_step(
                    state_revision=self.revision, step_id=action.step_id, action=action.kind,
                    outcome="partial" if self.uncertain else "failed", source=self.executor,
                    simulation_time_s=self.simulation_time_s, command=command, receipt=receipt,
                    partial=self.uncertain,
                )
                self._emit("step.failed", action, {"partial_execution": self.uncertain,
                                                   "executor_receipt": receipt})
                self._emit("run.paused" if self.uncertain else "run.failed", None,
                           {"reason": receipt["reason"], "state_uncertain": self.uncertain})
            self.pending = None
            result = self.snapshot()
            self.receipts[command_id] = {"receipt": deepcopy(receipt), "result": deepcopy(result)}
            return result

    def snapshot(self):
        with self.lock:
            raw = self.state.snapshot()
            stores = {**raw["containers"], "tip": raw["tip"]["liquid"], "waste": raw["waste"]}
            for store in stores.values():
                store["volume_ul"] = store.pop("volume_l") * 1_000_000
                capacity = store.pop("capacity_l")
                store["capacity_ul"] = None if capacity is None else capacity * 1_000_000
                store["concentrations_m"] = {
                    name: amount / (store["volume_ul"] / 1_000_000) if store["volume_ul"] else None
                    for name, amount in store["amounts_mol"].items()
                }
                # Parcel volumes retain explicitly named engine units.
            return {
                "contract_version": "1.0.0", "run_id": self.run_id,
                "scope": self.scope, "status": self.status,
                "executor": self.executor, "full_experiment_completed": bool(self.status == 'completed' and not self.uncertain and self.state.cell_setup.get('voltage_measurement_completed')),
                "conditions": deepcopy(self.conditions), "state_revision": self.revision,
                "plan": [{"step_id": action.step_id, "action": action.kind,
                          "source": action.source, "target": action.target,
                          "targets": list(action.targets),
                          "pipette_id": action.pipette_id,
                          "requested_volume_ul": None if action.volume_l is None else action.volume_l * 1_000_000}
                         for action in self.actions],
                "state_uncertain": self.uncertain, "last_seq": len(self.events),
                "completed_steps": self.index, "total_steps": len(self.actions),
                "current_step_id": self.actions[self.index].step_id if self.index < len(self.actions) else None,
                "simulation_time_s": self.simulation_time_s,
                "state": {"containers": {k: stores[k] for k in raw["containers"]},
                          "tip": {"attached": raw["tip"]["loaded"], "liquid": stores["tip"]},
                          "waste": stores["waste"], "temperature_k": raw["temperature_k"],
                          "cell_setup": raw["cell_setup"],
                          "chemical_model": {"model_id": raw["chemical_model_id"],
                                             "state": raw["chemical_model_state"]}},
                "state_ledger": raw["state_ledger"],
                "conservation": self.check_conservation(self.initial_inventory, self.state.inventory()),
                "error_model_realized": deepcopy(self.errors.snapshot()),
                "voltage": deepcopy(self.state.cell_setup.get('measurement_result')) or {"predicted_cell_mv": None, "virtual_meter_mv": None,
                            "measured_mv": None, "valid": False,
                            "reason": self.voltage_reason},
                "video": deepcopy(self.video),
            }

    def update_video(self, status, recording_ref=None, **metadata):
        with self.lock:
            if status not in {"recording", "ready", "failed"}:
                raise ValueError("视频状态无效")
            if status == "ready" and not recording_ref:
                raise ValueError("视频完成必须有本次录制文件引用")
            self.video = {"status": status, "recording_ref": recording_ref, **deepcopy(metadata)}
            if status in {"ready", "failed"}:
                self._emit("video." + status, None, self.video)

    def events_after(self, seq=0):
        with self.lock:
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("after_seq 必须为非负整数")
            return deepcopy(self.events[seq:])

    def archive(self):
        with self.lock:
            return {"snapshot": self.snapshot(), "config": deepcopy(self.config),
                    "seed": self.seed, "events": deepcopy(self.events),
                    "pending_command": deepcopy(self.pending["command"]) if self.pending else None,
                    "accepted_receipts": deepcopy(self.receipts)}

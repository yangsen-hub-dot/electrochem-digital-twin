"""Cross-domain provenance for one experiment state.

Chemical quantities remain authoritative in :mod:`state`; this ledger stores
workflow, device observations, entity relationships, certainty and lineage
without copying the chemical inventory into a second store.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UnifiedStateLedger:
    schema_version: str = "1.0.0"
    revision: int = 0
    workflow: dict = field(default_factory=lambda: {
        "status": "ready", "stage": "preparation", "completed_step_ids": [],
        "last_completed_step_id": None,
    })
    entities: dict[str, dict] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    device_state: dict = field(default_factory=lambda: {
        "status": "unobserved", "source": None, "step_id": None, "observation": None,
    })
    certainty: dict = field(default_factory=lambda: {
        "chemical": {"status": "modeled", "source": "initial_conditions"},
        "mechanical": {"status": "unknown", "source": None},
        "workflow": {"status": "protocol_defined", "source": "experiment_plan"},
        "measurement": {"status": "not_observed", "source": None},
    })
    chemical_model: dict = field(default_factory=lambda: {
        "model_id": None, "model_family": None, "state_schema_version": None,
    })
    lineage: dict = field(default_factory=dict)
    provenance: list[dict] = field(default_factory=list)

    def register_entity(self, entity_id: str, kind: str, **metadata) -> None:
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise ValueError("账本实体编号必须为非空字符串")
        self.entities.setdefault(entity_id, {
            "entity_id": entity_id, "kind": kind, "metadata": deepcopy(metadata),
        })

    def set_chemical_model(self, model_id: str, model_family: str,
                           state_schema_version: str = "1.0.0") -> None:
        self.chemical_model = {
            "model_id": model_id, "model_family": model_family,
            "state_schema_version": state_schema_version,
        }

    def add_initial_relation(self, subject: str, predicate: str, object_id) -> None:
        self._set_relation(subject, predicate, object_id, revision=0,
                           command_id=None, source="initial_experiment_state")

    def set_lineage(self, *, run_id: str, source_run_id: str,
                    checkpoint_step_id: str, checkpoint_revision: int,
                    candidate_id: str | None = None, branch_type: str = "local_repair",
                    repair_contract: dict | None = None) -> None:
        self.lineage = {
            "run_id": run_id, "source_run_id": source_run_id,
            "root_run_id": self.lineage.get("root_run_id") or source_run_id,
            "checkpoint_step_id": checkpoint_step_id,
            "checkpoint_revision": checkpoint_revision,
            "candidate_id": candidate_id, "branch_type": branch_type,
            "restored_from_confirmed_prefix": True,
            "physical_actions_replayed": False,
            "repair_contract": deepcopy(repair_contract) if repair_contract else None,
        }

    def _set_relation(self, subject: str, predicate: str, object_id,
                      *, revision: int, command_id: str | None, source: str) -> None:
        if isinstance(object_id, str):
            self.register_entity(object_id, "related_entity")
        elif isinstance(object_id, list):
            for related_id in object_id:
                if isinstance(related_id, str):
                    self.register_entity(related_id, "related_entity")
        for relation in self.relations:
            if relation.get("subject") == subject and relation.get("active"):
                relation["active"] = False
                relation["valid_until_revision"] = revision
        self.relations.append({
            "subject": subject, "predicate": predicate, "object": deepcopy(object_id),
            "valid_from_revision": revision, "valid_until_revision": None,
            "active": True, "source": source, "command_id": command_id,
        })

    @staticmethod
    def _stage_for(action: str | None) -> str:
        if action in {"load_tip", "aspirate", "dispense", "discard_tip", "pick_pipette", "park_pipette"}:
            return "liquid_preparation"
        if action in {"mix", "mix_pair"}:
            return "mixing"
        if action in {"pick_electrode", "place_electrode", "place_electrodes",
                      "pick_salt_bridge", "connect_salt_bridge"}:
            return "cell_assembly"
        if action in {"pick_lead", "connect_lead", "measure_voltage"}:
            return "measurement"
        return "other"

    def _record_success_relations(self, command: dict, receipt: dict, revision: int,
                                 source: str) -> None:
        action = command.get("action")
        command_id = command.get("command_id")
        target = command.get("target") or receipt.get("target")
        if action == "pick_pipette":
            pipette = "pipette:" + str(command.get("pipette_id"))
            self.register_entity(pipette, "tool", tool_id=command.get("pipette_id"))
            self._set_relation(pipette, "held_by", "robot_gripper", revision=revision,
                               command_id=command_id, source=source)
        elif action == "park_pipette":
            pipette = "pipette:" + str(command.get("pipette_id"))
            self.register_entity(pipette, "tool", tool_id=command.get("pipette_id"))
            self._set_relation(pipette, "supported_by", command.get("support_id") or target,
                               revision=revision, command_id=command_id, source=source)
        elif action == "pick_electrode":
            side = "right" if target == "right_half_cell" else "left"
            electrode = f"copper_electrode:{side}"
            self.register_entity(electrode, "electrode", side=side, material="Cu")
            self._set_relation(electrode, "held_by", "robot_gripper", revision=revision,
                               command_id=command_id, source=source)
        elif action == "place_electrode":
            side = "right" if target == "right_half_cell" else "left"
            electrode = f"copper_electrode:{side}"
            self.register_entity(electrode, "electrode", side=side, material="Cu")
            self._set_relation(electrode, "installed_in", target, revision=revision,
                               command_id=command_id, source=source)
        elif action == "pick_salt_bridge":
            self.register_entity("salt_bridge", "electrochemical_component")
            self._set_relation("salt_bridge", "held_by", "robot_gripper", revision=revision,
                               command_id=command_id, source=source)
        elif action == "connect_salt_bridge":
            self.register_entity("salt_bridge", "electrochemical_component")
            self._set_relation("salt_bridge", "connects",
                               command.get("targets") or ["left_half_cell", "right_half_cell"],
                               revision=revision, command_id=command_id, source=source)
        elif action == "pick_lead":
            side = "right" if target == "right_half_cell" else "left"
            lead = f"measurement_lead:{side}"
            self.register_entity(lead, "measurement_lead", side=side)
            self._set_relation(lead, "held_by", "robot_gripper", revision=revision,
                               command_id=command_id, source=source)
        elif action == "connect_lead":
            side = "right" if target == "right_half_cell" else "left"
            lead = f"measurement_lead:{side}"
            self.register_entity(lead, "measurement_lead", side=side)
            self._set_relation(lead, "connected_to", target, revision=revision,
                               command_id=command_id, source=source)
        elif action == "load_tip":
            pipette = "pipette:" + str(command.get("pipette_id"))
            tip = "pipette_tip:" + str(command_id or command.get("step_id"))
            self.register_entity(pipette, "tool", tool_id=command.get("pipette_id"))
            self.register_entity(tip, "disposable_tip", state_ref="/tip")
            self._set_relation(tip, "attached_to", pipette, revision=revision,
                               command_id=command_id, source=source)
            self.workflow["active_tip_entity_id"] = tip
        elif action == "discard_tip":
            tip = self.workflow.get("active_tip_entity_id") or "pipette_tip:unknown"
            self.register_entity(tip, "disposable_tip", state_ref="/tip")
            self._set_relation(tip, "disposed_to", "waste", revision=revision,
                               command_id=command_id, source=source)
            self.workflow["active_tip_entity_id"] = None

    def record_step(self, *, state_revision: int, step_id: str, action: str,
                    outcome: str, source: str, simulation_time_s: float | None,
                    command: dict | None = None, receipt: dict | None = None,
                    terminal: bool = False, partial: bool = False) -> None:
        if outcome not in {"succeeded", "failed", "partial"}:
            raise ValueError("账本步骤结果无效")
        if state_revision < self.revision or state_revision > self.revision + 1:
            raise ValueError("账本版本必须连续")
        command = deepcopy(command or {})
        receipt = deepcopy(receipt or {})
        if outcome == "succeeded" and state_revision != self.revision + 1:
            raise ValueError("成功步骤必须递增账本版本")

        self.revision = state_revision
        if outcome == "succeeded":
            completed = self.workflow.setdefault("completed_step_ids", [])
            if step_id not in completed:
                completed.append(step_id)
            self.workflow["last_completed_step_id"] = step_id
            self.workflow["stage"] = self._stage_for(action)
            self.workflow["status"] = "completed" if terminal else "running"
            self.certainty["chemical"] = {
                "status": "modeled", "source": receipt.get("actual_source") or source,
                "state_revision": state_revision,
            }
            self.certainty["workflow"] = {
                "status": "receipt_confirmed", "source": source,
                "state_revision": state_revision,
            }
            if action == "measure_voltage":
                self.certainty["measurement"] = {
                    "status": "virtual", "source": receipt.get("instrument_model", "simulated"),
                    "state_revision": state_revision,
                }
            self._record_success_relations(command, receipt, state_revision, source)
        else:
            self.workflow["status"] = "paused" if partial or outcome == "partial" else "failed"
            self.workflow["stage"] = self._stage_for(action)
            self.certainty["workflow"] = {
                "status": "partial" if partial or outcome == "partial" else "failed",
                "source": source, "state_revision": state_revision,
            }
            if partial or outcome == "partial":
                self.certainty["chemical"] = {
                    "status": "uncertain", "source": source,
                    "state_revision": state_revision,
                }

        observation = receipt.get("state_observation")
        if isinstance(observation, dict):
            self.device_state = {
                "status": "simulated_observation", "source": observation.get("source", source),
                "step_id": step_id, "state_revision": state_revision,
                "simulation_time_s": simulation_time_s,
                "observation": deepcopy(observation),
            }
            self.certainty["mechanical"] = {
                "status": "simulated_observation", "source": observation.get("source", source),
                "state_revision": state_revision,
            }
        elif outcome == "succeeded" and receipt.get("geometry_checks"):
            self.certainty["mechanical"] = {
                "status": "simulated_receipt", "source": source,
                "state_revision": state_revision,
            }

        evidence = receipt.get("evidence")
        record = {
            "sequence": len(self.provenance) + 1,
            "state_revision": state_revision, "step_id": step_id,
            "action": action, "outcome": outcome, "source": source,
            "occurred_at_utc": _utc_now(), "simulation_time_s": simulation_time_s,
            "model_elapsed_time_s": receipt.get("model_elapsed_time_s"),
            "chemical_model_transition_check": deepcopy(
                receipt.get("chemical_model_transition_check")
            ),
            "command_id": command.get("command_id") or receipt.get("command_id"),
            "evidence": deepcopy(evidence) if isinstance(evidence, dict) else {},
        }
        self.provenance.append(record)

    def snapshot(self) -> dict:
        return asdict(self)

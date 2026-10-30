"""Semantic action projection for the existing Matterix execution path.

This adapter deliberately stops before robot motion planning. The existing
Matterix runner remains responsible for scene geometry, paths, rendering, and
the simulation's operation-error realization.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import DeviceFeedback, DeviceInstruction


class MatterixAdapter:
    adapter_id = "matterix"

    def encode_instruction(self, instruction: DeviceInstruction) -> dict[str, Any]:
        """Create the semantic action envelope consumed at the Matterix boundary.

        This contains requested setpoints only. It does not copy sampled error
        values or imply that an action has been sent or executed.
        """
        payload = instruction.to_payload()
        return {
            "contract_version": instruction.contract_version,
            "backend": self.adapter_id,
            "run_id": instruction.run_id,
            "command_id": instruction.command_id,
            "step_id": instruction.step_id,
            "state_revision": instruction.state_revision,
            "action": instruction.action,
            "source": instruction.source,
            "target": instruction.target,
            "targets": list(instruction.targets),
            "requested_volume_l": instruction.requested_volume_l,
            "duration_s": instruction.duration_s,
            "component": instruction.component,
            "pipette_id": instruction.pipette_id,
            "parameters": payload["parameters"],
        }

    def normalize_feedback(
        self, instruction: DeviceInstruction, raw_feedback: Mapping[str, Any]
    ) -> DeviceFeedback:
        """Normalize a Matterix runner receipt without modifying session state."""
        if not isinstance(raw_feedback, Mapping):
            raise TypeError("Matterix feedback must be a mapping")
        if raw_feedback.get("executor") != "matterix":
            raise ValueError("receipt does not originate from the Matterix execution path")
        evidence = raw_feedback.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("Matterix receipt must include evidence")
        identity = {
            "run_id": raw_feedback.get("run_id"),
            "command_id": raw_feedback.get("command_id"),
            "step_id": raw_feedback.get("step_id"),
            "backend_id": self.adapter_id,
            "data_origin": "matterix_simulation",
            "status": raw_feedback.get("status"),
            "preconditions_verified": raw_feedback.get("preconditions_verified") is True,
            "evidence": dict(evidence),
            "reason": raw_feedback.get("reason"),
            "no_state_change": raw_feedback.get("no_state_change"),
            "state_uncertain": raw_feedback.get("status") == "partial",
            # The existing runner provides simulation_time_s, not a wall-clock
            # completion timestamp. Preserve it below as an observation rather
            # than inventing a timestamp during normalization.
        }
        excluded = {
            "run_id", "command_id", "step_id", "executor", "status",
            "preconditions_verified", "evidence", "reason", "no_state_change",
        }
        feedback = DeviceFeedback(
            **identity,
            observations={key: value for key, value in raw_feedback.items() if key not in excluded},
        )
        feedback.validate_for(instruction)
        return feedback

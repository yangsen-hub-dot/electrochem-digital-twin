"""Backend-neutral instruction and feedback contracts.

The instruction is built from the existing electrochem_sim.protocol.Action.
It carries requested setpoints only; simulation draws, visual geometry, and
vendor-specific wire formats belong to the selected backend adapter.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping


CONTRACT_VERSION = "1.0.0"
_HEX_ID = re.compile(r"^[a-f0-9]{32}$")
_SOURCES = {"matterix_simulation", "hardware_device"}
_STATUSES = {"succeeded", "failed", "partial"}


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return value


@dataclass(frozen=True)
class DeviceInstruction:
    """One requested operation from the shared experiment protocol.

    Volumes use SI litres and durations use seconds. ``requested_volume_l`` is
    a setpoint, never a claim about the amount actually delivered.
    """

    run_id: str
    command_id: str
    step_id: str
    sequence: int
    state_revision: int
    action: str
    source: str | None = None
    target: str | None = None
    targets: tuple[str, ...] = ()
    requested_volume_l: float | None = None
    duration_s: float | None = None
    component: str | None = None
    pipette_id: str | None = None
    contract_version: str = CONTRACT_VERSION
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "command_id"):
            if not isinstance(getattr(self, name), str) or not _HEX_ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be 32 lowercase hexadecimal characters")
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("step_id must be a non-empty string")
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("action must be a non-empty string")
        for name in ("sequence", "state_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract version: {self.contract_version}")
        if self.requested_volume_l is not None:
            volume = _finite_nonnegative(self.requested_volume_l, "requested_volume_l")
            if volume <= 0:
                raise ValueError("requested_volume_l must be greater than zero")
        if self.duration_s is not None:
            duration = _finite_nonnegative(self.duration_s, "duration_s")
            if duration <= 0:
                raise ValueError("duration_s must be greater than zero")
        if not isinstance(self.targets, tuple) or any(not isinstance(x, str) or not x for x in self.targets):
            raise ValueError("targets must be a tuple of non-empty strings")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("parameters must be a mapping")

    @classmethod
    def from_action(
        cls,
        action: Any,
        *,
        run_id: str,
        command_id: str,
        sequence: int,
        state_revision: int,
    ) -> "DeviceInstruction":
        """Project an existing protocol Action without re-planning it."""
        required = ("step_id", "kind", "source", "target", "targets", "volume_l",
                    "component", "pipette_id", "duration_s")
        missing = [name for name in required if not hasattr(action, name)]
        if missing:
            raise TypeError("action is not an electrochem_sim.protocol.Action; missing: " + ", ".join(missing))
        return cls(
            run_id=run_id,
            command_id=command_id,
            step_id=action.step_id,
            sequence=sequence,
            state_revision=state_revision,
            action=action.kind,
            source=action.source,
            target=action.target,
            targets=tuple(action.targets),
            requested_volume_l=action.volume_l,
            duration_s=action.duration_s,
            component=action.component,
            pipette_id=action.pipette_id,
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["targets"] = list(self.targets)
        payload["parameters"] = dict(self.parameters)
        payload["units"] = {"requested_volume_l": "L", "duration_s": "s"}
        return payload


@dataclass(frozen=True)
class DeviceFeedback:
    """Normalized result returned by either simulator or physical hardware."""

    run_id: str
    command_id: str
    step_id: str
    backend_id: str
    data_origin: str
    status: str
    preconditions_verified: bool
    observations: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    no_state_change: bool | None = None
    state_uncertain: bool = False
    completed_at_utc: str | None = None
    elapsed_s: float | None = None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("run_id", "command_id"):
            if not isinstance(getattr(self, name), str) or not _HEX_ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be 32 lowercase hexadecimal characters")
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("step_id must be a non-empty string")
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        if self.data_origin not in _SOURCES:
            raise ValueError(f"data_origin must be one of {sorted(_SOURCES)}")
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {sorted(_STATUSES)}")
        if not isinstance(self.preconditions_verified, bool) or not isinstance(self.state_uncertain, bool):
            raise ValueError("preconditions_verified and state_uncertain must be booleans")
        if self.status == "succeeded":
            if not self.evidence:
                raise ValueError("successful feedback must include execution evidence")
            if not self.preconditions_verified:
                raise ValueError("successful feedback must confirm preconditions")
        elif not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("failed or partial feedback must explain the outcome")
        if self.status == "failed" and self.no_state_change is not True:
            raise ValueError("failed feedback must establish that no state change occurred")
        if self.status == "partial" and not self.state_uncertain:
            raise ValueError("partial feedback must mark physical state as uncertain")
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract version: {self.contract_version}")
        if not isinstance(self.observations, Mapping) or not isinstance(self.evidence, Mapping):
            raise ValueError("observations and evidence must be mappings")
        if self.elapsed_s is not None:
            _finite_nonnegative(self.elapsed_s, "elapsed_s")
        if self.completed_at_utc is not None:
            try:
                parsed = datetime.fromisoformat(self.completed_at_utc.replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise ValueError("completed_at_utc must be an ISO-8601 timestamp") from exc
            if parsed.tzinfo is None:
                raise ValueError("completed_at_utc must include a timezone")

    def validate_for(self, instruction: DeviceInstruction) -> None:
        """Reject stale, duplicated-to-another-step, or misrouted responses."""
        for name in ("run_id", "command_id", "step_id"):
            if getattr(self, name) != getattr(instruction, name):
                raise ValueError(f"feedback {name} does not match the instruction")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

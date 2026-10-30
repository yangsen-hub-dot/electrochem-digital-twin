"""Small plugin registry for experiment execution backends."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .contracts import DeviceFeedback, DeviceInstruction


class UnknownAdapterError(LookupError):
    pass


@runtime_checkable
class DeviceAdapter(Protocol):
    """Translate the shared operation contract to/from one execution backend.

    Implementations own vendor SDK/network details. They do not own the
    experiment protocol, chemistry ledger, or error sampling.
    """

    adapter_id: str

    def encode_instruction(self, instruction: DeviceInstruction) -> Mapping[str, Any]:
        """Return the backend-specific instruction payload."""
        ...

    def normalize_feedback(
        self, instruction: DeviceInstruction, raw_feedback: Mapping[str, Any]
    ) -> DeviceFeedback:
        """Convert backend feedback to the shared receipt contract."""
        ...


class AdapterRegistry:
    """Explicit backend selection; an unknown backend never silently falls back."""

    def __init__(self) -> None:
        self._adapters: dict[str, DeviceAdapter] = {}

    def register(self, adapter: DeviceAdapter) -> None:
        adapter_id = getattr(adapter, "adapter_id", None)
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            raise ValueError("adapter_id must be a non-empty string")
        if adapter_id in self._adapters:
            raise ValueError(f"adapter already registered: {adapter_id}")
        if not isinstance(adapter, DeviceAdapter):
            raise TypeError("adapter does not implement the device adapter contract")
        self._adapters[adapter_id] = adapter

    def resolve(self, adapter_id: str) -> DeviceAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise UnknownAdapterError(
                f"No adapter is configured for {adapter_id!r}; refusing implicit backend fallback"
            ) from exc

    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

"""Reusable protocol boundary for Matterix and future lab hardware backends."""

from .contracts import DeviceFeedback, DeviceInstruction
from .matterix_adapter import MatterixAdapter
from .registry import AdapterRegistry, DeviceAdapter, UnknownAdapterError

__all__ = [
    "AdapterRegistry",
    "DeviceAdapter",
    "DeviceFeedback",
    "DeviceInstruction",
    "MatterixAdapter",
    "UnknownAdapterError",
]

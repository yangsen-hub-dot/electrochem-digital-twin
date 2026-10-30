"""Independent checks for workflow state transitions."""
from __future__ import annotations

from .conservation import check_conservation


def check_transition(initial_inventory, before, after, locked_containers):
    for name in locked_containers:
        if before.containers[name] != after.containers[name]:
            raise ValueError("post-electrolysis inventory is unresolved and cannot be changed")
    for liquid in [*after.containers.values(), after.tip.liquid, after.waste]:
        liquid.validate()
    result = check_conservation(initial_inventory, after.inventory())
    if not result.get("passed"):
        raise ValueError("inventory conservation failed")
    return result

"""Small shared contracts for repair classification and strategy output."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RepairTarget:
    step_id: str
    family: str
    issues: tuple[dict, ...]


def blocked(run_id, reason, status="blocked"):
    return {"run_id": run_id, "eligible": False, "status": status, "reason": reason}

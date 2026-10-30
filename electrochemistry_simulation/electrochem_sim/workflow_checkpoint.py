"""Restore an exact numerical workflow snapshot without replaying prior actions."""
from __future__ import annotations

from copy import deepcopy
from .state import ExperimentState, LiquidState, TipState
from .state_ledger import UnifiedStateLedger


def _liquid(row):
    keys = {"identifier", "volume_l", "amounts_mol", "mixed", "mixing_uniformity", "capacity_l",
            "mixing_components", "mixing_reference_spread_m"}
    liquid = LiquidState(**{k: deepcopy(v) for k, v in row.items() if k in keys})
    liquid.validate()
    return liquid


def restore_snapshot(snapshot):
    row = deepcopy(snapshot)
    tip = row["tip"]
    state = ExperimentState(containers={k: _liquid(v) for k, v in row["containers"].items()},
        temperature_k=row["temperature_k"], tip=TipState(loaded=tip["loaded"], liquid=_liquid(tip["liquid"])),
        waste=_liquid(row["waste"]), cell_setup=row["cell_setup"], completed_steps=row["completed_steps"],
        chemical_model_id=row["chemical_model_id"], chemical_model_state=row["chemical_model_state"],
        ledger=UnifiedStateLedger(**row["state_ledger"]))
    if state.snapshot() != snapshot:
        raise ValueError("checkpoint cannot be restored exactly")
    return state

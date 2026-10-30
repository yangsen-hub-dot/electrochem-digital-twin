"""Replaceable chemical-model contract and registry.

The current registered implementation is equilibrium-only. New kinetic models
can keep their time-dependent internal state here and receive each accepted
chemical transition, without binding the protocol or state ledger to one
reaction. A kinetic provider still needs a model-specific balance validator
before it can be used for scientific claims.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol


class ChemicalModel(Protocol):
    model_family: str
    state_schema_version: str

    def initialize_state(self, config: dict, state) -> dict: ...

    def advance(self, model_state: dict, state, *, step: dict, event: dict,
                elapsed_time_s: float, config: dict) -> dict: ...

    def validate_transition(self, before_state, after_state, before_model_state: dict,
                            after_model_state: dict, *, config: dict) -> dict: ...

    def observe(self, model_state: dict, state, cell: dict,
                species_definitions: dict) -> dict: ...


@dataclass(frozen=True)
class RegisteredChemicalModel:
    model_id: str
    provider: ChemicalModel

    @property
    def model_family(self) -> str:
        return self.provider.model_family

    @property
    def state_schema_version(self) -> str:
        return self.provider.state_schema_version

    def initialize_state(self, config: dict, state) -> dict:
        return self.provider.initialize_state(config, state)

    def advance(self, model_state: dict, state, *, step: dict, event: dict,
                elapsed_time_s: float, config: dict) -> dict:
        return self.provider.advance(model_state, state, step=step, event=event,
                                     elapsed_time_s=elapsed_time_s, config=config)

    def validate_transition(self, before_state, after_state, before_model_state: dict,
                            after_model_state: dict, *, config: dict) -> dict:
        return self.provider.validate_transition(before_state, after_state,
                                                 before_model_state, after_model_state,
                                                 config=config)

    def observe(self, model_state: dict, state, cell: dict,
                species_definitions: dict) -> dict:
        return self.provider.observe(model_state, state, cell, species_definitions)


class ConcentrationCellEquilibriumModel:
    model_family = "electrochemical_equilibrium"
    state_schema_version = "1.0.0"

    def initialize_state(self, config: dict, state) -> dict:
        return {"schema_version": self.state_schema_version, "elapsed_time_s": 0.0,
                "dynamics_enabled": False}

    def advance(self, model_state: dict, state, *, step: dict, event: dict,
                elapsed_time_s: float, config: dict) -> dict:
        updated = deepcopy(model_state)
        updated["elapsed_time_s"] = float(updated.get("elapsed_time_s", 0.0)) + max(0.0, elapsed_time_s)
        return updated

    def validate_transition(self, before_state, after_state, before_model_state: dict,
                            after_model_state: dict, *, config: dict) -> dict:
        elapsed = after_model_state.get("elapsed_time_s")
        passed = (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                  and elapsed >= 0)
        return {"passed": passed, "basis": "equilibrium_model_state_schema",
                "model_family": self.model_family}

    def observe(self, model_state: dict, state, cell: dict,
                species_definitions: dict) -> dict:
        from .electrochemistry import _predict_concentration_cell_nernst
        return _predict_concentration_cell_nernst(state, cell, species_definitions)


class UnavailableChemicalModel:
    """Keep legacy invalid-result reporting for an unknown configured ID."""
    model_family = "unregistered"
    state_schema_version = "unknown"

    def initialize_state(self, config: dict, state) -> dict:
        return {"schema_version": "unknown", "status": "unregistered"}

    def advance(self, model_state: dict, state, *, step: dict, event: dict,
                elapsed_time_s: float, config: dict) -> dict:
        return deepcopy(model_state)

    def validate_transition(self, before_state, after_state, before_model_state: dict,
                            after_model_state: dict, *, config: dict) -> dict:
        return {"passed": True, "basis": "unregistered_model_no_state_update"}

    def observe(self, model_state: dict, state, cell: dict,
                species_definitions: dict) -> dict:
        from .electrochemistry import _predict_concentration_cell_nernst
        return _predict_concentration_cell_nernst(state, cell, species_definitions)


_REGISTRY: dict[str, RegisteredChemicalModel] = {}


def register_chemical_model(model_id: str, provider: ChemicalModel, *, replace: bool = False) -> None:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("化学模型编号必须为非空字符串")
    if model_id in _REGISTRY and not replace:
        raise ValueError(f"化学模型已注册：{model_id}")
    for method in ("initialize_state", "advance", "validate_transition", "observe"):
        if not callable(getattr(provider, method, None)):
            raise TypeError(f"化学模型缺少接口方法：{method}")
    _REGISTRY[model_id] = RegisteredChemicalModel(model_id, provider)


def get_chemical_model(model_id: str) -> RegisteredChemicalModel | None:
    return _REGISTRY.get(model_id)


def resolve_chemical_model(config: dict, *, strict: bool = True) -> RegisteredChemicalModel:
    cell = config.get("cell", {})
    model_config = config.get("chemical_model", {})
    model_id = (model_config.get("model_id") or cell.get("chemical_model_id")
                or cell.get("equilibrium_model") or "ideal_concentration_cell_nernst")
    model = get_chemical_model(model_id)
    if model is None:
        if strict:
            raise ValueError(f"未注册化学模型：{model_id}")
        return RegisteredChemicalModel(model_id, UnavailableChemicalModel())
    return model


def registered_chemical_models() -> list[dict]:
    return [{"model_id": model.model_id, "model_family": model.model_family,
             "state_schema_version": model.state_schema_version}
            for model in _REGISTRY.values()]


_equilibrium = ConcentrationCellEquilibriumModel()
for _model_id in ("ideal_concentration_cell_nernst",
                  "reaction_quotient_concentration_cell_nernst"):
    register_chemical_model(_model_id, _equilibrium)

from .hmf_fdca_model import HmfFdcaEndpointModel, MODEL_ID as _HMF_MODEL_ID
register_chemical_model(_HMF_MODEL_ID, HmfFdcaEndpointModel())

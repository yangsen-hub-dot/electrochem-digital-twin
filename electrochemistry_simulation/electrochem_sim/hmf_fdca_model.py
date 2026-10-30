"""Endpoint-only literature model; never mutates the authoritative inventory."""

from __future__ import annotations

import math
from copy import deepcopy

FARADAY_C_PER_MOL = 96485.33212
POINTS = ((25.0, .96), (31.25, .96), (37.5, .84), (43.75, .74), (50.0, .52))
SOURCE = "https://doi.org/10.1002/cssc.202401190"
MODEL_ID = "hmf_tempo_fdca_endpoint_dargo2025_v1"


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _near(value, expected):
    # Numerical equality only, not an experimentally validated tolerance.
    return math.isclose(value, expected, rel_tol=1e-8, abs_tol=1e-12)


def _interpolate(concentration_mm):
    if not POINTS[0][0] <= concentration_mm <= POINTS[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(POINTS, POINTS[1:]):
        if concentration_mm <= x1:
            return y0 + (y1 - y0) * (concentration_mm - x0) / (x1 - x0)
    return POINTS[-1][1]


class HmfFdcaEndpointModel:
    model_family = "electrocatalytic_oxidation_endpoint"
    state_schema_version = "1.0.0"

    def initialize_state(self, config, state):
        return {"schema_version": self.state_schema_version, "elapsed_time_s": 0.0}

    def advance(self, model_state, state, *, step, event, elapsed_time_s, config):
        elapsed = _number(elapsed_time_s, "elapsed_time_s")
        if elapsed < 0:
            raise ValueError("elapsed_time_s must be nonnegative")
        updated = deepcopy(model_state)
        updated["elapsed_time_s"] += elapsed
        return updated

    def validate_transition(self, before_state, after_state, before_model_state, after_model_state, *, config):
        before = before_model_state.get("elapsed_time_s")
        after = after_model_state.get("elapsed_time_s")
        passed = (isinstance(before, (int, float)) and isinstance(after, (int, float))
                  and math.isfinite(before) and math.isfinite(after) and after >= before)
        return {"passed": passed, "basis": "endpoint_model_does_not_mutate_chemical_inventory"}

    def observe(self, model_state, state, cell, species_definitions):
        """Predict from an authoritative, already-realized liquid state and recorded conditions.

        No electrolysis action, species conversion, HPLC or hardware execution is implied.
        """
        result = {"valid": False, "model_id": MODEL_ID, "source": SOURCE,
                  "data_origin": "literature_estimate", "yield_fraction": None,
                  "fdca_amount_mol": None, "hplc_reading": None,
                  "uncertainty": "not_quantified", "inventory_updated": False}
        try:
            liquid = state.containers[cell["reaction_container"]]
            liquid.validate()
            volume = _number(liquid.volume_l, "volume_l")
            if volume <= 0:
                raise ValueError("reaction volume must be positive")
            amounts = liquid.amounts_mol
            if not all(name in amounts for name in ("HMF", "TEMPO", "KOH")):
                raise ValueError("HMF, TEMPO and KOH must be present in inventory")
            hmf = _number(amounts["HMF"], "HMF")
            tempo = _number(amounts["TEMPO"], "TEMPO")
            koh = _number(amounts["KOH"], "KOH")
            if hmf <= 0 or tempo < 0 or koh < 0:
                raise ValueError("invalid reactant amounts")
            current = _number(cell["current_a"], "current_a")
            duration = _number(cell["duration_s"], "duration_s")
            if current <= 0 or duration <= 0:
                raise ValueError("current and duration must be positive")
            charge = current * duration
            concentration_mm = hmf / volume * 1000
            result.update({"input_hmf_mol": hmf, "volume_l": volume,
                           "hmf_concentration_mm": concentration_mm,
                           "koh_concentration_m": koh / volume,
                           "tempo_fraction_of_hmf": tempo / hmf,
                           "charge_c": charge,
                           "ideal_six_electron_charge_c": 6 * FARADAY_C_PER_MOL * hmf,
                           "charge_per_hmf_f": charge / (FARADAY_C_PER_MOL * hmf)})
            reasons = []
            for condition, expected in (("anode", "graphite"), ("cathode", "stainless_steel"),
                                        ("electrolyte", "aqueous_koh"), ("mode", "constant_current"),
                                        ("cell_configuration", "paper_single_vial")):
                if cell.get(condition) != expected:
                    reasons.append(f"{condition}: unsupported or missing")
            for name, value, expected in (("volume_l", volume, .005), ("koh_m", koh / volume, .1),
                                          ("tempo_mol_fraction", tempo / hmf, .05),
                                          ("current_a", current, .01),
                                          ("charge_per_hmf_f", result["charge_per_hmf_f"], 6.0)):
                if not _near(value, expected):
                    reasons.append(f"{name}: differs from literature condition")
            yield_fraction = _interpolate(concentration_mm)
            if yield_fraction is None:
                reasons.append("hmf_concentration: outside literature range")
            if reasons:
                result["reason"] = "; ".join(reasons)
                return result
            result.update({"valid": True, "reason": None, "yield_fraction": yield_fraction,
                           "fdca_amount_mol": hmf * yield_fraction,
                           "fdca_concentration_m": hmf * yield_fraction / volume,
                           "method": "piecewise_linear_interpolation",
                           "limitations": ["endpoint only", "interpolated values not measured",
                                           "no kinetics or validated prediction interval",
                                           "not calibrated for actual hardware"]})
            return result
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            result["reason"] = str(exc)
            return result

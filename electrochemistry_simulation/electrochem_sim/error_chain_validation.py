"""Local numerical audit of the existing transfer workflow; no hardware or cloud calls.

Diagnostic copies suppress individual error mechanisms without changing the
production profile. All volumes are in µL in reports and L in engine states.
Ensemble intervals describe model draws, not measured confidence intervals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from copy import deepcopy
from pathlib import Path

from .condition_input import build_condition_config
from .simulator import load_config, run_experiment


MODES = ("ideal", "systematic_only", "repeatability_only", "residual_only", "mixing_only", "all")
CONDITIONS = {
    "left_concentration_m": 0.01, "right_concentration_m": 0.1,
    "left_volume_ml": 1.5, "right_volume_ml": 1.5, "temperature_c": 25.0,
    "salt_bridge_type": "KNO3", "salt_bridge_concentration_m": 0.1,
    "activity_correction_enabled": False, "liquid_junction_enabled": False,
    "errors_enabled": True,
    "operation_error_levels": {"pipetting": "low", "tip_carryover": "low", "mixing": "low"},
}


def scenario_config(base: dict, mode: str) -> dict:
    """Keep the generated doses/tip policy, zeroing only isolated diagnostic terms."""
    if mode not in MODES:
        raise ValueError(f"Unknown diagnostic mode: {mode}")
    config = build_condition_config(base, CONDITIONS)
    errors = config["error_model"]
    errors["enabled"] = mode != "ideal"
    factors = errors["factors"]
    if mode not in {"residual_only", "all"}:
        factors["tip_carryover"] = {
            "model": "bounded_residual_volume", "level": "diagnostic_zero",
            "parameters": {"residual_volume_ul": 0.0},
            "parameter_source": "diagnostic_suppression_not_calibration",
        }
    if mode not in {"mixing_only", "all"}:
        factors["mixing"] = {
            "model": "bounded_efficiency_per_action", "level": "diagnostic_ideal",
            "parameters": {"efficiency_per_action": 1.0},
            "parameter_source": "diagnostic_suppression_not_calibration",
        }
    parameters = factors["pipetting"]["parameters"]
    for profile in parameters["pipette_profiles"]:
        for anchor in profile["manufacturer_specification_anchors"]:
            if mode not in {"systematic_only", "all"}:
                anchor["systematic_error_limit_ul"] = 0.0
            if mode not in {"repeatability_only", "all"}:
                anchor["repeatability_sd_limit_ul"] = 0.0
    if mode != "all":
        factors["pipetting"]["parameter_source"] = "diagnostic_copy_of_manufacturer_reference"
    return config


def _close(actual: float, expected: float, label: str, abs_tol: float = 1e-10) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=abs_tol):
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def validate_result(result: dict) -> None:
    """Cross-check event totals, source/target/waste accounting and voltage inputs."""
    if result["status"] != "completed":
        raise AssertionError(str(result["failure"]))
    if not result["conservation"]["passed"]:
        raise AssertionError("Final conservation failed")
    events = [step["event"] for step in result["steps"]]
    for step in result["steps"]:
        if not step["conservation"]["passed"]:
            raise AssertionError("Step conservation failed")
        event = step["event"]
        if event["action"] == "aspirate":
            _close(event["simulated_volume_ul"], event["requested_volume_ul"]
                   + event["systematic_offset_ul"] + event["random_offset_ul"], "aspiration")
    final = result["final_state"]
    for cell_id in ("left_half_cell", "right_half_cell"):
        cell = final["containers"][cell_id]
        delivered = [event for event in events if event["action"] == "dispense" and event["target"] == cell_id]
        volume = math.fsum(event["simulated_volume_ul"] for event in delivered)
        _close(cell["volume_l"] * 1e6, volume, "delivered volume")
        for species, amount in cell["amounts_mol"].items():
            _close(amount, math.fsum(event["transferred_amounts_mol"].get(species, 0.0)
                                    for event in delivered), "delivered solute", 1e-15)
            _close(cell["concentrations_m"][species], amount / cell["volume_l"], "concentration")
            _close(math.fsum(part["amounts_mol"].get(species, 0.0)
                             for part in cell["mixing_components"]), amount, "mixing parcels", 1e-15)
    for source in ("copper_stock", "water"):
        removed = math.fsum(event["simulated_volume_ul"] for event in events
                            if event["action"] == "aspirate" and event["source"] == source)
        _close((result["initial_state"]["containers"][source]["volume_l"]
                - final["containers"][source]["volume_l"]) * 1e6, removed, "source inventory")
    discarded = math.fsum(event["simulated_volume_ul"] or 0.0 for event in events
                          if event["action"] == "discard_tip")
    _close(final["waste"]["volume_l"] * 1e6, discarded, "discarded residual")
    if final["tip"]["loaded"] or final["tip"]["liquid"]["volume_l"] != 0.0:
        raise AssertionError("Workflow must finish with an empty discarded tip")
    left = final["containers"]["left_half_cell"]["concentrations_m"]["Cu"]
    right = final["containers"]["right_half_cell"]["concentrations_m"]["Cu"]
    # Independent direct formula rather than calling the production helper.
    expected_mv = 8.314462618 * final["temperature_k"] / (2 * 96485.33212) * math.log(right / left) * 1000
    prediction = result["prediction"]
    components = prediction["voltage_components_mv"]
    _close(components["ideal_nernst"], expected_mv, "independent Nernst formula")
    _close(prediction["voltage_mv"], math.fsum(components[key] or 0.0 for key in
           ("ideal_nernst", "activity_correction", "liquid_junction_potential")), "voltage components")
    _close(right, 0.1, "undiluted right stock")
    if prediction["activity_correction"]["enabled"]:
        ionic = prediction["activity_correction"]["ionic_strength_molal_by_side"]
        _close(ionic["left"], 4 * left, "left ionic strength")
        _close(ionic["right"], 4 * right, "right ionic strength")
    mix = next(step for step in result["steps"] if step["event"]["action"] == "mix_pair")
    for cell_id, event in mix["event"]["cells"].items():
        before = mix["before"]["containers"][cell_id]
        after = mix["after"]["containers"][cell_id]
        _close(before["volume_l"], after["volume_l"], "mixing volume", 1e-15)
        mean = before["amounts_mol"]["Cu"] / before["volume_l"]
        for old, new in zip(before["mixing_components"], after["mixing_components"]):
            initial_c = old["amounts_mol"].get("Cu", 0.0) / old["volume_l"]
            actual_c = new["amounts_mol"].get("Cu", 0.0) / new["volume_l"]
            _close(actual_c, mean + (initial_c - mean) * (1 - event["mixing_efficiency"]), "mixing concentration")


def summarize(result: dict) -> dict:
    final = result["final_state"]
    prediction = result["prediction"]
    summary = {"seed": result["seed"], "run_id": result["run_id"],
               "voltage_mv": prediction["voltage_mv"],
               "local_voltage_range_mv": prediction["local_voltage_range_mv"],
               "waste_ul": final["waste"]["volume_l"] * 1e6}
    local_voltage = prediction["local_voltage_range_mv"]
    summary["local_voltage_width_mv"] = (local_voltage["maximum_mv"] - local_voltage["minimum_mv"]
                                         if local_voltage else None)
    for side in ("left", "right"):
        cell = final["containers"][f"{side}_half_cell"]
        summary[f"{side}_volume_ul"] = cell["volume_l"] * 1e6
        summary[f"{side}_concentration_m"] = cell["concentrations_m"]["Cu"]
        summary[f"{side}_local_range_m"] = prediction["local_concentration_ranges_m"][side]
        local = summary[f"{side}_local_range_m"]
        summary[f"{side}_local_concentration_width_m"] = local["maximum"] - local["minimum"]
    summary["transfers"] = [{key: event.get(key) for key in (
        "step_id", "action", "source", "target", "requested_volume_ul", "simulated_volume_ul",
        "systematic_offset_ul", "random_offset_ul", "retained_tip_volume_ul")}
        for step in result["steps"] for event in [step["event"]]
        if event["action"] in {"aspirate", "dispense", "discard_tip"}]
    return summary


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lo = math.floor(index)
    hi = math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def run_audit(base: dict, repeats: int = 200, first_seed: int = 20000) -> dict:
    if repeats < 2:
        raise ValueError("At least two ensemble runs are required")
    configs = {mode: scenario_config(base, mode) for mode in MODES}
    plan = configs["ideal"]["protocol"]
    if any(config["protocol"] != plan for config in configs.values()):
        raise AssertionError("Diagnostic cases changed the workflow")
    exemplars = {}
    ensembles = {}
    metrics = ("left_volume_ul", "right_volume_ul", "left_concentration_m", "right_concentration_m", "voltage_mv", "waste_ul",
               "left_local_concentration_width_m", "right_local_concentration_width_m", "local_voltage_width_mv")
    for mode, config in configs.items():
        result = run_experiment(config, first_seed)
        validate_result(result)
        exemplars[mode] = result
        samples = []
        for seed in range(first_seed, first_seed + repeats):
            draw = result if seed == first_seed else run_experiment(config, seed)
            validate_result(draw)
            sample = summarize(draw)
            samples.append({key: sample[key] for key in ("seed", *metrics)})
        ensembles[mode] = {"count": repeats, "samples": samples, "statistics": {
            key: {"mean": statistics.mean(row[key] for row in samples),
                  "sample_sd": statistics.stdev(row[key] for row in samples),
                  "model_draw_p025": _quantile([row[key] for row in samples], .025),
                  "model_draw_p975": _quantile([row[key] for row in samples], .975)}
            for key in metrics}}
    corrections = {}
    reference_events = [step["event"] for step in exemplars["all"]["steps"]]
    for label, activity, junction in (("ideal", False, False), ("activity", True, False), ("activity_and_junction", True, True)):
        config = deepcopy(configs["all"])
        config["cell"]["activity_correction"]["enabled"] = activity
        config["cell"]["liquid_junction_potential"]["enabled"] = junction
        result = run_experiment(config, first_seed)
        validate_result(result)
        if [step["event"] for step in result["steps"]] != reference_events:
            raise AssertionError("Corrections unexpectedly changed realized actions")
        corrections[label] = result["prediction"]
    return {
        "schema_version": "1.0", "conditions": CONDITIONS,
        "automatic_compensation": False, "cloud_actions_submitted": False,
        "profile_id": configs["all"]["error_model"]["profile_set_id"],
        "resolved_base_error_model_sha256": hashlib.sha256(json.dumps(base["error_model"], sort_keys=True).encode()).hexdigest(),
        "random_comparison_policy": "Independent scenario distributions; equal seed numbers do not establish paired causal contributions. Corrections are checked on identical event realizations.",
        "interval_interpretation": "Empirical 2.5/97.5 percentiles of assumed model draws; not calibration, confidence bounds, or measured device precision.",
        "mixing_interpretation": "Mean voltage uses bulk concentration; local voltage is a conceptual endpoint scenario range, not a statistical interval or electrode-position prediction.",
        "unresolved_evidence": "Manufacturer delivered-volume specifications and additional residual prior may overlap; their independence and real-world additive interpretation remain unverified.",
        "exemplars": exemplars,
        "single_seed_summary": {mode: summarize(result) for mode, result in exemplars.items()},
        "ensembles": ensembles, "electrochemical_corrections_same_actions": corrections,
        "validation_passed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=200)
    parser.add_argument("--first-seed", type=int, default=20000)
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "config" / "copper_concentration_cell.json"
    report = run_audit(load_config(path), args.repeats, args.first_seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"validation_passed": report["validation_passed"], "runs_checked": len(MODES) * args.repeats + 3,
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

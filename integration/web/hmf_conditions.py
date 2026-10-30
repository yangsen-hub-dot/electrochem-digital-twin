"""Build an isolated numerical protocol from a small set of user conditions."""
from __future__ import annotations

import math
from copy import deepcopy
from electrochem_sim.hmf_fdca_model import FARADAY_C_PER_MOL

DEFAULTS = {"hmf_mm": 31.25, "tempo_percent": 5.0, "koh_m": 0.1, "current_ma": 10.0, "pipette_bias_percent": 0.0}
RANGES = {"hmf_mm": (1.0, 100.0), "tempo_percent": (1.0, 10.0),
          "koh_m": (0.05, 0.2), "current_ma": (1.0, 20.0), "pipette_bias_percent": (-5.0, 5.0)}


def build_conditions(base_config, submitted):
    if not isinstance(submitted, dict) or set(submitted) - set(DEFAULTS):
        raise ValueError("未知实验条件")
    values = {}
    for name, default in DEFAULTS.items():
        value = submitted.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name}: 必须是有限数字")
        low, high = RANGES[name]
        if not low <= value <= high:
            raise ValueError(f"{name}: 超出允许范围 [{low}, {high}]")
        values[name] = float(value)
    # Fixed virtual stock transfer volumes: 0.5 mL + 0.5 mL + 4 mL.
    # The requested final concentrations determine virtual stock concentrations.
    config = deepcopy(base_config)
    bias = values["pipette_bias_percent"] / 100.0
    config["error_model"] = {"enabled": bool(bias), "source_proportional_bias": {"hmf_stock": bias}}
    hmf_m = values["hmf_mm"] / 1000.0
    config["containers"]["hmf_stock"]["concentrations_m"]["HMF"] = hmf_m * 10
    config["containers"]["tempo_stock"]["concentrations_m"]["TEMPO"] = hmf_m * values["tempo_percent"] / 100 * 10
    config["containers"]["koh_stock"]["concentrations_m"]["KOH"] = values["koh_m"] * 1.25
    electrolysis = [step for step in config["workflow"] if step["action"] == "electrolyze"]
    if len(electrolysis) != 1:
        raise ValueError("示例流程必须包含一次电解")
    step = electrolysis[0]
    step["current_a"] = values["current_ma"] / 1000.0
    initial_hmf_mol = hmf_m * .005
    step["duration_s"] = 6 * FARADAY_C_PER_MOL * initial_hmf_mol / step["current_a"]
    return config, values

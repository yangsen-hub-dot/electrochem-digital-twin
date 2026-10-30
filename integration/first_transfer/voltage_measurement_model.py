"""Reference-bounded voltage-measurement sensitivity, separate from chemistry.

This module reports a conditional manufacturer-specification envelope. It does
not draw a fictional meter reading or claim a calibrated error distribution.
"""

import math


MODEL_ID = "gamry_reference_620_specification_envelope_v1"
SOURCE_URL = "https://www.gamry.com/assets/988-00086-Reference-620-Operators-Manual.pdf"
ZERO_OFFSET_BOUND_MV = 1.0
RELATIVE_GAIN_BOUND = 0.002


def specification_envelope(predicted_cell_mv: float) -> dict:
    """Return a two-sided conditional accuracy bound around a model prediction.

    Gamry Reference 620's published zero-offset and gain limits are combined
    as a worst-case bound: |b| + |g|*|E|. These are specification limits, not
    sampled errors, standard deviations, or project calibration data.
    """
    if isinstance(predicted_cell_mv, bool):
        raise ValueError("预测电压必须是有限数字")
    try:
        voltage = float(predicted_cell_mv)
    except (TypeError, ValueError) as exc:
        raise ValueError("预测电压必须是有限数字") from exc
    if not math.isfinite(voltage):
        raise ValueError("预测电压必须是有限数字")

    gain_bound_mv = abs(voltage) * RELATIVE_GAIN_BOUND
    total_bound_mv = ZERO_OFFSET_BOUND_MV + gain_bound_mv
    return {
        "model_id": MODEL_ID,
        "instrument_reference": "Gamry Reference 620",
        "parameter_source": "manufacturer_specification_limit_sensitivity_scenario",
        "source_url": SOURCE_URL,
        "prediction_mv": voltage,
        "zero_offset_bound_mv": ZERO_OFFSET_BOUND_MV,
        "relative_gain_bound": RELATIVE_GAIN_BOUND,
        "gain_bound_mv": gain_bound_mv,
        "total_bound_mv": total_bound_mv,
        "lower_bound_mv": voltage - total_bound_mv,
        "upper_bound_mv": voltage + total_bound_mv,
        "single_virtual_reading_mv": voltage,
        "single_reading_is_ideal": True,
        "repeatability_noise_status": "not_modeled_no_condition_matched_data",
        "resolution_status": "not_modeled_no_selected_voltage_range",
        "distribution": None,
        "data_origin": "simulated_reference_bound",
        "limitations": [
            "该区间是按 Gamry Reference 620 厂家最大误差限构造的条件性敏感性范围，不是本项目实测校准或概率区间",
            "厂家规格的适用条件未在当前仿真中逐项复现；不可据此声称当前系统达到该准确度",
            "当前单次虚拟读数仍等于电化学模型预测值；手册给出量程分辨率，但仿真未选择对应量程/后级增益，因此未建模取整；重复读数波动也尚未纳入",
        ],
    }

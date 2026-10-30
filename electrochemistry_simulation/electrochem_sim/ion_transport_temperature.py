"""有来源边界的无限稀释离子电导温度参考估算。"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .ion_transport import FARADAY_CONSTANT_C_MOL

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ION_CONDUCTIVITY_TEMPERATURE_LIBRARY = (
    PROJECT_ROOT
    / "config"
    / "ion_transport"
    / "aqueous_ion_conductivity_temperature_reference_v1.json"
)


def load_ion_conductivity_temperature_library(path: str | Path = DEFAULT_ION_CONDUCTIVITY_TEMPERATURE_LIBRARY) -> dict:
    """读取并校验离子无限稀释当量电导温度估算库。"""
    library_path = Path(path)
    with library_path.open("r", encoding="utf-8") as stream:
        library = json.load(stream)
    if not isinstance(library, dict) or not isinstance(library.get("ions"), dict):
        raise ValueError("离子电导温度库必须包含 ions 对象")
    if not library.get("library_id") or not library.get("schema_version"):
        raise ValueError("离子电导温度库缺少 library_id 或 schema_version")

    references = library.get("references", {})
    model = library.get("temperature_model", {})
    temperature_range = model.get("valid_temperature_range_c")
    if not isinstance(temperature_range, list) or len(temperature_range) != 2:
        raise ValueError("离子电导温度库缺少有效的适用温度范围")
    minimum_c, maximum_c = (float(value) for value in temperature_range)
    if not all(math.isfinite(value) for value in (minimum_c, maximum_c)) or minimum_c >= maximum_c:
        raise ValueError("离子电导温度库适用温度范围无效")

    viscosity_model = library.get("water_viscosity_model", {})
    if not isinstance(viscosity_model, dict) or not viscosity_model.get("terms"):
        raise ValueError("离子电导温度库缺少水黏度计算参数")
    viscosity_references = viscosity_model.get("reference_ids") or []
    if not viscosity_references or any(reference_id not in references for reference_id in viscosity_references):
        raise ValueError("水黏度模型缺少有效来源引用")
    for term in viscosity_model["terms"]:
        try:
            coefficient = float(term["coefficient"])
            exponent = float(term["exponent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("水黏度模型参数格式无效") from exc
        if not math.isfinite(coefficient) or not math.isfinite(exponent):
            raise ValueError("水黏度模型参数必须是有限数值")
    temperature_references = model.get("reference_ids") or []
    if not temperature_references or any(reference_id not in references for reference_id in temperature_references):
        raise ValueError("离子温度修正模型缺少有效来源引用")

    for ion_id, entry in library["ions"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"离子 {ion_id} 的温度参数格式无效")
        try:
            charge = int(entry["charge_number"])
            lambda_25 = float(entry["lambda_eq_25_s_cm2_mol_equiv"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"离子 {ion_id} 缺少有效的电荷或 25°C 电导锚点") from exc
        if charge == 0 or not math.isfinite(lambda_25) or lambda_25 <= 0:
            raise ValueError(f"离子 {ion_id} 的电导温度参数无效")
        reference_ids = entry.get("reference_ids")
        if not reference_ids or any(reference_id not in references for reference_id in reference_ids):
            raise ValueError(f"离子 {ion_id} 缺少有效来源引用")
    return library


def _water_viscosity_pa_s(temperature_k: float, model: dict) -> float:
    """按 IAPWS SR6-08 的 0.1 MPa 简化关联式计算水的动力黏度。"""
    reduced_temperature = temperature_k / float(model["reference_temperature_k"])
    viscosity_micro_pa_s = sum(
        float(term["coefficient"]) * reduced_temperature ** float(term["exponent"])
        for term in model["terms"]
    )
    viscosity_pa_s = viscosity_micro_pa_s * 1e-6
    if not math.isfinite(viscosity_pa_s) or viscosity_pa_s <= 0:
        raise ValueError("水黏度关联式返回无效结果")
    return viscosity_pa_s


def ion_mobilities_from_temperature_reference(
    temperature_k: float,
    required_ion_ids: set[str] | list[str] | tuple[str, ...],
    library_path: str | Path = DEFAULT_ION_CONDUCTIVITY_TEMPERATURE_LIBRARY,
) -> dict:
    """由 25°C 无限稀释锚点及水黏度比例估算单离子迁移率。

    只在库声明的温度范围内计算；返回的 status 明确表示参考估算，
    调用方仍必须要求用户显式允许参考值估算。
    """
    temperature = float(temperature_k)
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("离子迁移温度必须是有限正数")
    library = load_ion_conductivity_temperature_library(library_path)
    temperature_c = temperature - 273.15
    model = library["temperature_model"]
    minimum_c, maximum_c = (float(value) for value in model["valid_temperature_range_c"])
    if temperature_c < minimum_c - 1e-9 or temperature_c > maximum_c + 1e-9:
        raise ValueError(
            f"公开离子温度参考估算仅覆盖 {minimum_c:g}–{maximum_c:g} °C，"
            f"当前 {temperature_c:g} °C；不进行范围外外推"
        )

    viscosity_model = library["water_viscosity_model"]
    viscosity_at_temperature = _water_viscosity_pa_s(temperature, viscosity_model)
    reference_temperature = float(library["reference_temperature_k"])
    viscosity_at_reference = _water_viscosity_pa_s(reference_temperature, viscosity_model)
    # Walden-style limiting-conductance estimate: lambda * eta ~= constant.
    # Do not apply the separate T/eta scaling for diffusion coefficients here.
    temperature_scale_factor = viscosity_at_reference / viscosity_at_temperature

    references = library["references"]
    ions: dict[str, dict] = {}
    for ion_id in sorted(set(required_ion_ids)):
        entry = library["ions"].get(ion_id)
        if entry is None:
            raise ValueError(f"离子温度参考库缺少离子 {ion_id}")
        lambda_25 = float(entry["lambda_eq_25_s_cm2_mol_equiv"])
        lambda_temperature = lambda_25 * temperature_scale_factor
        if not math.isfinite(lambda_temperature) or lambda_temperature <= 0:
            raise ValueError(f"离子 {ion_id} 的估算当量电导率无效")
        mobility = lambda_temperature * 1e-4 / FARADAY_CONSTANT_C_MOL
        ions[ion_id] = {
            "formula": entry["formula"],
            "charge_number": int(entry["charge_number"]),
            "lambda_eq_s_cm2_mol_equiv": lambda_temperature,
            "mobility_magnitude_m2_v_s": mobility,
            "reference_ids": list(entry["reference_ids"]),
        }

    source_ids = sorted(
        {reference_id for ion in ions.values() for reference_id in ion["reference_ids"]}
        | set(model["reference_ids"])
        | set(viscosity_model["reference_ids"])
    )
    return {
        "status": "supported_reference_estimate",
        "method": model["model_id"],
        "formula": model["formula"],
        "temperature_k": temperature,
        "temperature_c": temperature_c,
        "valid_temperature_range_c": [minimum_c, maximum_c],
        "water_viscosity_pa_s": viscosity_at_temperature,
        "temperature_scale_factor": temperature_scale_factor,
        "condition_matched": False,
        "source_ids": source_ids,
        "source_citations": [
            references[reference_id]["citation"]
            for reference_id in source_ids
        ],
        "source_urls": [
            references[reference_id]["url"]
            for reference_id in source_ids
        ],
        "ions": ions,
    }

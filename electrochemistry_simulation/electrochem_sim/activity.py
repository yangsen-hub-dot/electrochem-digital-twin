"""电解质活度估算；本模块不自行猜测未配置的溶液组成。"""

from __future__ import annotations

import math

DAVIES_MODEL = "davies_single_ion_convention"
DAVIES_A_WATER_25C = 0.509
DAVIES_REFERENCE_TEMPERATURE_K = 298.15
DAVIES_MAX_IONIC_STRENGTH_MOLAL = 0.5


def ionic_strength_molal(ion_molalities: dict[str, float], charges: dict[str, int]) -> float:
    """由离子组成计算 I = 1/2 Σ mᵢ zᵢ²，单位 mol/kg。"""
    if not ion_molalities:
        raise ValueError("计算离子强度至少需要一种离子")
    total = 0.0
    for ion, molality_value in ion_molalities.items():
        if ion not in charges:
            raise ValueError(f"缺少离子 {ion} 的电荷数")
        molality = float(molality_value)
        charge = int(charges[ion])
        if not math.isfinite(molality) or molality < 0:
            raise ValueError(f"离子 {ion} 的质量摩尔浓度必须有限且非负")
        if charge == 0:
            raise ValueError(f"离子 {ion} 的电荷数不能为零")
        total += molality * charge**2
    return 0.5 * total


def davies_activity_coefficient(
    ionic_strength_molal: float,
    charge_number: int,
    A: float = DAVIES_A_WATER_25C,
) -> float:
    """Davies 近似：log10(γᵢ)=-A zᵢ²(√I/(1+√I)-0.3I)。

    单离子 γ 采用约定性估算；水溶液 25 °C 默认 A=0.509。
    本实现将 I 限定在 0–0.5 mol/kg 的工程适用范围内。
    """
    ionic_strength = float(ionic_strength_molal)
    coefficient_A = float(A)
    charge = int(charge_number)
    if not math.isfinite(ionic_strength) or not 0 <= ionic_strength <= DAVIES_MAX_IONIC_STRENGTH_MOLAL:
        raise ValueError("Davies 模型要求离子强度在 0–0.5 mol/kg 范围内")
    if not math.isfinite(coefficient_A) or coefficient_A <= 0:
        raise ValueError("Davies 温度参数 A 必须有限且大于零")
    if charge == 0:
        raise ValueError("活性离子的电荷数不能为零")
    root_I = math.sqrt(ionic_strength)
    log10_gamma = -coefficient_A * charge**2 * (root_I / (1 + root_I) - 0.3 * ionic_strength)
    return 10**log10_gamma


def activity_coefficients_for_cell(
    settings: dict,
    charge_number: int,
    temperature_k: float,
    electrons_transferred: int = 2,
) -> dict:
    """读取两侧离子强度并返回活度系数；未启用时明确使用 γ=1。"""
    if not settings.get("enabled", False):
        return {
            "enabled": False,
            "model": settings.get("model", DAVIES_MODEL),
            "status": "disabled",
            "gamma_left": 1.0,
            "gamma_right": 1.0,
            "ionic_strength_molal_by_side": None,
            "voltage_correction_mv": 0.0,
            "assumptions": ["活度系数取 1；维持理想基线"],
        }

    model = settings.get("model", DAVIES_MODEL)
    if model != DAVIES_MODEL:
        raise ValueError(f"尚未注册活度系数模型: {model}")
    A = float(settings.get("A", DAVIES_A_WATER_25C))
    reference_temperature = float(settings.get("A_reference_temperature_k", DAVIES_REFERENCE_TEMPERATURE_K))
    if not math.isclose(float(temperature_k), reference_temperature, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(
            f"当前 A={A:g} 的参考温度为 {reference_temperature:g} K；"
            "温度不匹配，请提供对应温度的 Davies A 参数及其参考温度"
        )
    ionic_by_side = settings.get("ionic_strength_molal_by_side") or {}
    if ionic_by_side.get("left") is None or ionic_by_side.get("right") is None:
        raise ValueError("启用活度修正时，必须分别填写左右侧离子强度（mol/kg）")
    left_I = float(ionic_by_side["left"])
    right_I = float(ionic_by_side["right"])
    gamma_left = davies_activity_coefficient(left_I, charge_number, A)
    gamma_right = davies_activity_coefficient(right_I, charge_number, A)
    # Same standard state on both sides; its factor cancels in the activity ratio.
    if int(electrons_transferred) <= 0:
        raise ValueError("电子转移数必须大于零")
    correction_mv = (
        8.314462618 * float(temperature_k) / (int(electrons_transferred) * 96485.33212)
        * math.log(gamma_right / gamma_left) * 1000
    )
    return {
        "enabled": True,
        "model": model,
        "status": "estimated",
        "gamma_left": gamma_left,
        "gamma_right": gamma_right,
        "ionic_strength_source": settings.get("ionic_strength_source", "configured_values"),
        "ionic_strength_molal_by_side": {"left": left_I, "right": right_I},
        "A": A,
        "A_reference_temperature_k": reference_temperature,
        "voltage_correction_mv": correction_mv,
        "assumptions": [
            "Davies 单离子活度系数采用约定性估算",
            "稀水溶液近似 molarity ≈ molality；状态账本未追踪溶剂质量",
            "离子强度由用户配置；不从仅含活性离子的状态自动猜算",
        ],
    }


def activity_coefficients_for_reaction(
    settings: dict,
    *,
    charges: dict[str, int],
    reaction_quotient_terms: dict[str, float],
    temperature_k: float,
    electrons_transferred: int,
) -> dict:
    """为配置化半反应商计算每种离子的两侧活度系数及电压修正。

    ``reaction_quotient_terms`` 使用产物化学计量数为正、反应物为负的
    约定；电子和纯固体/纯液体不写入。标准态相同的常数会在两侧相减时抵消。
    """
    if not settings.get("enabled", False):
        gamma_by_species = {
            ion: {"left": 1.0, "right": 1.0} for ion in reaction_quotient_terms
        }
        return {
            "enabled": False,
            "model": settings.get("model", DAVIES_MODEL),
            "status": "disabled",
            "gamma_left": 1.0,
            "gamma_right": 1.0,
            "gamma_by_species": gamma_by_species,
            "ionic_strength_molal_by_side": None,
            "voltage_correction_mv": 0.0,
            "assumptions": ["活度系数取 1；维持理想基线"],
        }

    model = settings.get("model", DAVIES_MODEL)
    if model != DAVIES_MODEL:
        raise ValueError(f"尚未注册活度系数模型: {model}")
    coefficient_A = float(settings.get("A", DAVIES_A_WATER_25C))
    reference_temperature = float(
        settings.get("A_reference_temperature_k", DAVIES_REFERENCE_TEMPERATURE_K)
    )
    temperature = float(temperature_k)
    electrons = int(electrons_transferred)
    if not math.isclose(temperature, reference_temperature, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(
            f"当前 A={coefficient_A:g} 的参考温度为 {reference_temperature:g} K；"
            "温度不匹配，请提供对应温度的 Davies A 参数及其参考温度"
        )
    if electrons <= 0:
        raise ValueError("电子转移数必须大于零")
    ionic_by_side = settings.get("ionic_strength_molal_by_side") or {}
    if ionic_by_side.get("left") is None or ionic_by_side.get("right") is None:
        raise ValueError("启用活度修正时，必须分别填写左右侧离子强度（mol/kg）")
    left_I = float(ionic_by_side["left"])
    right_I = float(ionic_by_side["right"])

    gamma_by_species: dict[str, dict[str, float]] = {}
    for ion in reaction_quotient_terms:
        if ion not in charges:
            raise ValueError(f"反应商物种 {ion} 未配置电荷数")
        charge = int(charges[ion])
        gamma_by_species[ion] = {
            "left": davies_activity_coefficient(left_I, charge, coefficient_A),
            "right": davies_activity_coefficient(right_I, charge, coefficient_A),
        }
    log_gamma_quotient_change = sum(
        float(exponent)
        * math.log(gamma_by_species[ion]["right"] / gamma_by_species[ion]["left"])
        for ion, exponent in reaction_quotient_terms.items()
    )
    correction_mv = (
        -8.314462618 * temperature / (electrons * 96485.33212)
        * log_gamma_quotient_change * 1000
    )
    primary = next(iter(reaction_quotient_terms))
    return {
        "enabled": True,
        "model": model,
        "status": "estimated",
        "gamma_left": gamma_by_species[primary]["left"],
        "gamma_right": gamma_by_species[primary]["right"],
        "gamma_by_species": gamma_by_species,
        "ionic_strength_source": settings.get("ionic_strength_source", "configured_values"),
        "ionic_strength_molal_by_side": {"left": left_I, "right": right_I},
        "A": coefficient_A,
        "A_reference_temperature_k": reference_temperature,
        "voltage_correction_mv": correction_mv,
        "assumptions": [
            "Davies 单离子活度系数采用约定性估算",
            "稀水溶液近似 molarity ≈ molality；状态账本未追踪溶剂质量",
            "活度修正按配置的半反应商化学计量数逐项计算",
        ],
    }


def dimensionless_activity(concentration_m: float, gamma: float, standard_concentration_m: float = 1.0) -> float:
    """以 1 mol/L 标准浓度近似计算无量纲活度 γc/c°。"""
    values = (float(concentration_m), float(gamma), float(standard_concentration_m))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("浓度、活度系数和标准浓度必须有限且大于零")
    return values[1] * values[0] / values[2]

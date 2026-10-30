"""液接电势估算：Henderson 近似与 Nernst–Planck 零电流关系积分。"""

from __future__ import annotations

import math
from pathlib import Path

from .ion_transport import load_ion_transport_library
from .ion_transport_temperature import ion_mobilities_from_temperature_reference

R_J_PER_MOL_K = 8.314462618
F_C_PER_MOL = 96485.33212
SUPPORTED_MODEL = "henderson_approximation"
NERNST_PLANCK_MODEL = "nernst_planck_zero_current_linear_profile"
SUPPORTED_MODELS = {SUPPORTED_MODEL, NERNST_PLANCK_MODEL}
ALLOW_REFERENCE_POLICY = "allow_reference_estimate"
NERNST_PLANCK_GRID_POINTS = 2001


def henderson_junction_potential_v(
    concentrations_a_m: dict[str, float],
    concentrations_b_m: dict[str, float],
    charges: dict[str, int],
    mobilities_m2_v_s: dict[str, float],
    temperature_k: float,
) -> float:
    """计算 A→B 液接电势，使用离子浓度线性变化、迁移率恒定近似。

    Henderson 近似：
      Δφ = -(RT/F) Σ[sgn(zᵢ)uᵢΔcᵢ] · ln(Sᴮ/Sᴬ)/(Sᴮ-Sᴬ)
      S = Σ[|zᵢ|uᵢcᵢ]

    浓度单位在两端保持一致即可（本项目使用 mol/L）；结果正号表示
    B 端溶液电势高于 A 端。此值是模型估算，不是测量值。
    """
    temperature = float(temperature_k)
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("液接电势计算需要有限正温度")
    ion_ids = set(concentrations_a_m) | set(concentrations_b_m)
    if not ion_ids:
        raise ValueError("液接两端至少需要一种离子")

    conductivity_a = 0.0
    conductivity_b = 0.0
    signed_transport_change = 0.0
    for ion_id in ion_ids:
        if ion_id not in charges or ion_id not in mobilities_m2_v_s:
            raise ValueError(f"离子 {ion_id} 缺少电荷数或迁移率")
        charge = int(charges[ion_id])
        mobility = float(mobilities_m2_v_s[ion_id])
        concentration_a = float(concentrations_a_m.get(ion_id, 0.0))
        concentration_b = float(concentrations_b_m.get(ion_id, 0.0))
        if charge == 0:
            raise ValueError(f"离子 {ion_id} 的电荷数不能为零")
        if not math.isfinite(mobility) or mobility <= 0:
            raise ValueError(f"离子 {ion_id} 的迁移率必须有限且大于零")
        if any(not math.isfinite(c) or c < 0 for c in (concentration_a, concentration_b)):
            raise ValueError(f"离子 {ion_id} 的浓度必须有限且非负")
        charge_weighted_mobility = abs(charge) * mobility
        conductivity_a += charge_weighted_mobility * concentration_a
        conductivity_b += charge_weighted_mobility * concentration_b
        signed_transport_change += math.copysign(1.0, charge) * mobility * (concentration_b - concentration_a)

    if conductivity_a <= 0 or conductivity_b <= 0:
        raise ValueError("液接两端都必须含有可迁移离子")

    denominator = conductivity_b - conductivity_a
    scale = max(conductivity_a, conductivity_b)
    if abs(denominator) <= 1e-12 * scale:
        log_ratio_over_difference = 1.0 / conductivity_a
    else:
        log_ratio_over_difference = math.log(conductivity_b / conductivity_a) / denominator
    return -R_J_PER_MOL_K * temperature / F_C_PER_MOL * signed_transport_change * log_ratio_over_difference


def nernst_planck_junction_potential_v(
    concentrations_a_m: dict[str, float],
    concentrations_b_m: dict[str, float],
    charges: dict[str, int],
    diffusion_coefficients_m2_s: dict[str, float],
    temperature_k: float,
    *,
    grid_points: int = NERNST_PLANCK_GRID_POINTS,
) -> float:
    """Numerically integrate the 1D zero-current Nernst–Planck field relation.

    The concentration path is prescribed as a linear interpolation between
    the two bulk solutions; local electroneutrality is assumed. These are the
    same principal junction assumptions used by the Henderson closed form, so
    this numerical implementation is a method cross-check, not a more general
    Poisson–Nernst–Planck solver.
    """
    temperature = float(temperature_k)
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("液接电势计算需要有限正温度")
    if isinstance(grid_points, bool) or int(grid_points) < 3 or int(grid_points) % 2 == 0:
        raise ValueError("Nernst–Planck Simpson 积分需要奇数个且不少于 3 个网格点")
    grid_points = int(grid_points)
    ion_ids = set(concentrations_a_m) | set(concentrations_b_m)
    if not ion_ids:
        raise ValueError("液接两端至少需要一种离子")

    species = []
    for ion_id in sorted(ion_ids):
        if ion_id not in charges or ion_id not in diffusion_coefficients_m2_s:
            raise ValueError(f"离子 {ion_id} 缺少电荷数或扩散系数")
        charge = int(charges[ion_id])
        diffusion = float(diffusion_coefficients_m2_s[ion_id])
        concentration_a = float(concentrations_a_m.get(ion_id, 0.0))
        concentration_b = float(concentrations_b_m.get(ion_id, 0.0))
        if charge == 0:
            raise ValueError(f"离子 {ion_id} 的电荷数不能为零")
        if not math.isfinite(diffusion) or diffusion <= 0:
            raise ValueError(f"离子 {ion_id} 的扩散系数必须有限且大于零")
        if any(not math.isfinite(c) or c < 0 for c in (concentration_a, concentration_b)):
            raise ValueError(f"离子 {ion_id} 的浓度必须有限且非负")
        species.append((charge, diffusion, concentration_a, concentration_b))

    # With zero current and local electroneutrality, the NP equations give
    # d(phi)/dx = -(RT/F) * sum(z_i D_i dc_i/dx) / sum(z_i^2 D_i c_i).
    # The prescribed linear profile makes the numerator constant; Simpson
    # quadrature integrates the position-dependent denominator.
    increments = [item[3] - item[2] for item in species]
    numerator = sum(item[0] * item[1] * delta for item, delta in zip(species, increments))
    step = 1.0 / (grid_points - 1)
    weighted_sum = 0.0
    for index in range(grid_points):
        fraction = index * step
        denominator = sum(
            charge * charge * diffusion
            * (concentration_a + fraction * (concentration_b - concentration_a))
            for charge, diffusion, concentration_a, concentration_b in species
        )
        if not math.isfinite(denominator) or denominator <= 0:
            raise ValueError("Nernst–Planck 液接剖面缺少可迁移离子")
        field_factor = numerator / denominator
        weight = 1 if index in {0, grid_points - 1} else (4 if index % 2 else 2)
        weighted_sum += weight * field_factor
    integral = step * weighted_sum / 3.0
    return -R_J_PER_MOL_K * temperature / F_C_PER_MOL * integral


def _neutrality_error(concentrations_m: dict[str, float], charges: dict[str, int]) -> float:
    net_charge = sum(charges[ion_id] * concentration for ion_id, concentration in concentrations_m.items())
    charge_scale = sum(abs(charges[ion_id]) * concentration for ion_id, concentration in concentrations_m.items())
    return abs(net_charge) / max(charge_scale, 1e-30)


def _bridge_composition(settings: dict) -> dict[str, float]:
    bridge = settings.get("bridge_electrolyte") or {}
    components = bridge.get("electrolytes")
    if components is None:
        components = [bridge]
    if not isinstance(components, list) or not components:
        raise ValueError("盐桥需要配置至少一种电解质")
    concentrations: dict[str, float] = {}
    for component in components:
        salt_concentration = float(component["concentration_m"])
        ion_stoichiometry = component.get("ion_stoichiometry")
        if not math.isfinite(salt_concentration) or salt_concentration <= 0:
            raise ValueError("盐桥电解质浓度必须有限且大于零")
        if not isinstance(ion_stoichiometry, dict) or not ion_stoichiometry:
            raise ValueError("盐桥电解质必须配置离子及其化学计量数")
        for ion_id, coefficient_value in ion_stoichiometry.items():
            coefficient = float(coefficient_value)
            if not math.isfinite(coefficient) or coefficient <= 0:
                raise ValueError(f"盐桥离子 {ion_id} 的化学计量数必须大于零")
            concentrations[ion_id] = concentrations.get(ion_id, 0.0) + salt_concentration * coefficient
    return concentrations


def _declared_ion_ids(settings: dict) -> list[str]:
    side_ion_ids = settings.get("solution_ions_by_side") or {}
    ion_ids = set(side_ion_ids.get("left", [])) | set(side_ion_ids.get("right", []))
    bridge = settings.get("bridge_electrolyte") or {}
    components = bridge.get("electrolytes") or [bridge]
    for component in components:
        ion_ids.update((component.get("ion_stoichiometry") or {}).keys())
    return sorted(ion_ids)


def _load_library(settings: dict) -> tuple[dict, Path]:
    configured_path = settings.get("transport_parameter_library")
    if not configured_path:
        raise ValueError("未配置离子迁移参数库路径")
    path = Path(configured_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / "config" / path
    return load_ion_transport_library(path), path


def evaluate_liquid_junction_potential(
    settings: dict | None,
    *,
    temperature_k: float | None = None,
    concentrations_by_side_m: dict[str, dict[str, float]] | None = None,
    species_definitions: dict | None = None,
) -> dict:
    """评估左右半电池及盐桥构成的两处液接界面。

    无限稀释参数默认只供参考。只有配置显式选择
    ``allow_reference_estimate`` 时才会用它们生成标记为估算值的结果。
    """
    settings = settings or {}
    enabled = bool(settings.get("enabled", False))
    selected_model = settings.get("model", SUPPORTED_MODEL)
    parameter_context = {
        "model": selected_model,
        "transport_parameter_library": settings.get("transport_parameter_library"),
        "required_ion_ids": _declared_ion_ids(settings),
        "parameter_use_policy": settings.get("parameter_use_policy"),
    }
    if not enabled:
        return {
            "enabled": False,
            "status": "not_modeled",
            "correction_mv": None,
            "condition_matched": False,
            "reason": "液接电势当前关闭；电压结果不含此项修正",
            **parameter_context,
        }

    temperature_selection = None
    try:
        if selected_model not in SUPPORTED_MODELS:
            raise ValueError(f"当前未注册液接电势模型: {selected_model}")
        if temperature_k is None or concentrations_by_side_m is None or species_definitions is None:
            raise ValueError("液接电势计算缺少温度、两侧离子浓度或物种定义")
        side_ion_ids = settings.get("solution_ions_by_side") or {}
        left_ids = side_ion_ids.get("left") or []
        right_ids = side_ion_ids.get("right") or []
        if not left_ids or not right_ids:
            raise ValueError("需要分别配置左右半电池所含离子")

        left = {ion_id: float(concentrations_by_side_m["left"].get(ion_id, 0.0)) for ion_id in left_ids}
        right = {ion_id: float(concentrations_by_side_m["right"].get(ion_id, 0.0)) for ion_id in right_ids}
        bridge = _bridge_composition(settings)
        all_ion_ids = set(left) | set(right) | set(bridge)
        parameter_context["required_ion_ids"] = sorted(all_ion_ids)

        charges: dict[str, int] = {}
        for ion_id in all_ion_ids:
            definition = species_definitions.get(ion_id)
            if not isinstance(definition, dict) or "charge" not in definition:
                raise ValueError(f"离子 {ion_id} 未配置电荷数")
            charges[ion_id] = int(definition["charge"])
        for name, composition in (("左侧溶液", left), ("盐桥", bridge), ("右侧溶液", right)):
            if _neutrality_error(composition, charges) > 1e-6:
                raise ValueError(f"{name}离子组成不满足电中性；请补全离子或核对浓度/化学计量数")

        library, library_path = _load_library(settings)
        parameter_context["transport_parameter_library"] = str(library_path)
        library_ions = library["ions"]
        policy = settings.get("parameter_use_policy", "condition_matched_only")
        library_scope = library.get("status", "unspecified")
        condition_matched = library_scope in {"condition_matched", "validated_condition_matched"}
        if not condition_matched and policy != ALLOW_REFERENCE_POLICY:
            return {
                "enabled": True,
                "status": "awaiting_condition_matched_parameters",
                "correction_mv": None,
                "condition_matched": False,
                "reason": "参数库数据不是当前条件匹配值；如仅做探索性估算，需显式选择 allow_reference_estimate",
                "library_status": library_scope,
                **parameter_context,
            }

        temperature = float(temperature_k)
        mobilities: dict[str, float] = {}
        temperature_mismatch = False
        for ion_id in all_ion_ids:
            if ion_id not in library_ions:
                raise ValueError(f"参数库缺少离子 {ion_id} 的扩散/迁移数据")
            entry = library_ions[ion_id]
            if int(entry["charge_number"]) != charges[ion_id]:
                raise ValueError(f"离子 {ion_id} 的参数库电荷数与实验物种定义不一致")
            reference_temperature = float(entry["reference_temperature_k"])
            if not math.isclose(temperature, reference_temperature, rel_tol=0.0, abs_tol=1e-6):
                temperature_mismatch = True
            else:
                mobilities[ion_id] = float(entry["mobility_magnitude_m2_V_s"])

        if temperature_mismatch:
            if condition_matched:
                raise ValueError("条件匹配迁移参数的参考温度与当前温度不一致，不使用其他库替代")
            if policy != ALLOW_REFERENCE_POLICY:
                raise ValueError("迁移参数参考温度与当前温度不匹配；未显式允许参考值估算")
            temperature_selection = ion_mobilities_from_temperature_reference(
                temperature,
                all_ion_ids,
            )
            mobilities = {
                ion_id: float(temperature_selection["ions"][ion_id]["mobility_magnitude_m2_v_s"])
                for ion_id in all_ion_ids
            }

        diffusion_coefficients = {
            ion_id: mobility * R_J_PER_MOL_K * temperature
            / (abs(charges[ion_id]) * F_C_PER_MOL)
            for ion_id, mobility in mobilities.items()
        }

        def evaluate_model(model_id: str) -> tuple[float, float, float]:
            if model_id == NERNST_PLANCK_MODEL:
                calculate = lambda a, b: nernst_planck_junction_potential_v(
                    a, b, charges, diffusion_coefficients, temperature
                )
            else:
                calculate = lambda a, b: henderson_junction_potential_v(
                    a, b, charges, mobilities, temperature
                )
            left_to_bridge_v = calculate(left, bridge)
            bridge_to_right_v = calculate(bridge, right)
            total_v = left_to_bridge_v + bridge_to_right_v
            return total_v, left_to_bridge_v, bridge_to_right_v

        henderson_total_v, henderson_left_v, henderson_right_v = evaluate_model(SUPPORTED_MODEL)
        nernst_planck_total_v, nernst_planck_left_v, nernst_planck_right_v = evaluate_model(NERNST_PLANCK_MODEL)
        if selected_model == NERNST_PLANCK_MODEL:
            selected_total_v, left_to_bridge, bridge_to_right = (
                nernst_planck_total_v,
                nernst_planck_left_v,
                nernst_planck_right_v,
            )
        else:
            selected_total_v, left_to_bridge, bridge_to_right = (
                henderson_total_v,
                henderson_left_v,
                henderson_right_v,
            )
        model_name_zh = (
            "Nernst–Planck 线性剖面数值积分（简化）"
            if selected_model == NERNST_PLANCK_MODEL
            else "Henderson 线性梯度近似"
        )
        correction_mv = selected_total_v * 1000
        model_comparison = {
            "henderson_approximation": {
                "correction_mv": henderson_total_v * 1000,
                "left_solution_to_bridge_mv": henderson_left_v * 1000,
                "bridge_to_right_solution_mv": henderson_right_v * 1000,
            },
            NERNST_PLANCK_MODEL: {
                "correction_mv": nernst_planck_total_v * 1000,
                "left_solution_to_bridge_mv": nernst_planck_left_v * 1000,
                "bridge_to_right_solution_mv": nernst_planck_right_v * 1000,
                "numerical_grid_points": NERNST_PLANCK_GRID_POINTS,
            },
            "difference_nernst_planck_minus_henderson_mv": (
                nernst_planck_total_v - henderson_total_v
            ) * 1000,
            "comparison_note": "两条路线使用相同离子参数与线性浓度剖面；该差值只核对数值积分，不代表准确度或实验验证。",
        }
        estimated_from_reference = not condition_matched
        return {
            "enabled": True,
            "status": "estimated_from_reference_parameters" if estimated_from_reference else "calculated",
            "correction_mv": correction_mv,
            "condition_matched": condition_matched,
            "library_status": library_scope,
            "model_name_zh": model_name_zh,
            "temperature_parameter_selection": temperature_selection,
            "model_comparison": model_comparison,
            "junctions_mv": {
                "left_solution_to_bridge": left_to_bridge * 1000,
                "bridge_to_right_solution": bridge_to_right * 1000,
            },
            "ion_mobilities_m2_v_s": mobilities,
            "ion_diffusion_coefficients_m2_s": diffusion_coefficients,
            "numerical_method": "Nernst–Planck route: 1D zero-current field along a prescribed linear concentration profile; Simpson quadrature; not a full steady flux-conservation or Poisson solver",
            "numerical_grid_points": NERNST_PLANCK_GRID_POINTS,
            "assumptions": [
                *(["Henderson 线性浓度梯度近似；各离子迁移率在液接区内视为常数"]
                  if selected_model == SUPPORTED_MODEL else [
                      "一维 Nernst–Planck 零电流电场关系数值积分；局部电中性，离子浓度沿界面线性插值；未求解完整的稳态各离子通量守恒或 Poisson 空间电荷方程",
                      "扩散系数由迁移率经 Nernst–Einstein 关系换算；该线性剖面条件下结果应与 Henderson 近似一致",
                  ]),
                "用两处界面电势差相加",
                "以离子浓度近似活度；未模拟液接区的空间剖面或时间演化",
                *(["迁移参数来自非条件匹配的无限稀释参考值；结果仅作探索性估算"] if estimated_from_reference else []),
            ],
            "reason": None,
            **parameter_context,
        }
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {
            "enabled": True,
            "status": "invalid_parameters",
            "correction_mv": None,
            "condition_matched": False,
            "reason": str(exc),
            "temperature_parameter_selection": temperature_selection,
            **parameter_context,
        }

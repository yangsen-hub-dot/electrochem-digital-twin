"""连续商用移液器参考曲线与量程选择。

该模块仅把厂家离散性能锚点插值成量程内的参考情景；不是本项目设备校准。
单位：体积 μL、误差限 μL。
"""

from __future__ import annotations

import math


MODEL_ID = "continuous_commercial_pipette_reference"


def select_pipette(profiles: list[dict], volume_ul: float) -> dict:
    """选择可覆盖设定值的移液器；有重叠时优先较小量程的型号。"""
    if isinstance(volume_ul, bool) or not math.isfinite(float(volume_ul)) or volume_ul <= 0:
        raise ValueError("移液设定值必须为有限正数")
    candidates = []
    for profile in profiles:
        low, high = map(float, profile["nominal_volume_range_ul"])
        if low <= volume_ul <= high:
            candidates.append((high - low, high, profile))
    if not candidates:
        raise ValueError(f"{volume_ul:g} μL 超出当前移液器组合量程")
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def interpolate_anchor(anchors: list[dict], volume_ul: float) -> dict:
    """在线性绝对误差单位下插值，不跨量程端点外推。"""
    if not anchors:
        raise ValueError("连续移液参考曲线缺少锚点")
    ordered = sorted(anchors, key=lambda row: float(row["volume_ul"]))
    volume_ul = float(volume_ul)
    low = float(ordered[0]["volume_ul"])
    high = float(ordered[-1]["volume_ul"])
    if not low <= volume_ul <= high:
        raise ValueError(f"{volume_ul:g} μL 超出厂家参考锚点范围 {low:g}–{high:g} μL")
    for point in ordered:
        if math.isclose(volume_ul, float(point["volume_ul"]), rel_tol=0.0, abs_tol=1e-10):
            return {**point, "evidence_status": "manufacturer_specification_anchor"}
    for left, right in zip(ordered, ordered[1:]):
        x0, x1 = float(left["volume_ul"]), float(right["volume_ul"])
        if x0 < volume_ul < x1:
            fraction = (volume_ul - x0) / (x1 - x0)
            return {
                "volume_ul": volume_ul,
                "systematic_error_limit_ul": _lerp(
                    float(left["systematic_error_limit_ul"]),
                    float(right["systematic_error_limit_ul"]), fraction,
                ),
                "repeatability_sd_limit_ul": _lerp(
                    float(left["repeatability_sd_limit_ul"]),
                    float(right["repeatability_sd_limit_ul"]), fraction,
                ),
                "evidence_status": "linear_interpolation_between_manufacturer_anchors",
            }
    raise ValueError("无法在厂家参考锚点间插值")


def _lerp(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * fraction


def reference_at_volume(parameters: dict, volume_ul: float) -> dict:
    """解析型号、连续误差边界和证据状态。"""
    profiles = parameters.get("pipette_profiles", [])
    profile = select_pipette(profiles, float(volume_ul))
    reference = interpolate_anchor(profile.get("manufacturer_specification_anchors", []), volume_ul)
    multiplier = float(parameters.get("scenario_multiplier", 1.0))
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("移液情景倍率必须是有限正数")
    return {
        "pipette_id": str(profile["pipette_id"]),
        "pipette_name": str(profile["pipette_name"]),
        "tip_id": str(profile["recommended_tip_id"]),
        "nominal_volume_range_ul": list(map(float, profile["nominal_volume_range_ul"])),
        "volume_ul": float(volume_ul),
        "systematic_error_limit_ul": float(reference["systematic_error_limit_ul"]) * multiplier,
        "repeatability_sd_limit_ul": float(reference["repeatability_sd_limit_ul"]) * multiplier,
        "evidence_status": reference["evidence_status"],
        "parameter_source_status": "manufacturer_specification_reference_not_project_calibration",
        "scenario_multiplier": multiplier,
    }


def aliquots_for_volume(volume_ul: float) -> list[dict]:
    """用已选双量程组合分配定量转移；超过 P1000 单次容量时均分。

    P200 优先处理 20–200 μL；P1000 处理大于 200–1000 μL。
    对更大总量按最少次数等分，保证每一吸均落在 P1000 量程内。
    """
    volume_ul = float(volume_ul)
    if not math.isfinite(volume_ul) or volume_ul <= 0:
        raise ValueError("计划转移量必须为有限正数")
    if volume_ul < 20.0:
        raise ValueError("小于 20 μL，超出当前 P200/P1000 组合的移液范围")
    if volume_ul <= 200.0:
        return [{"pipette_id": "gilson_pipetman_p200", "volume_ul": volume_ul}]
    count = max(1, math.ceil(volume_ul / 1000.0))
    aliquot = volume_ul / count
    if aliquot < 100.0:
        raise ValueError("自动分次后单次吸液低于 P1000 最低量程")
    result = [{"pipette_id": "gilson_pipetman_p1000", "volume_ul": aliquot}
              for _ in range(count - 1)]
    result.append({"pipette_id": "gilson_pipetman_p1000",
                   "volume_ul": volume_ul - math.fsum(row["volume_ul"] for row in result)})
    return result

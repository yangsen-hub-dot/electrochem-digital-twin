"""带来源边界的 Davies Debye–Hückel A 温度参数查表。"""

from __future__ import annotations

import json
import math
from pathlib import Path


DEFAULT_DAVIES_A_LIBRARY = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "activity"
    / "davies_debye_huckel_A_water_neA_v1.json"
)


def davies_A_at_temperature(temperature_k: float, library_path: str | Path = DEFAULT_DAVIES_A_LIBRARY) -> dict:
    """返回来源表内的 A 值；离散表点之间线性插值，范围外拒绝外推。"""
    temperature = float(temperature_k)
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("温度必须是有限正数")

    with Path(library_path).open("r", encoding="utf-8") as stream:
        library = json.load(stream)
    values = library.get("values") if isinstance(library, dict) else None
    if not isinstance(values, list) or len(values) < 2:
        raise ValueError("Davies A 温度表格式无效")

    points = sorted(
        (float(item["temperature_c"]), float(item["A"])) for item in values
    )
    temperature_c = temperature - 273.15
    minimum_c, maximum_c = points[0][0], points[-1][0]
    if temperature_c < minimum_c - 1e-9 or temperature_c > maximum_c + 1e-9:
        raise ValueError(
            f"Davies A 温度表覆盖 {minimum_c:g}–{maximum_c:g} °C，"
            f"当前 {temperature_c:g} °C；不进行范围外外推"
        )

    reference = next(iter(library.get("references", {})), None)
    reference_metadata = library.get("references", {}).get(reference, {}) if reference else {}
    for point_c, coefficient in points:
        if math.isclose(temperature_c, point_c, rel_tol=0.0, abs_tol=1e-8):
            return {
                "A": coefficient,
                "temperature_k": temperature,
                "method": "published_tabulated_value",
                "status": "supported_exact",
                "source_id": reference,
                "source_citation": reference_metadata.get("citation"),
                "source_url": reference_metadata.get("url"),
                "table_temperature_range_c": [minimum_c, maximum_c],
            }

    for (lower_c, lower_a), (upper_c, upper_a) in zip(points, points[1:]):
        if lower_c < temperature_c < upper_c:
            fraction = (temperature_c - lower_c) / (upper_c - lower_c)
            coefficient = lower_a + fraction * (upper_a - lower_a)
            return {
                "A": coefficient,
                "temperature_k": temperature,
                "method": "linear_interpolation_between_published_values",
                "status": "supported_interpolated",
                "source_id": reference,
                "source_citation": reference_metadata.get("citation"),
                "source_url": reference_metadata.get("url"),
                "interpolation_interval_c": [lower_c, upper_c],
                "table_temperature_range_c": [minimum_c, maximum_c],
            }
    raise ValueError("Davies A 温度表未找到可用区间")

"""离子迁移参数库读取与扩散系数/电迁移率换算。"""

from __future__ import annotations

import json
import math
from pathlib import Path

GAS_CONSTANT_J_MOL_K = 8.314462618
FARADAY_CONSTANT_C_MOL = 96485.33212


def mobility_magnitude_from_diffusion(
    diffusion_coefficient_m2_s: float,
    charge_number: int,
    temperature_k: float,
) -> float:
    """用 Einstein/Nernst–Einstein 关系计算电迁移率的正值大小。

    该关系用于给定条件下的理想/稀溶液近似；阴阳离子迁移方向由电荷符号决定。
    输入扩散系数必须与所给温度、溶液条件相匹配。
    """
    diffusion = float(diffusion_coefficient_m2_s)
    charge = int(charge_number)
    temperature = float(temperature_k)
    if not math.isfinite(diffusion) or diffusion <= 0:
        raise ValueError("扩散系数必须是有限正数")
    if charge == 0:
        raise ValueError("离子电荷数不能为零")
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("温度必须是有限正数")
    return abs(charge) * FARADAY_CONSTANT_C_MOL * diffusion / (GAS_CONSTANT_J_MOL_K * temperature)


def load_ion_transport_library(path: str | Path) -> dict:
    """读取并校验离子迁移参数 JSON，不做条件插值或自动外推。"""
    library_path = Path(path)
    with library_path.open("r", encoding="utf-8") as stream:
        library = json.load(stream)
    if not isinstance(library, dict) or not isinstance(library.get("ions"), dict):
        raise ValueError("离子迁移参数库必须包含 ions 对象")
    if not library.get("library_id") or not library.get("schema_version"):
        raise ValueError("离子迁移参数库缺少 library_id 或 schema_version")
    references = library.get("references", {})
    for ion_id, entry in library["ions"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"离子 {ion_id} 的参数记录格式无效")
        try:
            charge = int(entry["charge_number"])
            diffusion = float(entry["diffusion_coefficient_m2_s"])
            mobility = float(entry["mobility_magnitude_m2_V_s"])
            temperature = float(entry["reference_temperature_k"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"离子 {ion_id} 缺少有效的电荷、扩散系数、迁移率或参考温度") from exc
        if charge == 0 or not all(math.isfinite(value) and value > 0 for value in (diffusion, mobility, temperature)):
            raise ValueError(f"离子 {ion_id} 的电荷或迁移参数无效")
        if not entry.get("reference_ids") or any(ref_id not in references for ref_id in entry["reference_ids"]):
            raise ValueError(f"离子 {ion_id} 缺少有效来源引用")
        expected_mobility = mobility_magnitude_from_diffusion(diffusion, charge, temperature)
        if not math.isclose(mobility, expected_mobility, rel_tol=2e-5, abs_tol=0.0):
            raise ValueError(f"离子 {ion_id} 的迁移率与扩散系数不一致")
    return library


def load_electrolyte_transport_database(path: str | Path) -> dict:
    """读取按性质分类的有限浓度电解质文献数据，不进行插值或模型换算。"""
    database_path = Path(path)
    with database_path.open("r", encoding="utf-8") as stream:
        database = json.load(stream)
    if not isinstance(database, dict) or not isinstance(database.get("datasets"), list):
        raise ValueError("电解质输运数据库必须包含 datasets 数组")
    if not database.get("database_id") or not database.get("schema_version"):
        raise ValueError("电解质输运数据库缺少 database_id 或 schema_version")

    references = database.get("references", {})
    seen_ids: set[str] = set()
    for dataset in database["datasets"]:
        if not isinstance(dataset, dict):
            raise ValueError("电解质输运数据集格式无效")
        dataset_id = dataset.get("dataset_id")
        if not dataset_id or dataset_id in seen_ids:
            raise ValueError("电解质输运数据集缺少唯一 dataset_id")
        seen_ids.add(dataset_id)
        if not dataset.get("property_type") or not dataset.get("data_status"):
            raise ValueError(f"数据集 {dataset_id} 缺少参数类型或数据状态")
        source_ids = dataset.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids or any(ref not in references for ref in source_ids):
            raise ValueError(f"数据集 {dataset_id} 缺少有效来源")

        points = dataset.get("points", [])
        if not isinstance(points, list):
            raise ValueError(f"数据集 {dataset_id} 的 points 必须是数组")
        for point in points:
            if not isinstance(point, dict):
                raise ValueError(f"数据集 {dataset_id} 包含无效数据点")
            value = point.get("value")
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"数据集 {dataset_id} 包含无效参数值")
            concentration_keys = (
                "concentration_mol_l",
                "concentration_mol_kg",
            )
            for key in concentration_keys:
                if key in point:
                    concentration = float(point[key])
                    if not math.isfinite(concentration) or concentration < 0:
                        raise ValueError(f"数据集 {dataset_id} 包含无效浓度")

        series_list = dataset.get("series", [])
        if not isinstance(series_list, list):
            raise ValueError(f"数据集 {dataset_id} 的 series 必须是数组")
        for series in series_list:
            if not isinstance(series, dict) or not series.get("electrolyte_formula"):
                raise ValueError(f"数据集 {dataset_id} 包含无效电解质序列")
            values = series.get("values_1e-9_m2_s")
            if not isinstance(values, list) or len(values) != len(dataset.get("point_concentrations_mol_l", [])):
                raise ValueError(f"数据集 {dataset_id} 的浓度和值数组长度不一致")
            for value in values:
                if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                    raise ValueError(f"数据集 {dataset_id} 包含无效互扩散系数")
    return database

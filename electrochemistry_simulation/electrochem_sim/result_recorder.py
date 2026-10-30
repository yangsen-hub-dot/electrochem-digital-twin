"""仿真结果记录：负责持久化，不参与仿真或物理计算。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


CHINA_TIMEZONE = timezone(timedelta(hours=8))


def _safe_filename_part(value: object) -> str:
    """Keep descriptive Unicode names while removing cross-platform unsafe characters."""
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", str(value).strip())
    part = re.sub(r"_+", "_", part).strip(" ._")
    return part or "未命名实验"


def _china_datetime(created_at_utc: str | None = None) -> datetime:
    if created_at_utc:
        value = created_at_utc[:-1] + "+00:00" if created_at_utc.endswith("Z") else created_at_utc
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)
    return timestamp.astimezone(CHINA_TIMEZONE)


def result_date_directory(output_dir: str | Path, created_at_utc: str | None = None) -> Path:
    """Return the date-organized directory, using the experiment's China-local date."""
    return Path(output_dir) / _china_datetime(created_at_utc).strftime("%Y-%m-%d")


def build_result_filename(
    category_zh: object,
    name_zh: object,
    created_at_utc: str | None,
    unique_id: object,
    extension: str,
) -> str:
    """Build a compact name; the containing YYYY-MM-DD directory carries the date."""
    suffix = str(extension).lstrip(".")
    short_id = re.sub(r"[^A-Za-z0-9]", "", str(unique_id))[:8]
    if not short_id:
        raise ValueError("结果文件必须包含非空唯一编号")
    time_tag = _china_datetime(created_at_utc).strftime("%H%M%S")
    return (
        f"{_safe_filename_part(category_zh)}_"
        f"{_safe_filename_part(name_zh)}_"
        f"{time_tag}_{short_id}.{suffix}"
    )


def save_result(result: dict, output_dir: str | Path) -> Path:
    config = result.get("config", {})
    directory = result_date_directory(output_dir, result.get("created_at_utc"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / build_result_filename(
        config.get("result_category_short_zh", "单"),
        config.get(
            "experiment_name_short_zh",
            config.get("experiment_name_zh", config.get("experiment_id", "实验运行")),
        ),
        result.get("created_at_utc"),
        result["run_id"],
        "json",
    )
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path

"""Read-only evidence gates used before offering an executable repair plan."""
import json
from collections import Counter
from pathlib import Path

from ..continuation import source_revisions
from .models import blocked


STATUSES = {"pass", "fail", "review", "uncovered"}
CORE_EVIDENCE = {"run.json", "video_manifest.json",
                 "process_validation/geometry.json", "process_validation/trace.jsonl"}


def report_rows(report):
    """Include failed summary checks as well as detailed issues; never drop either."""
    if not isinstance(report, dict):
        raise ValueError("校验报告不是对象")
    rows = []
    for key in ("checks", "issues"):
        values = report.get(key, [])
        if not isinstance(values, list):
            raise ValueError("校验报告的检查项或问题列表无效")
        for row in values:
            if (not isinstance(row, dict) or row.get("status") not in STATUSES
                    or not isinstance(row.get("group"), str)
                    or not isinstance(row.get("code"), str)):
                raise ValueError("校验报告包含无效检查项或未知结果状态")
        rows.extend(values)
    return rows


def validate_report_binding(folder, report, run_id, *, require_finished=True):
    """Copied/partial archives may be classified offline, but cannot authorize actions."""
    folder = Path(folder)
    if report.get("run_id") != run_id:
        return blocked(run_id, "校验报告与来源运行编号不一致。")
    try:
        rows = report_rows(report)
    except ValueError as exc:
        return blocked(run_id, str(exc))
    if not rows:
        return blocked(run_id, "校验报告没有检查项，不能据此规划修复。")
    counts = Counter(row["status"] for row in rows)
    expected_counts = {key: counts[key] for key in STATUSES}
    expected_status = next((key for key in ("fail", "review", "uncovered")
                            if counts[key]), "pass")
    if report.get("counts") != expected_counts or report.get("status") != expected_status:
        return blocked(run_id, "校验报告的汇总与逐项结果不一致，请重新检查。")
    revisions = source_revisions(folder)
    if (not CORE_EVIDENCE.issubset(revisions)
            or report.get("source_revisions") != revisions):
        return blocked(run_id, "校验报告未绑定当前完整证据或证据已变化，请重新检查。")
    audit_path = folder / "process_validation/audit_status.json"
    if audit_path.exists() and require_finished:
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return blocked(run_id, "无法读取独立检查状态。")
        if not isinstance(audit, dict) or audit.get("status") != "finished":
            return blocked(run_id, "独立检查尚未正常结束，不能生成修复候选。", "waiting")
    return None

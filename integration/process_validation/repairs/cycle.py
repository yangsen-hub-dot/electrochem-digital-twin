"""Persist the detect/classify/solve/execute/recheck trace beside each repair run."""
import json
from pathlib import Path

from ..report import atomic_json
from .effects.storage import evaluate_repair


def record_recheck(run_dir, report):
    run_dir = Path(run_dir).resolve()
    try:
        manifest = json.loads((run_dir / "video_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    correction = manifest.get("correction")
    if not isinstance(correction, dict):
        return None
    stages = [dict(row) for row in correction.get("pipeline_stages", [])
              if isinstance(row, dict)]
    check = {
        "stage": "check", "status": "completed",
        "checker": "independent_process_validation",
        "report_status": report.get("status"),
        "policy_id": report.get("policy_id"),
        "counts": dict(report.get('counts', {})),
    }
    for index, row in enumerate(stages):
        if row.get("stage") == "check":
            stages[index] = {**row, **check}
            break
    else:
        stages.append(check)
    result = {
        "schema_version": "1.0.0", "run_id": run_dir.name,
        "source_run_id": correction.get("source_run_id"),
        "root_run_id": correction.get("root_run_id"),
        "candidate_id": correction.get("candidate_id"),
        "strategy_family": correction.get("strategy_family"),
        "fault_step_ids": correction.get("fault_step_ids", []),
        "repair_contract": correction.get("repair_contract"),
        "stages": stages, "recheck": check,
        "effectiveness": evaluate_repair(run_dir, report, during_audit=True),
        "interpretation": "局部修复分支的独立复验结果；不替代完整流程验收或账本。",
    }
    atomic_json(run_dir / "process_validation" / "repair_cycle.json", result)
    return result

"""Small JSON report contract, shared by CLI and web (no experiment mutations)."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os

LABELS = {"pass": "通过", "fail": "异常", "review": "待复核", "uncovered": "未覆盖"}


class Report:
    def __init__(self, run_id, policy):
        self.data = {"schema_version": "1.0", "run_id": run_id,
                     "created_at_utc": datetime.now(timezone.utc).isoformat(),
                     "policy_id": policy["policy_id"], "checks": [], "issues": [],
                     "threshold_basis": policy["threshold_basis"],
                     "scope": "read_only_simulation_process_validation",
                     "does_not_modify_experiment": True}
        self._issues = {}

    def check(self, group, code, status, message, **details):
        self.data["checks"].append(dict(group=group, code=code, status=status,
                                       message=message, **details))

    def issue(self, group, code, status, message, **details):
        key = (group, code, details.get("step_id"), details.get("body"), details.get("obstacle"),
               details.get('container'), details.get('species'), details.get('field'))
        if key in self._issues:
            row = self._issues[key]
            row["occurrences"] += 1
            if "frame_index" in details:
                row["last_frame_index"] = details["frame_index"]
                row["last_video_time_s"] = details.get("video_time_s")
            return
        row = dict(group=group, code=code, status=status, message=message,
                   occurrences=1, **details)
        self._issues[key] = row
        self.data["issues"].append(row)

    def finish(self):
        rows = self.data["checks"] + self.data["issues"]
        counts = {key: sum(row["status"] == key for row in rows) for key in LABELS}
        status = next((key for key in ("fail", "review", "uncovered") if counts[key]), "pass")
        self.data.update(status=status, status_label=LABELS[status], counts=counts,
                         all_required_checks_passed=status == "pass")
        return self.data


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def write_report(root, data):
    root = Path(root)
    atomic_json(root / "report.json", data)
    lines = ["# 仿真过程自动校验", "", f"运行编号：{data['run_id']}",
             f"结论：{data['status_label']}；规则：{data['policy_id']}", "",
             "报告独立于原实验状态，检查不会改写原回执、液量、浓度或误差抽样。", ""]
    if data.get("recommendations"):
        lines.extend(["## 处理建议", ""])
        for item in data["recommendations"]:
            where = f" · {item['step_id']}" if item.get("step_id") else ""
            lines.append(f"- [{item['mode']}] {item['recommendation']}{where}")
        lines.append("")
    for title, key in (("异常与待核对位置", "issues"), ("检查范围与结果", "checks")):
        lines.extend(["## " + title, ""])
        for item in data[key]:
            location = " · ".join(str(item[k]) for k in ("step_id", "video_time_s", "body", "obstacle") if item.get(k) is not None)
            lines.append(f"- [{LABELS[item['status']]}] {item['message']}" + ("；" + location if location else ""))
        lines.append("")
    (root / "报告.md").write_text("\n".join(lines), encoding="utf-8")

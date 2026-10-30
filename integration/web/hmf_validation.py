"""Read-only HMF numerical-run checks; not mechanical or chemical validation."""
from __future__ import annotations
from process_validation.repairs.hmf.planner import build_plan


def assess_run(result):
    checks, issues = [], []
    records = result.get("steps", [])
    failure = result.get("failure")
    repair = result.get('repair')
    if isinstance(repair, dict):
        verification = repair.get('verification', {})
        if verification.get('target_resolved'):
            checks.append('原失败步骤已局部重跑并通过数值复验；后续步骤尚未执行')
        else:
            issues.append({'kind':'repair_unverified','message':'局部修复未通过复验',
                           'suggestion':'查看失败原因，停止自动续跑。'})
        return {'checks':checks,'issues':issues,'automatic_repair':'local_segment_only',
                'repair_plan':None,'verification':verification,
                'not_checked':['真实机器人轨迹与碰撞','电极浸入深度','电解后物料与反应守恒','HPLC 和真实设备']}
    repair_plan = build_plan(result) if failure else None
    if failure:
        issues.append({"kind": (repair_plan.get('family') if repair_plan and repair_plan.get('eligible') else 'workflow_failure'), "step": failure.get("step_id"),
                       "message": failure.get("reason"), "suggestion": "检查失败步骤及其前置条件后重新运行。"})
    else:
        checks.append("数值步骤已完成")
    if result.get("conservation", {}).get("passed"):
        checks.append("已记录的配液库存守恒")
    else:
        issues.append({"kind": "inventory", "message": "记录库存守恒未通过或缺失", "suggestion": "核对配液记录。"})
    electrolysis = [r.get("event", {}) for r in records if r.get("status") == "completed" and r.get("event", {}).get("action") == "electrolyze"]
    if electrolysis:
        prediction = electrolysis[-1].get("prediction", {})
        if prediction.get("valid"):
            checks.append("终点预测在当前模型的参考范围内")
        else:
            issues.append({"kind": "prediction_unavailable", "message": prediction.get("reason", "模型范围不足"),
                           "suggestion": "返回参考条件或补充该条件下的实验数据，再重新验证。"})
    else:
        issues.append({"kind": "no_electrolysis", "message": "尚无成功的电解数值事件", "suggestion": "检查流程是否到达电解步骤。"})
    return {"checks": checks, "issues": issues, "automatic_repair": "local_precondition_only",
            "repair_plan": repair_plan,
            "not_checked": ["真实机器人轨迹与碰撞", "电极浸入深度", "电解后物料与反应守恒", "HPLC 和真实设备"]}

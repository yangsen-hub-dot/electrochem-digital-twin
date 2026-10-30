"""Rule-based next-step suggestions from independent validation evidence."""

from __future__ import annotations


RULES = {
    ("geometry", "vertical_grasp_axis"): (
        "检查夹爪闭合轴与目标姿态；只有姿态证据明确且前缀可恢复时，才生成局部候选并复验。",
        "candidate_if_eligible"),
    ("geometry", "upward_grasp_axis"): (
        "检查夹爪接近轴和指端朝向；先做高位姿态预检，不通过时不要闭爪。",
        "candidate_if_eligible"),
    ("geometry", "fixture_intersection"): (
        "依据相交帧和支架几何调整局部进退路径；若终点几何本身不成立，转为场景几何修复。",
        "candidate_if_eligible"),
    ("geometry", "between_frames"): (
        "增加路径中间位姿/连续碰撞证据；当前离散采样不能判定安全通过。",
        "review_required"),
    ("ledger", "conservation"): (
        "暂停后续步骤，核对最近成功回执中的实际量、枪头残液和库存，再从最后一个确定状态恢复。",
        "reconcile_before_resume"),
    ("ledger", "invalid_volume"): (
        "核对对应容器和转移回执；液量恢复为有效且守恒通过前，不继续后续化学步骤。",
        "reconcile_before_resume"),
    ("ledger", "ledger_revision"): (
        "对照成功回执重建状态版本；账本与回执重新一致前，不创建修复分支。",
        "reconcile_before_resume"),
    ("sync", "confirmed_ledger"): (
        "修正画面数据与成功回执的时间绑定；不改化学账本或重复提交动作。",
        "fix_observation_binding"),
    ("sync", "frame_binding"): (
        "检查录像帧、动作阶段和仿真时钟的关联记录，再重新校验该运行。",
        "fix_observation_binding"),
    ("records", "execution_failed"): (
        "根据失败回执和末端轨迹定位动作阶段；只有已登记规则支持且有确定检查点时才做局部修复。",
        "candidate_if_eligible"),
    ("video", "frozen_while_moving"): (
        "先检查编码帧与轨迹是否同步；该指标是待复核信号，不直接证明机械臂停住或碰撞。",
        "review_required"),
    ("video", "black_screen"): (
        "检查相机、遮挡和编码画面，再复核同一时间点的场景轨迹。",
        "review_required"),
}


def build_recommendations(issues: list[dict]) -> list[dict]:
    """Attach conservative, evidence-linked guidance without launching work."""
    recommendations = []
    seen = set()
    for issue in issues:
        key = (issue.get("group"), issue.get("code"), issue.get("step_id"))
        if key in seen:
            continue
        seen.add(key)
        guidance, mode = RULES.get(key[:2], (
            "保留原运行与证据，补充问题对应的独立检查规则后再判断是否局部修复。",
            "diagnostic_only"))
        recommendations.append({
            "issue_group": key[0], "issue_code": key[1], "step_id": key[2],
            "status": issue.get("status"), "mode": mode,
            "recommendation": guidance,
            "evidence": {name: issue[name] for name in
                         ("frame_index", "last_frame_index", "video_time_s", "body", "obstacle")
                         if name in issue},
            "starts_simulation": False,
        })
    return recommendations

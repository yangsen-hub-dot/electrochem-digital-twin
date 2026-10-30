"""Protocol, receipts, ledger and mixed-duration checks on existing records."""
import math


def check_unified_state_ledger(snapshot, successful, archive, report):
    ledger = snapshot.get("state_ledger")
    if not isinstance(ledger, dict):
        report.check("ledger", "unified_state_ledger", "uncovered",
                     "旧运行档案没有统一状态账本；保留原记录，不补造化学/设备状态证据")
        return
    if ledger.get("schema_version") != "1.0.0":
        report.issue("ledger", "ledger_schema", "fail", "统一状态账本版本缺失或不受支持")
        return

    completed_ids = [row.get("receipt", {}).get("step_id") for row in successful]
    workflow = ledger.get("workflow", {})
    if (ledger.get("revision") != snapshot.get("state_revision")
            or workflow.get("completed_step_ids") != completed_ids):
        report.issue("ledger", "ledger_revision", "fail",
                     "统一账本版本/成功步骤与实验状态回执不一致")
    else:
        report.check("ledger", "ledger_revision", "pass",
                     f"统一账本覆盖 {len(completed_ids)} 个已确认步骤，版本与回执一致")

    provenance = ledger.get("provenance", [])
    successful_provenance = [row for row in provenance if row.get("outcome") == "succeeded"]
    provenance_ids = [row.get("step_id") for row in successful_provenance]
    provenance_revisions = [row.get("state_revision") for row in successful_provenance]
    if (provenance_ids != completed_ids
            or provenance_revisions != list(range(1, len(completed_ids) + 1))):
        report.issue("ledger", "ledger_provenance", "fail",
                     "状态账本来源记录的步骤/版本与成功回执顺序不一致")
    else:
        report.check("ledger", "ledger_provenance", "pass",
                     "每个成功步骤均有对应的账本来源记录")

    state_model = snapshot.get("state", {}).get("chemical_model", {})
    ledger_model = ledger.get("chemical_model", {})
    if (not ledger_model.get("model_id") or not ledger_model.get("model_family")
            or not ledger_model.get("state_schema_version")
            or state_model.get("model_id") != ledger_model.get("model_id")):
        report.issue("ledger", "chemical_model_identity", "fail",
                     "运行状态与统一账本中的化学模型编号不一致")
    else:
        report.check("ledger", "chemical_model_identity", "pass",
                     f"化学模型已版本化记录：{ledger_model['model_id']}")

    event_receipts = [event.get("payload", {}).get("executor_receipt", {})
                      for event in archive.get("events", [])
                      if event.get("type") in {"step.completed", "step.failed"}]
    last_observation = next((receipt.get("state_observation") for receipt in reversed(event_receipts)
                             if isinstance(receipt.get("state_observation"), dict)), None)
    device_state = ledger.get("device_state", {})
    if last_observation is not None and device_state.get("observation") != last_observation:
        report.issue("ledger", "device_observation", "fail",
                     "统一账本设备末态与最近成功回执的观测不一致")
    elif last_observation is not None:
        report.check("ledger", "device_observation", "pass",
                     "设备末态关联到最近成功回执和轨迹证据")

    entities = ledger.get("entities", {})
    relations = ledger.get("relations", [])
    if not isinstance(entities, dict) or not isinstance(relations, list):
        report.issue("ledger", "entity_graph", "fail", "实体/关系账本结构无效")
    else:
        active_subjects = [row.get("subject") for row in relations if row.get("active")]
        if any(subject not in entities for subject in active_subjects):
            report.issue("ledger", "entity_graph", "fail", "活动对象关系引用了未登记实体")
        elif len(active_subjects) != len(set(active_subjects)):
            report.issue("ledger", "entity_graph", "fail", "同一实体存在多个互斥的活动位置关系")
        else:
            report.check("ledger", "entity_graph", "pass",
                         f"实体注册与 {len(relations)} 条关系历史结构有效")

    lineage = ledger.get("lineage", {})
    if lineage:
        if (lineage.get("run_id") != report.data["run_id"]
                or not lineage.get("source_run_id")
                or not lineage.get("checkpoint_step_id")
                or lineage.get("restored_from_confirmed_prefix") is not True
                or lineage.get("physical_actions_replayed") is not False):
            report.issue("ledger", "repair_lineage", "fail",
                         "局部修复/续跑没有有效的来源运行与确认检查点关系")
        else:
            report.check("ledger", "repair_lineage", "pass",
                         "新分支关联来源运行与确认检查点，未重播已完成的物理动作")
            if lineage.get("branch_type") == "local_correction":
                contract = lineage.get("repair_contract") or {}
                if (contract.get("source_run_id") != lineage.get("source_run_id")
                        or contract.get("checkpoint_step_id") != lineage.get("checkpoint_step_id")
                        or contract.get("checkpoint_revision") != lineage.get("checkpoint_revision")
                        or contract.get("reuse_confirmed_prefix") is not True
                        or contract.get("replay_previous_physical_actions") is not False):
                    report.issue("ledger", "repair_contract", "fail",
                                 "局部修复没有与账本检查点一致的修复契约")
                else:
                    report.check("ledger", "repair_contract", "pass",
                                 "修复分支保留条件/种子/已确认前缀并绑定原校验策略")

    certainty = ledger.get("certainty", {})
    if snapshot.get("state_uncertain") is True and certainty.get("chemical", {}).get("status") != "uncertain":
        report.issue("ledger", "uncertainty_tracking", "fail",
                     "运行已标记状态不确定，但账本没有标记化学状态不确定")
    elif certainty.get("chemical", {}).get("status"):
        report.check("ledger", "uncertainty_tracking", "pass",
                     "化学、机械、流程和测量状态分别记录来源/确定性")


def check_records(archive, manifest, report):
    snapshot = archive.get("snapshot", {})
    run_id = report.data["run_id"]
    if snapshot.get("run_id") != run_id or manifest.get("run_id") != run_id:
        report.issue("records", "run_identity", "fail", "录像、回执与当前运行编号不一致")
        return
    report.check("records", "run_identity", "pass", "录像与动作档案运行编号一致")
    plan = snapshot.get("plan", [])
    expected = [row["step_id"] for row in plan]
    receipts = sorted(archive.get("accepted_receipts", {}).values(), key=lambda row: row["result"]["state_revision"])
    successful = [row for row in receipts if row.get("receipt", {}).get("status") == "succeeded"]
    check_unified_state_ledger(snapshot, successful, archive, report)
    ids = [row["receipt"]["step_id"] for row in successful]
    if ids != expected[:len(ids)] or len(ids) != len(set(ids)):
        report.issue("records", "protocol_order", "fail", "成功动作没有按实验协议顺序执行，或存在重复提交")
    else:
        report.check("records", "protocol_order", "pass", f"已确认的 {len(ids)} 步与协议前缀一致")
    if snapshot.get("completed_steps") != len(ids):
        report.issue("records", "step_count", "fail", "已完成步数与成功回执数不一致")
    full = snapshot.get("full_experiment_completed") is True
    if full and len(ids) != len(expected):
        report.issue("records", "false_completion", "fail", "完整成功标记与成功回执数量冲突")
    elif (full and manifest.get("status") == "segment_diagnostic_ready"
          and manifest.get("correction")):
        report.check("records", "scope", "review",
                     "修复分支重建了已确认前缀并执行后缀；本段录像只覆盖局部修复过程")
    elif full and manifest.get("status") != "ready":
        report.issue("records", "false_completion", "fail", "完整成功标记与录像终态冲突")
    elif not full:
        diagnostic = manifest.get("diagnostic")
        state = "review" if diagnostic and manifest.get("status") == "segment_diagnostic_ready" else "fail"
        report.check("records", "scope", state, "局部诊断片段，不能作为完整实验验收" if state == "review" else "实验未完整完成，保留原暂停/失败状态")
    else:
        report.check("records", "scope", "pass",
                     "协议链已完成；本片为修正后的续跑录像，前段证据分别保留"
                     if manifest.get('continuation') else "完整协议、成功回执与完整录像终态一致")
    for event in archive.get("events", []):
        if event.get("type") in {"step.failed", "run.paused"}:
            p = event.get("payload", {})
            reason = p.get("reason") or p.get("executor_receipt", {}).get("reason") or "原动作失败"
            report.issue("records", "execution_failed", "fail", str(reason), step_id=event.get("step_id"), simulation_time_s=event.get("simulation_time_s"))
    last_time, last_revision = -1.0, 0
    for row in successful:
        receipt, state = row["receipt"], row["result"]
        revision, when = state["state_revision"], receipt["simulation_time_s"]
        if revision != last_revision+1 or not math.isfinite(when) or when < last_time:
            report.issue("records", "receipt_sequence", "fail", "回执版本或仿真时间不连续", step_id=receipt["step_id"])
        last_revision, last_time = revision, when
        if state.get("conservation", {}).get("passed") is not True:
            report.issue("ledger", "conservation", "fail", "成功回执后的物料守恒未通过", step_id=receipt["step_id"])
        for container, liquid in state.get("state", {}).get("containers", {}).items():
            volume = liquid.get("volume_ul")
            if volume is not None and (not math.isfinite(volume) or volume < -1e-8):
                report.issue("ledger", "invalid_volume", "fail", "液量为负数或非有限值", step_id=receipt["step_id"], container=container)
    if snapshot.get("conservation", {}).get("passed") is True:
        report.check("ledger", "conservation", "pass", "读取到的逐步与终态物料守恒记录通过")
    levels = snapshot.get("error_model_realized", {})
    events = archive.get("events", [])
    for name, action in (("pipetting", "aspirate"), ("tip_carryover", "dispense"), ("mixing", "mix")):
        if not levels.get("enabled"):
            report.check("errors", name, "pass", f"本次过程误差关闭：{name}")
            continue
        factor = levels.get(name)
        matching = [e for e in events if e.get("action") in {action, "mix_pair" if action == "mix" else action} and e.get("type") == "step.completed"]
        report.check("errors", name, "pass" if factor and matching else "uncovered",
                     f"{name} 参数与已执行事件存在" if factor and matching else f"{name} 尚未执行或旧档案缺明细；不补造数据")
    mixing = levels.get("mixing", {})
    if mixing.get("model") == "truncated_gaussian_rate_per_second" and levels.get("enabled"):
        duration = mixing.get("sampled_duration_s")
        rates = mixing.get("sampled_paired_rates_s_inv") or ({"single": mixing["sampled_rate_s_inv"]} if mixing.get("sampled_rate_s_inv") is not None else {})
        efficiencies = mixing.get("sampled_paired_efficiencies") or {}
        if rates and duration is not None:
            for side, rate in rates.items():
                expected_eta = -math.expm1(-rate*duration)
                actual = efficiencies.get(side)
                if actual is not None and not math.isclose(actual, expected_eta, abs_tol=1e-12):
                    report.issue("errors", "mixing_formula", "fail", "混匀效率与抽样速率、时长不一致", side=side)
            report.check("errors", "mixing_duration", "pass", f"混匀时长已记录：{duration:g} s；按抽样 k 核对效率")
        else:
            report.check("errors", "mixing_duration", "uncovered", "尚无已执行混匀的速率/时长数据")

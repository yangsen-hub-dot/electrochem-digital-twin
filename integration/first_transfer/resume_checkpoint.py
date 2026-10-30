"""Reconstruct accepted state deterministically; never replay physical actions."""
from copy import deepcopy
import math


def restore_confirmed_prefix(session, archive, checkpoint_step_id, *, require_terminal=False,
                             candidate_id=None, branch_type="local_repair",
                             repair_contract=None):
    completed = [e for e in archive.get("events", []) if e.get("type") == "step.completed"]
    matches = [i for i, e in enumerate(completed) if e.get("step_id") == checkpoint_step_id]
    if len(matches) != 1:
        raise ValueError("恢复档案缺少唯一的成功检查点")
    prefix = completed[:matches[0] + 1]
    if require_terminal:
        saved = archive.get("snapshot", {})
        if (archive.get("pending_command") or saved.get("state_uncertain") is not False
                or len(prefix) != len(completed) or saved.get("completed_steps") != len(prefix)):
            raise ValueError("续跑来源没有结束在确定的成功检查点")
        if session.config != archive.get("config"):
            raise ValueError("当前模型或实验配置与修正记录不同")
    for event in prefix:
        command = session.prepare()
        if command["step_id"] != event.get("step_id"):
            raise ValueError("恢复步骤顺序与当前动作计划不一致")
        payload = event.get("payload", {})
        receipt = deepcopy(payload.get("executor_receipt"))
        if not isinstance(receipt, dict) or receipt.get("status") != "succeeded":
            raise ValueError("成功步骤缺少可复用执行回执")
        source_command_id = receipt.get("command_id")
        if not isinstance(source_command_id, str) or not source_command_id:
            raise ValueError("成功步骤回执缺少原始 command_id")
        prior_volume = payload.get("actual_volume_ul")
        if (prior_volume is not None and command.get("actual_volume_ul") is not None
                and not math.isclose(float(prior_volume), float(command["actual_volume_ul"]),
                                     rel_tol=0., abs_tol=1e-7)):
            raise ValueError("重建的液量与历史回执不一致")
        # Keep source command identities during deterministic reconstruction.
        # Disposable-tip entities in the unified ledger are keyed by the
        # original load_tip command_id; replacing it creates false relation
        # mismatches even though the accepted physical history is unchanged.
        command["command_id"] = source_command_id
        session.pending["command"]["command_id"] = source_command_id
        prepared_event = session.events[-1] if session.events else None
        if (prepared_event and prepared_event.get("type") == "step.prepared"
                and prepared_event.get("step_id") == event.get("step_id")
                and isinstance(prepared_event.get("payload"), dict)):
            prepared_event["payload"]["command_id"] = source_command_id
        receipt["run_id"] = session.run_id
        session.accept_receipt(receipt)
    if require_terminal:
        restored, saved = session.snapshot(), archive["snapshot"]
        for field in ("state", "error_model_realized", "state_revision", "completed_steps",
                      "plan", "conditions", "simulation_time_s", "conservation"):
            if restored.get(field) != saved.get(field):
                raise ValueError("续跑检查点重建不一致：" + field)
    source_ledger = archive.get("snapshot", {}).get("state_ledger")
    if isinstance(source_ledger, dict):
        checkpoint_revision = len(prefix)
        if source_ledger.get("schema_version") != session.state.ledger.schema_version:
            raise ValueError("来源状态账本结构版本与当前恢复器不一致")
        if (isinstance(source_ledger.get("revision"), bool)
                or not isinstance(source_ledger.get("revision"), int)
                or source_ledger["revision"] < checkpoint_revision):
            raise ValueError("来源状态账本没有覆盖所选已确认检查点")
        provenance = source_ledger.get("provenance", [])
        source_prefix = [row for row in provenance
                         if row.get("outcome") == "succeeded"
                         and row.get("state_revision", 0) <= checkpoint_revision]
        source_prefix_steps = [row.get("step_id") for row in source_prefix]
        expected_prefix_steps = [event.get("step_id") for event in prefix]
        workflow_steps = source_ledger.get("workflow", {}).get("completed_step_ids", [])
        if (source_prefix_steps != expected_prefix_steps
                or workflow_steps[:checkpoint_revision] != expected_prefix_steps):
            raise ValueError("来源状态账本的步骤来源与成功回执前缀不一致")
        # Relations are stored at the latest state. Reconstruct their history
        # as it existed at the selected revision: retain earlier inactive rows,
        # omit later rows, and hide validity intervals that begin after it.
        source_relations = []
        for relation in source_ledger.get("relations", []):
            valid_from = relation.get("valid_from_revision")
            valid_until = relation.get("valid_until_revision")
            if not isinstance(valid_from, int) or valid_from > checkpoint_revision:
                continue
            as_of_checkpoint = deepcopy(relation)
            if valid_until is None or valid_until > checkpoint_revision:
                as_of_checkpoint["active"] = True
                as_of_checkpoint["valid_until_revision"] = None
            else:
                as_of_checkpoint["active"] = False
            source_relations.append(as_of_checkpoint)
        restored_relations = session.state.ledger.relations
        normalize_relations = lambda rows: [
            {key: value for key, value in row.items() if key != "command_id"}
            for row in rows
        ]
        if normalize_relations(source_relations) != normalize_relations(restored_relations):
            raise ValueError("恢复后的对象关系与来源状态账本不一致")
        session.state.ledger.lineage["root_run_id"] = (
            source_ledger.get("lineage", {}).get("root_run_id")
            or archive.get("snapshot", {}).get("run_id")
        )
    session.state.ledger.set_lineage(
        run_id=session.run_id, source_run_id=archive.get("snapshot", {}).get("run_id"),
        checkpoint_step_id=checkpoint_step_id, checkpoint_revision=len(prefix),
        candidate_id=candidate_id, branch_type=branch_type,
        repair_contract=repair_contract,
    )
    # The runner offsets its new physics clock by this confirmed logical time.
    # Keep the same baseline so the first new receipt advances chemistry only
    # by the new action duration, rather than counting the prefix time again.
    session._last_model_time_s = session.simulation_time_s
    return session.snapshot()

"""Authenticated experiment coordinator, saved history and bounded cloud batches."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


DEFAULT_CONDITIONS = {
    "left_concentration_m": 0.01, "right_concentration_m": 0.1,
    "left_volume_ml": 1.0, "right_volume_ml": 1.0,
    "temperature_c": 25.0, "salt_bridge_type": "KNO3",
    "salt_bridge_concentration_m": 0.1, "activity_correction_enabled": True,
    "liquid_junction_enabled": False, "errors_enabled": True,
    "operation_error_levels": {"pipetting": "low", "tip_carryover": "low", "mixing": "low"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


class BusyError(ValueError):
    pass


class ExperimentCoordinator:
    def __init__(self, engine_root, runner, python, runs_root, jobs_root, launcher=subprocess.Popen):
        integration_root = str(Path(__file__).resolve().parents[1])
        if integration_root not in sys.path:
            sys.path.insert(0, integration_root)
        self.engine_root = Path(engine_root).resolve()
        sys.path.insert(0, str(self.engine_root)) if str(self.engine_root) not in sys.path else None
        self.simulator = importlib.import_module("electrochem_sim.simulator")
        self.input = importlib.import_module("electrochem_sim.condition_input")
        self.state_module = importlib.import_module("electrochem_sim.state")
        self.base_config = self.simulator.load_config(self.engine_root / "config" / "copper_concentration_cell.json")
        self.runner = Path(runner).resolve()
        sys.path.insert(0, str(self.runner.parent)) if str(self.runner.parent) not in sys.path else None
        self.measurement_steps = importlib.import_module('measurement_session').measurement_steps
        self.expand_pipette_tool_actions = importlib.import_module('pipette_workflow').expand_pipette_tool_actions
        self.python = str(python)
        self.runs_root = Path(runs_root).resolve()
        self.jobs_root = Path(jobs_root).resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.launcher = launcher
        self.lock = threading.RLock()
        self.active = None
        self.process = None
        self.jobs = {}
        self.requests = {}
        for path in self.jobs_root.glob("*.job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if not re.fullmatch(r"[a-f0-9]{32}", job["run_id"]):
                    continue
                if job["status"] in {"starting", "running", "finalizing"}:
                    # Never restart an unconfirmed physical/virtual execution.
                    job["status"] = "paused"
                    job["failure"] = "协调服务已重启，需核对原执行器和最后确认状态后再继续"
                if job.get('reviewed_segment') and self._completed_reviewed_segment(job['run_id'], job):
                    job['status'] = 'awaiting_validation'
                    job['failure'] = None
                elif job.get('correction') and self._terminal_correction_verified(job['run_id']):
                    job['status'] = 'completed'
                    job['failure'] = None
                elif job.get('correction') and self._completed_correction(job['run_id']):
                    job['status'] = 'awaiting_validation'
                    job['failure'] = None
                self.jobs[job["run_id"]] = job
                prior=self.jobs.get(self.requests.get(job['request_id']))
                if prior is None or (job.get('created_at_utc',''),job['run_id'])>(
                        prior.get('created_at_utc',''),prior['run_id']):
                    self.requests[job["request_id"]] = job["run_id"]
            except (OSError, ValueError, KeyError):
                continue
        if self.jobs:
            self.active = max(self.jobs.values(), key=lambda j: j["created_at_utc"])["run_id"]
        self.profile = self._profile()

    def _profile(self):
        settings = self.base_config.get("error_model", {})
        profile_file = settings.get("profile_file")
        if not profile_file:
            return None
        catalog = json.loads((self.engine_root / "config" / profile_file).read_text(encoding="utf-8"))
        result = {
            "tested_volume_bands_ul": catalog.get("applicability", {}).get("tested_volume_bands_ul", []),
            "applicability": catalog.get("applicability", {}),
        }
        for factor, choice in settings.get("factors", {}).items():
            model = catalog.get("models", {}).get(factor, {}).get(choice.get("model"), {})
            result[factor] = {}
            for level, parameters in model.items():
                if not isinstance(parameters, dict):
                    continue
                if factor == "pipetting":
                    effective = {**model.get(parameters.get("inherits"), {}), **parameters}
                    result[factor][level] = {
                        key: value for key, value in effective.items() if key != "inherits"
                    }
                else:
                    result[factor][level] = {"range": parameters.get(
                        "residual_volume_ul_range" if factor == "tip_carryover" else
                        "rate_range_s_inv" if factor == "mixing" and "rate_range_s_inv" in parameters else "efficiency_range")}
        return result

    def _save_job(self, job):
        atomic_json(self.jobs_root / (job["run_id"] + ".job.json"), job)

    def _read_archive(self, run_id):
        try:
            result = json.loads((self.runs_root / run_id / "run.json").read_text(encoding="utf-8"))
            if result["snapshot"]["run_id"] != run_id:
                raise ValueError("运行编号不一致")
            return result
        except (OSError, ValueError, KeyError):
            return None

    def _completed_correction(self, run_id):
        archive = self._read_archive(run_id)
        snapshot = (archive or {}).get('snapshot', {})
        try:
            manifest = json.loads((self.runs_root / run_id / 'video_manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return bool(manifest.get('run_id') == run_id and manifest.get('correction')
                    and not manifest.get('continuation')
                    and manifest.get('status') == 'segment_diagnostic_ready'
                    and snapshot.get('state_uncertain') is False
                    and not archive.get('pending_command'))

    def _terminal_correction_verified(self, run_id):
        from process_validation.terminal_completion import terminal_correction_verified
        return terminal_correction_verified(self.runs_root / run_id)

    def _completed_reviewed_segment(self, run_id, job):
        archive = self._read_archive(run_id)
        try:
            manifest = json.loads((self.runs_root / run_id / 'video_manifest.json').read_text(encoding='utf8'))
        except (OSError, ValueError):
            return False
        segment = job.get('reviewed_segment') or {}
        diagnostic = manifest.get('diagnostic') or {}
        snapshot = (archive or {}).get('snapshot', {})
        return bool(manifest.get('run_id') == run_id
                    and manifest.get('status') == 'segment_diagnostic_ready'
                    and diagnostic.get('source_run_id') == segment.get('source_run_id')
                    and diagnostic.get('stop_after_step') == segment.get('stop_after_step')
                    and diagnostic.get('executed_step_ids') == segment.get('next_step_ids')
                    and snapshot.get('state_uncertain') is False and not archive.get('pending_command'))

    def _assert_renderer_idle(self):
        self._reap()
        if self.process is not None or any(job.get('status') in {'starting', 'running', 'finalizing'}
                                           for job in self.jobs.values()):
            raise BusyError('当前仿真执行器仍在运行，请等待本次动作和录像结束')
        # A service restart must not make a surviving renderer invisible.
        proc = Path('/proc')
        if proc.is_dir():
            for path in proc.glob('[0-9]*/cmdline'):
                try:
                    argv = path.read_bytes().decode(errors='replace').split('\0')
                except OSError:
                    continue
                if '--run-id' in argv and any(Path(arg).name == self.runner.name for arg in argv):
                    raise BusyError('仍有仿真执行器进程，暂不能启动另一段实验')

    @staticmethod
    def _workflow_flags(actions):
        kinds = {row.get('action') for row in actions}
        flags = []
        for kind, flag in (('mix_pair', '--include-paired-mix'), ('mix', '--include-left-mix'),
                           ('connect_salt_bridge', '--include-salt-bridge'),
                           ('measure_voltage', '--include-measurement')):
            if kind in kinds:
                flags.append(flag)
        for side in ('left', 'right'):
            if any(row.get('step_id') == f'ui_place_{side}_copper_electrode' for row in actions):
                flags.append(f'--include-{side}-electrode')
        return flags

    def _reap(self):
        if not self.process or not self.active:
            job = self.jobs.get(self.active) if self.active else None
            if job and job.get('status') == 'awaiting_validation' and self._terminal_correction_verified(self.active):
                job['status'] = 'completed'
                job['failure'] = None
            self._advance_auto_correction()
            return
        code = self.process.poll()
        if code is None:
            return
        job = self.jobs[self.active]
        archive = self._read_archive(self.active)
        snapshot = archive.get("snapshot", {}) if archive else {}
        if code == 0 and job.get('reviewed_segment') and self._completed_reviewed_segment(self.active, job):
            job['status'] = 'awaiting_validation'
            job['failure'] = None
        elif code == 0 and job.get('correction') and self._completed_correction(self.active):
            job['status'] = 'awaiting_validation'
            job['failure'] = None
        elif code == 0 and snapshot.get("status") == "completed" and snapshot.get("video", {}).get("status") == "ready":
            job["status"] = "completed"
        elif snapshot.get("status") == "paused":
            job["status"] = "paused"
            job["failure"] = "动作已部分执行或状态无法核对；已暂停"
        else:
            job["status"] = "failed"
            job["failure"] = (snapshot.get('failure') or snapshot.get('video', {}).get('reason')
                              or f"云端执行未确认完成（退出码 {code}），请查看本次 runner.log")
        job["finished_at_utc"] = now()
        self._save_job(job)
        self.process = None
        self._advance_auto_correction()

    def _advance_auto_correction(self):
        """Run classify/solve after audit, then re-audit each bounded repair segment."""
        with self.lock:
            run_id = self.active
            job = self.jobs.get(run_id) if run_id else None
            if (not job or not job.get("auto_correction_chain")
                    or job.get("auto_correction_advanced")
                    or job.get("status") not in {"completed", "failed", "paused", "awaiting_validation"}):
                return
            source_dir = (self.runs_root / run_id).resolve()
            report_path = source_dir / "process_validation" / "report.json"
            audit_status_path = source_dir / "process_validation" / "audit_status.json"
            if not report_path.is_file() or not audit_status_path.is_file():
                return
            try:
                audit_status = json.loads(audit_status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if audit_status.get("status") != "finished":
                return
            cycle_path = source_dir / "process_validation" / "repair_cycle.json"
            if cycle_path.is_file():
                try:
                    job["repair_pipeline"] = json.loads(cycle_path.read_text(encoding="utf-8"))
                    self._save_job(job)
                except (OSError, ValueError):
                    pass
            try:
                from process_validation.repairs import build_repair_plan
                report = json.loads(report_path.read_text(encoding="utf-8"))
                plan = build_repair_plan(source_dir, report)
            except (OSError, ValueError) as exc:
                job["auto_correction_stop_reason"] = f"自动复验读取失败：{exc}"
                job["auto_correction_chain"] = False
                self._save_job(job)
                return
            if (not plan.get('eligible') and plan.get('status') == 'not_applicable'
                    and job.get('correction') and job.get('status') == 'awaiting_validation'):
                from process_validation.continuation import build_continuation_plan
                continuation = build_continuation_plan(source_dir)
                if continuation.get('eligible'):
                    # The independent audit and improvement gates authorize only
                    # the checked checkpoint. The suffix is a new recorded run,
                    # independently audited before choosing another repair.
                    job['auto_correction_advanced'] = True
                    self._save_job(job)
                    try:
                        next_state = self.start_continuation(
                            run_id, continuation['expected_revision'],
                            continuation['checkpoint_signature'], auto_chain=True)
                    except (OSError, ValueError, RuntimeError) as exc:
                        job['auto_correction_stop_reason'] = f'后续重验未能安全启动：{exc}'
                        job['auto_correction_chain'] = False
                        self._save_job(job)
                        return
                    job['auto_correction_run_id'] = next_state['run_id']
                    job['status'] = 'auto_correcting'
                    self._save_job(job)
                    return
                plan['reason'] = continuation.get('reason') or '续跑检查点尚未通过独立复验'
            if not plan.get("eligible"):
                pipeline_state = job.setdefault("repair_pipeline", {})
                pipeline_state["recheck"] = {
                    "status": "completed", "result": report.get("status"),
                    "failed_items": sum(row.get("status") == "fail"
                                         for row in report.get("checks", []) + report.get("issues", [])),
                    "blocked_reason": plan.get("reason"),
                }
                pipeline_state["next_action"] = "stop"
                job["auto_correction_stop_reason"] = plan.get("reason") or "当前证据不支持继续自动修正"
                job["auto_correction_chain"] = False
                self._save_job(job)
                return
            # Mark before launching: start_correction re-enters _reap through
            # its idle check, and must not schedule this same attempt twice.
            job["auto_correction_advanced"] = True
            job.setdefault("repair_pipeline", {})["next_action"] = "start_local_repair"
            self._save_job(job)
            try:
                next_state = self.start_correction(run_id, auto_chain=True)
            except (OSError, ValueError, RuntimeError) as exc:
                job["auto_correction_stop_reason"] = f"下一次局部修正未能安全启动：{exc}"
                job["auto_correction_chain"] = False
                self._save_job(job)
                return
            job["auto_correction_run_id"] = next_state["run_id"]
            job["status"] = "auto_correcting"
            job["failure"] = None
            self._save_job(job)

    def start(self, conditions, request_id, *, run_id=None, seed=None):
        with self.lock:
            self._reap()
            if isinstance(conditions, dict) and conditions.get('template_id', 'copper_concentration_cell') != 'copper_concentration_cell':
                raise ValueError('当前单次动作流程为铜浓差电池；其他反应须选择已注册的批量模板与执行方式')
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
                raise ValueError("需要有效的请求编号，供网络重试去重")
            fingerprint = hashlib.sha256(json.dumps(conditions, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if request_id in self.requests:
                job = self.jobs[self.requests[request_id]]
                if job["fingerprint"] != fingerprint:
                    raise ValueError("同一请求编号的实验条件冲突")
                return self.state(job["run_id"])
            if self.active and self.jobs[self.active]["status"] in {"starting", "running", "finalizing", "paused", "awaiting_validation"}:
                raise BusyError("已有实验正在运行或等待核对，请勿重复启动")
            config = self.input.build_condition_config(self.base_config, conditions)
            # CPU-only nominal preflight: no robot execution and no resampling of
            # the eventual run's operation errors. Reject unsupported physics early.
            if conditions.get('liquid_junction_enabled'):
                nominal = deepcopy(config)
                nominal['error_model']['enabled'] = False
                prediction = self.simulator.run_experiment(nominal, seed=0)['prediction']
                if not prediction.get('valid'):
                    raise ValueError('启动前液接参数检查未通过：'+str(prediction.get('reason')))
            selected = []
            for step in config["protocol"]:
                step_id = step["step_id"]
                if step_id.startswith(("ui_left_stock_", "ui_left_water_",
                                       "ui_right_stock_", "ui_right_water_")):
                    selected.append(step)
            prepared_targets = {step.get("target") for step in selected
                                if step["action"] == "dispense"}
            if not {"left_half_cell", "right_half_cell"}.issubset(prepared_targets):
                raise ValueError("同步混匀流程需要完整的左右侧配液步骤")
            selected.append({"step_id": "ui_simultaneous_mix", "action": "mix_pair",
                             "targets": ["left_half_cell", "right_half_cell"],
                             "duration_s": config["condition_inputs"].get("mixing_duration_s", 10.0)})
            selected.extend([
                {"step_id": "ui_park_pipette", "action": "park_pipette",
                 "target": "pipette_stand"},
                {"step_id": "ui_pick_left_copper_electrode", "action": "pick_electrode",
                 "source": "electrode_rack", "target": "left_half_cell",
                 "component": "copper_electrode"},
                {"step_id": "ui_place_left_copper_electrode", "action": "place_electrode",
                 "target": "left_half_cell", "component": "copper_electrode"},
                {"step_id": "ui_pick_right_copper_electrode", "action": "pick_electrode",
                 "source": "electrode_rack", "target": "right_half_cell",
                 "component": "copper_electrode"},
                {"step_id": "ui_place_right_copper_electrode", "action": "place_electrode",
                 "target": "right_half_cell", "component": "copper_electrode"},
                {"step_id": "ui_pick_salt_bridge", "action": "pick_salt_bridge",
                 "source": "salt_bridge_rack", "target": "salt_bridge_holder",
                 "targets": ["left_half_cell", "right_half_cell"], "component": "salt_bridge"},
                {"step_id": "ui_connect_salt_bridge", "action": "connect_salt_bridge",
                 "target": "salt_bridge_holder", "targets": ["left_half_cell", "right_half_cell"],
                 "component": "salt_bridge"},
            ])
            selected.extend(self.measurement_steps())
            selected = self.expand_pipette_tool_actions(selected)
            if not selected:
                raise ValueError("实验条件没有产生母液转移步骤")
            run_id = run_id or uuid4().hex
            if not re.fullmatch(r'[a-f0-9]{32}', run_id):
                raise ValueError('运行编号无效')
            job = {"run_id": run_id, "request_id": request_id, "fingerprint": fingerprint,
                   "conditions": deepcopy(config["condition_inputs"]), "seed": secrets.randbits(31) if seed is None else seed,
                   "created_at_utc": now(), "status": "starting", "failure": None,
                   "total_steps": len(selected), "plan": selected,
                   # Let a completed independent audit enter the bounded
                   # detect/classify/solve/execute/recheck chain when eligible.
                   # Unsupported findings and exhausted candidates stop safely.
                   "auto_correction_chain": True}
            conditions_path = self.jobs_root / (run_id + ".conditions.json")
            atomic_json(conditions_path, job["conditions"])
            log_path = self.jobs_root / (run_id + ".runner.log")
            argv = [self.python, "-u", str(self.runner), "--engine-root", str(self.engine_root),
                    "--conditions", str(conditions_path), "--output-root", str(self.runs_root),
                    "--seed", str(job["seed"]), "--run-id", run_id, "--include-paired-mix",
                    "--include-left-electrode", "--include-right-electrode", "--include-salt-bridge", '--include-measurement', '--live-preview']
            with log_path.open("wb") as log:
                process = self.launcher(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.jobs[run_id] = job
            self.requests[request_id] = run_id
            self.active = run_id
            self.process = process
            job["worker_pid"] = process.pid
            job["log_path"] = str(log_path)
            self._save_job(job)
            return self.state(run_id)

    def start_correction(self, source_run_id, *, auto_chain=True):
        """Launch a bounded, evidence-linked local repair from a confirmed prefix.

        Each repair is a separate run; the independent process validator checks
        the resulting segment before another candidate can be considered.
        """
        with self.lock:
            self._assert_renderer_idle()
            if not isinstance(source_run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_run_id):
                raise ValueError("来源运行编号无效")
            source_dir = (self.runs_root / source_run_id).resolve()
            if source_dir.parent != self.runs_root or not source_dir.is_dir():
                raise ValueError("找不到需要修正的原始运行记录")
            report_path = source_dir / "process_validation" / "report.json"
            if not report_path.is_file():
                raise ValueError("请先完成该运行的过程自动校验，再生成局部修正")
            from process_validation.repairs import build_repair_plan
            report = json.loads(report_path.read_text(encoding="utf-8"))
            plan = build_repair_plan(source_dir, report)
            if not plan.get("eligible"):
                raise ValueError(plan.get("reason") or "当前异常不适用于首版自动修正")

            archive = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
            manifest = json.loads((source_dir / "video_manifest.json").read_text(encoding="utf-8"))
            snapshot = archive.get("snapshot", {})
            completed_events = [event for event in archive.get("events", [])
                                if event.get("type") == "step.completed"]
            checkpoint_revision = next((index for index, event in enumerate(completed_events, 1)
                                        if event.get("step_id") == plan.get("checkpoint_step_id")), None)
            if checkpoint_revision is None:
                raise ValueError("局部修正检查点不在已确认成功步骤中")
            failed_events = [event for event in archive.get("events", [])
                             if event.get("type") == "step.failed"]
            fault_step_id = (plan.get("fault_step_id") or
                             (failed_events[-1].get("step_id") if failed_events else
                             next((row.get("step_id") for row in report.get("issues", [])
                                   if row.get("status") == "fail" and row.get("step_id")), None)))
            evidence_fields = ("step_id", "frame_index", "last_frame_index", "video_time_s",
                               "last_video_time_s", "body", "obstacle", "side", "axis")
            fault_evidence = [{key: row[key] for key in evidence_fields if key in row}
                              | {"group": row.get("group"), "code": row.get("code"),
                                 "message": row.get("message")}
                              for row in report.get("issues", [])
                              if row.get("status") in {"fail", "review"}]
            prior_lineage = snapshot.get("state_ledger", {}).get("lineage", {})
            plan["repair_contract"] = {
                "schema_version": "1.1.0", "kind": "evidence_bounded_local_repair",
                "source_run_id": source_run_id,
                "root_run_id": prior_lineage.get("root_run_id") or source_run_id,
                "candidate_id": plan.get("candidate_id"),
                "strategy_family": plan.get("strategy_family", "electrode_pose_path"),
                "fault_step_id": fault_step_id,
                "fault_step_ids": plan.get("fault_step_ids", []),
                "checkpoint_step_id": plan["checkpoint_step_id"],
                "checkpoint_revision": checkpoint_revision,
                "fault_evidence": plan.get("fault_evidence") or fault_evidence,
                "pipeline_stages": plan.get("pipeline_stages", []),
                "candidate_preflight": plan.get("candidate_preflight"),
                "dependency_plan": plan.get('dependency_plan'),
                "repair_sequence": plan.get('repair_sequence'),
                "proposal_fingerprint": plan.get('proposal_fingerprint'),
                "reuse_confirmed_prefix": True,
                "replay_previous_physical_actions": False,
                "preserve": plan.get("preserve", ["conditions", "random_seed", "accepted_receipts",
                             "chemical_state_at_checkpoint", "chemical_model_state_at_checkpoint",
                             "independent_source_archive"]),
                "revalidate_with_policy_id": report.get("policy_id"),
                "retry_limit": plan.get("max_attempts"),
            }
            conditions = snapshot.get("conditions")
            if not isinstance(conditions, dict):
                raise ValueError("原始运行缺少实验条件，不能安全复现局部状态")
            actions = snapshot.get("plan", [])
            include_flags = self._workflow_flags(actions)

            new_run_id = uuid4().hex
            conditions_path = self.jobs_root / (new_run_id + ".conditions.json")
            profile_path = self.jobs_root / (new_run_id + ".correction.json")
            atomic_json(conditions_path, conditions)
            profile = {**plan, "source_run_id": source_run_id,
                       "candidate_id": plan.get(
                           "candidate_id",
                           f"electrode_side_grasp_clear_corridor_{plan['attempt_index']}"),
                       "actions": plan.get("actions", {}),
                       "routes": plan.get("routes", {}),
                       "auto_correction_chain": bool(auto_chain)}
            running_stages = deepcopy(plan.get("pipeline_stages", []))
            for stage in running_stages:
                if stage.get("stage") == "execute":
                    stage["status"] = "running"
            atomic_json(profile_path, profile)
            runtime = manifest.get("presentation", {}).get("runtime", {})
            matterix_root = runtime.get("matterix_root", "/home/ubuntu/Documents/Kit/apps/Matterix")
            seed = archive.get("seed", 0)
            argv = [self.python, "-u", str(self.runner), "--engine-root", str(self.engine_root),
                    "--conditions", str(conditions_path), "--output-root", str(self.runs_root),
                    "--seed", str(seed), "--run-id", new_run_id, "--matterix-root", matterix_root,
                    "--resume-archive", str(source_dir / "run.json"),
                    "--resume-after-step", plan["checkpoint_step_id"],
                    "--stop-after-step", plan["stop_after_step"],
                    "--correction-profile", str(profile_path), *include_flags]
            request_id = "correction_" + new_run_id
            job = {"run_id": new_run_id, "request_id": request_id,
                   "source_run_id": source_run_id, "correction": profile,
                   "auto_correction_chain": bool(auto_chain),
                   "repair_pipeline": {"stages": running_stages},
                   "fingerprint": hashlib.sha256(json.dumps(conditions, sort_keys=True,
                                                               allow_nan=False).encode()).hexdigest(),
                   "conditions": deepcopy(conditions), "seed": seed, "created_at_utc": now(),
                   "status": "starting", "failure": None,
                   "total_steps": len(actions), "plan": actions}
            log_path = self.jobs_root / (new_run_id + ".runner.log")
            with log_path.open("wb") as log:
                process = self.launcher(argv, stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL)
            self.jobs[new_run_id] = job
            self.requests[request_id] = new_run_id
            self.active = new_run_id
            self.process = process
            job["worker_pid"] = process.pid
            job["log_path"] = str(log_path)
            self._save_job(job)
            return self.state(new_run_id)

    def start_continuation(self, source_run_id, expected_revision, checkpoint_signature,
                           retry_failed_preflight=False, *, auto_chain=False):
        """Continue once from an audited, released electrode correction checkpoint."""
        with self.lock:
            if not isinstance(source_run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_run_id):
                raise ValueError('修正运行编号无效')
            request_id = 'continue_' + source_run_id
            if request_id in self.requests:
                saved = self.jobs[self.requests[request_id]]
                original = saved.get('continuation', {})
                retryable_preflight = False
                if retry_failed_preflight and saved.get('status') == 'failed':
                    archive = self._read_archive(saved['run_id'])
                    log_path = Path(saved.get('log_path', ''))
                    try:
                        log_text = log_path.read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        log_text = ''
                    retryable_preflight = (
                        archive is None and
                        any(marker in log_text for marker in (
                            '续跑检查点重建不一致：state',
                            '恢复后的对象关系与来源状态账本不一致',
                        )) and
                        'usage: matterix_first_transfer.py' in log_text
                    )
                if (original.get('expected_revision') != expected_revision or
                        (original.get('checkpoint_signature') != checkpoint_signature
                         and not retryable_preflight)):
                    raise ValueError('这条修正已提交过另一份续跑请求，请读取当前运行')
                if not retryable_preflight:
                    return self.state(saved['run_id'])
                # Only an explicit retry may replace a failed attempt that
                # stopped during checkpoint preflight, before an archive or
                # simulator execution existed. Preserve the failed job record.
            self._assert_renderer_idle()
            source_dir = (self.runs_root / source_run_id).resolve()
            if source_dir.parent != self.runs_root or not source_dir.is_dir():
                raise ValueError('找不到修正记录')
            from process_validation.continuation import build_continuation_plan
            plan = build_continuation_plan(source_dir)
            if not plan.get('eligible'):
                raise ValueError(plan.get('reason') or '这次修正尚不能继续')
            if (isinstance(expected_revision, bool) or expected_revision != plan['expected_revision'] or
                    checkpoint_signature != plan['checkpoint_signature']):
                raise ValueError('检查点或报告已经变化，请重新读取报告后继续')
            archive = json.loads((source_dir / 'run.json').read_text(encoding='utf-8'))
            manifest = json.loads((source_dir / 'video_manifest.json').read_text(encoding='utf-8'))
            actions = archive['snapshot']['plan']
            new_run_id = uuid4().hex
            conditions = archive['snapshot']['conditions']
            conditions_path = self.jobs_root / (new_run_id + '.conditions.json')
            profile_path = self.jobs_root / (new_run_id + '.continuation.json')
            atomic_json(conditions_path, conditions)
            atomic_json(profile_path, plan)
            matterix_root = manifest.get('presentation', {}).get('runtime', {}).get(
                'matterix_root', '/home/ubuntu/Documents/Kit/apps/Matterix')
            argv = [self.python, '-u', str(self.runner), '--engine-root', str(self.engine_root),
                    '--conditions', str(conditions_path), '--output-root', str(self.runs_root),
                    '--seed', str(archive['seed']), '--run-id', new_run_id,
                    '--matterix-root', matterix_root, '--resume-archive', str(source_dir / 'run.json'),
                    '--resume-after-step', plan['checkpoint_step_id'],
                    '--continuation-profile', str(profile_path), '--live-preview',
                    *self._workflow_flags(actions)]
            job = {'run_id': new_run_id, 'request_id': request_id,
                   'source_run_id': source_run_id, 'continuation': plan,
                   'auto_correction_chain': bool(auto_chain),
                   'fingerprint': hashlib.sha256(json.dumps(conditions, sort_keys=True,
                                                            allow_nan=False).encode()).hexdigest(),
                   'conditions': deepcopy(conditions), 'seed': archive['seed'],
                   'created_at_utc': now(), 'status': 'starting', 'failure': None,
                   'total_steps': len(actions), 'plan': actions}
            log_path = self.jobs_root / (new_run_id + '.runner.log')
            with log_path.open('wb') as log:
                process = self.launcher(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.jobs[new_run_id] = job
            self.requests[request_id] = new_run_id
            self.active, self.process = new_run_id, process
            job.update(worker_pid=process.pid, log_path=str(log_path))
            self._save_job(job)
            return self.state(new_run_id)

    def _reviewed_preflight_retryable(self, job, source_run_id, review):
        """Keep automatic retry narrow; a human may request a fresh source-based attempt."""
        if not isinstance(review,dict):
            return False
        scope=job.get('reviewed_segment') or {}
        if (job.get('source_run_id')!=source_run_id
                or job.get('status') not in {'paused','failed'}
                or scope.get('next_step_ids')!=review.get('next_step_ids')
                or scope.get('stop_after_step')!=review.get('stop_after_step')
                or scope.get('reviewed_run_id')!=review.get('run_id')
                or scope.get('review',{}).get('checkpoint_step_id')!=review.get('checkpoint_step_id')):
            return False
        try:
            run_dir=(self.runs_root/job['run_id']).resolve()
            if run_dir.parent!=self.runs_root:
                return False
            manifest=json.loads((run_dir/'video_manifest.json').read_text(encoding='utf8'))
        except (OSError,ValueError,KeyError,TypeError):
            return False
        diagnostic=manifest.get('diagnostic') or {}
        violation=(manifest.get('physical_constraints') or {}).get('violation') or {}
        if (review.get('retry_of_run_id')==job.get('run_id')
                and review.get('retry_authorization')=='user_requested'
                and isinstance(review.get('retry_message'),str) and review['retry_message'].strip()
                and manifest.get('status')=='failed'
                and diagnostic.get('source_run_id')==source_run_id
                and diagnostic.get('restored_after_step')==review.get('checkpoint_step_id')
                and diagnostic.get('stop_after_step')==review.get('stop_after_step')):
            return True
        return bool(manifest.get('status')=='failed'
            and manifest.get('frame_count')==1
            and diagnostic.get('source_run_id')==source_run_id
            and diagnostic.get('restored_after_step')==review.get('checkpoint_step_id')
            and diagnostic.get('stop_after_step')==review.get('stop_after_step')
            and diagnostic.get('executed_step_ids')==[]
            and violation.get('code')=='planned_hardware_clearance'
            and violation.get('phase')=='pick_lead/align')

    def start_reviewed_segment(self, source_run_id, review):
        """Run only the next human-approved, independently checked action pair."""
        with self.lock:
            if not isinstance(source_run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_run_id):
                raise ValueError('来源运行编号无效')
            request_id = 'reviewed_segment_' + source_run_id
            if request_id in self.requests:
                saved = self.jobs[self.requests[request_id]]
                if saved.get('reviewed_segment', {}).get('review') == review:
                    return self.state(saved['run_id'])
                if not self._reviewed_preflight_retryable(saved,source_run_id,review):
                    raise ValueError('已有不同的人工确认接续，请先读取其状态')
            self._assert_renderer_idle()
            source_dir = (self.runs_root / source_run_id).resolve()
            if source_dir.parent != self.runs_root or not source_dir.is_dir():
                raise ValueError('找不到用户确认的运行')
            from process_validation.continuation import build_continuation_plan
            plan = build_continuation_plan(source_dir, reviewed_handoff=review)
            if not plan.get('eligible') or plan.get('status') != 'ready_reviewed_segment':
                raise ValueError(plan.get('reason') or '用户确认的检查点尚不可接续')
            scope = plan['reviewed_handoff']
            archive = json.loads((source_dir / 'run.json').read_text(encoding='utf8'))
            manifest = json.loads((source_dir / 'video_manifest.json').read_text(encoding='utf8'))
            actions, conditions = archive['snapshot']['plan'], archive['snapshot']['conditions']
            new_run_id = uuid4().hex
            conditions_path = self.jobs_root / (new_run_id + '.conditions.json')
            atomic_json(conditions_path, conditions)
            matterix_root = manifest.get('presentation', {}).get('runtime', {}).get(
                'matterix_root', '/home/ubuntu/Documents/Kit/apps/Matterix')
            argv = [self.python, '-u', str(self.runner), '--engine-root', str(self.engine_root),
                    '--conditions', str(conditions_path), '--output-root', str(self.runs_root),
                    '--seed', str(archive['seed']), '--run-id', new_run_id,
                    '--matterix-root', matterix_root, '--resume-archive', str(source_dir / 'run.json'),
                    '--resume-after-step', plan['checkpoint_step_id'],
                    '--stop-after-step', scope['stop_after_step'], '--live-preview',
                    *self._workflow_flags(actions)]
            job = {'run_id': new_run_id, 'request_id': request_id, 'source_run_id': source_run_id,
                   'reviewed_segment': {**scope, 'source_run_id': source_run_id,
                       'checkpoint_signature': plan['checkpoint_signature'], 'review': deepcopy(review)},
                   'auto_correction_chain': False,
                   'fingerprint': hashlib.sha256(json.dumps(conditions, sort_keys=True,
                                                              allow_nan=False).encode()).hexdigest(),
                   'conditions': deepcopy(conditions), 'seed': archive['seed'], 'created_at_utc': now(),
                   'status': 'starting', 'failure': None, 'total_steps': len(actions), 'plan': actions}
            log_path = self.jobs_root / (new_run_id + '.runner.log')
            with log_path.open('wb') as log:
                process = self.launcher(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.jobs[new_run_id] = job
            self.requests[request_id] = new_run_id
            self.active, self.process = new_run_id, process
            job.update(worker_pid=process.pid, log_path=str(log_path))
            self._save_job(job)
            return self.state(new_run_id)

    def editor(self, conditions):
        from reaction_catalog import FIELDS
        return {"current": deepcopy(conditions), "active_species_name": "Cu²⁺", "redox_couple_name_zh": "Cu²⁺/Cu",
                'condition_fields': deepcopy(FIELDS), 'template_id': 'copper_concentration_cell',
                "stock_concentration_m": 0.1, "operation_error_profile": self.profile,
                "liquid_junction_model_options": self.input.LIQUID_JUNCTION_MODEL_OPTIONS,
                "measurable_ranges": {"temperature_c": {
                    "liquid_junction_policy": self.base_config['cell']['liquid_junction_potential'].get('parameter_use_policy'),
                    "activity_reference_range": [0,50], "liquid_junction_reference_range": [0,95],
                    "davies_ionic_strength_range_mol_kg": [0,.5]}}}

    def state(self, run_id=None):
        with self.lock:
            self._reap()
            run_id = run_id or self.active
            job = self.jobs.get(run_id) if run_id else None
            if run_id and not job:
                raise KeyError("找不到运行")
            # Keep clients following the original correction request attached
            # to the currently executing bounded retry, while preserving each
            # attempt as a separate immutable run in history.
            for _ in range(3):
                next_run_id = (job or {}).get("auto_correction_run_id")
                next_job = self.jobs.get(next_run_id) if next_run_id else None
                if not next_job:
                    break
                run_id, job = next_run_id, next_job
            conditions = job["conditions"] if job else DEFAULT_CONDITIONS
            archive = self._read_archive(run_id) if job else None
            snapshot = deepcopy(archive["snapshot"]) if archive else None
            if snapshot:
                state = snapshot["state"]
                video = snapshot.get("video", {})
                completed = snapshot["completed_steps"]
                status = snapshot["status"]
                if job["status"] in {"failed", "paused", "awaiting_validation"}:
                    status = job["status"]
                elif status == "completed" and self.process:
                    status = "finalizing"
                elif completed == 0 and video.get("status") == "not_connected":
                    status = "starting"
            else:
                state = {"containers": {"left_half_cell": {"volume_ul": 0, "concentrations_m": {"Cu": None}},
                                        "right_half_cell": {"volume_ul": 0, "concentrations_m": {"Cu": None}}},
                         "tip": {"attached": False, "liquid": {"volume_ul": 0}},
                         "temperature_k": conditions["temperature_c"] + 273.15}
                video = {"status": "not_connected", "recording_ref": None}
                completed = 0
                status = job["status"] if job else "idle"
            voltage_reason = ((snapshot or {}).get("voltage", {}).get("reason") if snapshot else None)
            if (snapshot or {}).get('voltage', {}).get('valid'):
                voltage_reason = '开路电压虚拟测量已确认；不是实际仪器实测值'
            elif not voltage_reason:
                has_water = bool(job and any(
                    step["step_id"].startswith("ui_left_water_") for step in job["plan"]))
                has_mix = bool(job and any(step["step_id"] == "ui_simultaneous_mix" for step in job["plan"]))
                has_left_electrode = bool(job and any(step["step_id"] == "ui_place_left_copper_electrode" for step in job["plan"]))
                has_right_electrode = bool(job and any(step["step_id"] == "ui_place_right_copper_electrode" for step in job["plan"]))
                voltage_reason = ("左右两侧已配液并同步混匀，左右铜电极均已安装；盐桥尚未连接，因此不计算电压"
                                  if has_left_electrode and has_right_electrode else
                                  "左右两侧已配液并同步混匀；当前只安装左侧铜电极，右侧电极和盐桥尚未连接，因此不计算电压"
                                  if has_left_electrode else
                                  "左右两侧定量配液与同步混匀已完成；尚未组装电池或测量电压"
                                  if has_mix else
                                  "左侧目标浓度与体积已由母液和定量加水形成；尚未混匀、组装电池或测量电压"
                                  if has_water else "当前只执行母液转移，尚未配液和组装电池")
            return {
                "mode": "first_transfer", "service": "cloud-experiment-coordinator",
                "run_id": run_id, "status": status, "failure": job.get("failure") if job else None,
                "conditions": deepcopy(conditions), "condition_editor": self.editor(conditions),
                "state": state, "completed_steps": completed, "total_steps": snapshot["total_steps"] if snapshot else job["total_steps"] if job else 4,
                "plan": snapshot["plan"] if snapshot else job["plan"] if job else [],
                "state_ledger": snapshot.get("state_ledger") if snapshot else None,
                "current_step_id": snapshot.get("current_step_id") if snapshot else None,
                "state_uncertain": snapshot.get("state_uncertain", False) if snapshot else False,
                'continuation': deepcopy(job.get('continuation')) if job else None,
                'correction': deepcopy(job.get('correction')) if job else None,
                'reviewed_segment': deepcopy(job.get('reviewed_segment')) if job else None,
                'repair_pipeline': deepcopy(job.get('repair_pipeline')) if job else None,
                'auto_correction': ({'enabled': bool(job.get('auto_correction_chain')),
                    'next_run_id': job.get('auto_correction_run_id'),
                    'stop_reason': job.get('auto_correction_stop_reason')}
                    if job and (job.get('auto_correction_chain') or job.get('auto_correction_stop_reason')
                                or job.get('auto_correction_run_id')) else None),
                'source_run_id': job.get('source_run_id') if job else None,
                "voltage_mv": (snapshot or {}).get('voltage', {}).get('virtual_meter_mv'),
                'voltage': (snapshot or {}).get('voltage', {}),
                "full_experiment_completed": bool((snapshot or {}).get('full_experiment_completed')),
                "voltage_reason": voltage_reason,
                "video": video, "events": archive.get("events", []) if archive else [],
                "pending_command": archive.get("pending_command") if archive else None,
                "conservation": snapshot.get("conservation") if snapshot else None,
                "timestamp": now(),
            }


def create_server(host, port, coordinator, token, batch_service=None, allowed_origins=("null",)):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from process_validation.service import ValidationService
    validation_service = ValidationService(coordinator.runs_root, getattr(coordinator, 'python', sys.executable))
    if batch_service is None:
        from batch_jobs import CloudBatchService
        batch_service = CloudBatchService(coordinator)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(self.command, urlsplit(self.path).path, flush=True)

        def send_json(self, code, data):
            value = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            if self.headers.get("Origin") in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

        def authorized(self):
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())

        def do_OPTIONS(self):
            self.send_response(204)
            if self.headers.get("Origin") in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
            self.end_headers()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/health":
                self.send_json(200, {"status": "ok", "mode": "first_transfer", "auth_required": True,
                                     "capabilities": {"history": True, "batch": True}})
                return
            if not self.authorized():
                self.send_json(401, {"message": "请填写下方的实验访问码"})
                return
            try:
                run_id = parse_qs(url.query).get("run_id", [None])[0]
                if run_id and not re.fullmatch(r"[a-f0-9]{32}", run_id):
                    raise ValueError("运行编号无效")
                if url.path == '/capabilities':
                    self.send_json(200, {**batch_service.capabilities(), 'process_validation': {'available': True, 'path': '/validation', 'read_only_audit': True, 'bounded_local_repairs': True, 'automatic_detect_classify_solve_recheck': True, 'continue_after_checked_correction': True}, 'live_preview': {'available': True, 'transport': 'latest_jpeg_with_same_frame_state', 'path': '/preview', 'target_interval_s': 0.5}})
                elif url.path == '/history':
                    self.send_json(200, batch_service.history.payload())
                elif url.path == '/batch/status':
                    job_id = parse_qs(url.query).get('job_id', [None])[0]
                    self.send_json(200, batch_service.status(job_id))
                elif url.path == '/batch/list':
                    self.send_json(200, batch_service.list_batches())
                elif url.path == '/sweep':
                    job_id = parse_qs(url.query).get('job_id', [None])[0]
                    self.send_json(200, batch_service.sweep(job_id))
                elif url.path in {"/state", "/run"}:
                    self.send_json(200, coordinator.state(run_id))
                elif url.path == "/preview":
                    from preview_reader import preview_payload
                    after = int(parse_qs(url.query).get("after_sequence", ["0"])[0])
                    self.send_json(200, preview_payload(coordinator.runs_root, run_id, after))
                elif url.path == "/validation":
                    self.send_json(200, validation_service.read(run_id))
                elif url.path == "/steps":
                    state = coordinator.state(run_id)
                    self.send_json(200, {"run_id": state["run_id"], "events": state["events"]})
                else:
                    self.send_json(404, {"message": "当前节点不支持此接口"})
            except KeyError as exc:
                self.send_json(404, {"message": str(exc)})
            except ValueError as exc:
                self.send_json(422, {"message": str(exc)})

        def do_POST(self):
            if not self.authorized():
                self.send_json(401, {"message": "请填写下方的实验访问码"})
                return
            path = urlsplit(self.path).path
            if path not in {'/simulate', '/batch/start', '/batch/cancel', '/batch/promote-to-video', '/validation/start', '/validation/correct', '/validation/continue', '/validation/reviewed-segment'}:
                self.send_json(404, {"message": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("实验条件请求大小无效")
                conditions = json.loads(self.rfile.read(length))
                if path == '/validation/start':
                    self.send_json(202, validation_service.start(conditions.get('run_id')))
                elif path == '/validation/correct':
                    with coordinator.lock:
                        if batch_service.busy():
                            raise BusyError('批量仿真正在运行，暂不启动局部修正')
                        # A repair ends at its own segment unless a caller
                        # explicitly requests an automatic continuation.
                        state = coordinator.start_correction(conditions.get('run_id'),
                                                             auto_chain=conditions.get('auto_chain') is True)
                    self.send_json(202, {'accepted': True, 'run_id': state['run_id'],
                                         'source_run_id': conditions.get('run_id'),
                                         'scope': 'evidence_bounded_local_repair_segment', 'state': state})
                elif path == '/validation/continue':
                    with coordinator.lock:
                        if batch_service.busy():
                            raise BusyError('批量仿真正在运行，暂不能接着执行')
                        state = coordinator.start_continuation(conditions.get('run_id'),
                            conditions.get('expected_revision'), conditions.get('checkpoint_signature'),
                            retry_failed_preflight=conditions.get('retry_failed_preflight') is True,
                            auto_chain=conditions.get('auto_chain') is True)
                    self.send_json(202, {'accepted': True, 'run_id': state['run_id'],
                        'source_run_id': conditions.get('run_id'), 'scope': 'remaining_workflow', 'state': state})
                elif path == '/validation/reviewed-segment':
                    with coordinator.lock:
                        if batch_service.busy():
                            raise BusyError('批量仿真正在运行，暂不能启动局部片段')
                        state = coordinator.start_reviewed_segment(conditions.get('run_id'),
                                                                   conditions.get('review'))
                    self.send_json(202, {'accepted': True, 'run_id': state['run_id'],
                        'source_run_id': conditions.get('run_id'), 'scope': 'human_reviewed_bounded_segment',
                        'state': state})
                elif path == '/batch/promote-to-video':
                    job = batch_service.promote_to_video(conditions, self.headers.get('Idempotency-Key'))
                    self.send_json(202, {'accepted': True, 'job': job})
                elif path == '/batch/start':
                    job = batch_service.start(conditions, self.headers.get('Idempotency-Key'))
                    self.send_json(202, {'accepted': True, 'job': job})
                elif path == '/batch/cancel':
                    self.send_json(200, batch_service.cancel(conditions.get('job_id')))
                else:
                    with coordinator.lock:
                        if batch_service.busy():
                            raise BusyError('批量实验运行或等待核对中，请勿同时提交单次实验')
                        state = coordinator.start(conditions, self.headers.get("Idempotency-Key"))
                    self.send_json(202, {"accepted": True, "run_id": state["run_id"], "state": state})
            except BusyError as exc:
                self.send_json(409, {"message": str(exc)})
            except (ValueError, TypeError) as exc:
                self.send_json(422, {"message": str(exc)})
            except OSError as exc:
                self.send_json(500, {"message": "无法启动云端执行器：" + str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--engine-root", type=Path, required=True)
    p.add_argument("--runner", type=Path, required=True)
    p.add_argument("--worker-python", default=sys.executable)
    p.add_argument("--runs-root", type=Path, required=True)
    p.add_argument("--jobs-root", type=Path, required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=50007)
    p.add_argument('--render-workers', type=int, default=1)
    p.add_argument('--numerical-workers', type=int, default=4)
    p.add_argument('--allowed-origin', action='append', default=[],
                   help='Allowed frontend origin; repeat for more sites. Defaults to local file origin.')
    a = p.parse_args()
    token = a.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        p.error("访问码无效")
    coordinator = ExperimentCoordinator(a.engine_root, a.runner, a.worker_python, a.runs_root, a.jobs_root)
    from batch_jobs import CloudBatchService
    batch_service = CloudBatchService(coordinator, a.render_workers, a.numerical_workers)
    server = create_server(a.host, a.port, coordinator, token, batch_service,
                           tuple(a.allowed_origin or ["null"]))
    print(f"COORDINATOR_READY port={server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

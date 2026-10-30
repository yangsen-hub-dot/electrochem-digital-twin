"""Durable batch orchestration with isolated run processes and bounded concurrency.

Rendered jobs use the same coordinator/runner as single experiments, each with
its own jobs directory and GPU assignment. Numerical jobs reuse the same
condition builder and engine, without fabricated robot execution receipts.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time
from uuid import uuid4

from reaction_catalog import ReactionCatalog
from history_records import CloudHistory, sweep_row


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


class CloudBatchService:
    def __init__(self, coordinator, render_workers=1, numerical_workers=4, launcher=subprocess.Popen):
        self.coordinator = coordinator
        self.catalog = ReactionCatalog(coordinator)
        self.history = CloudHistory(coordinator.runs_root, coordinator.jobs_root, coordinator.input.LIQUID_JUNCTION_MODEL_OPTIONS)
        self.limits = {'workflow_video': render_workers, 'numerical': numerical_workers}
        if any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 16 for v in self.limits.values()):
            raise ValueError('服务器并发限制无效')
        self.launcher = launcher
        self.root = coordinator.jobs_root / 'batches'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.requests = {}
        self.executor = None
        for path in self.root.glob('*.batch.json'):
            try:
                batch = json.loads(path.read_text(encoding='utf-8'))
                if not re.fullmatch(r'[a-f0-9]{32}', batch['job_id']):
                    continue
                if batch['status'] in {'running', 'cancelling'}:
                    batch['status'] = 'paused'
                    batch['message'] = '服务已重启；原执行状态需核对，未自动重跑'
                    for case in batch['cases']:
                        if case['status'] in {'queued', 'running'}:
                            case['status'] = 'paused'
                    save(path, batch)
                self.jobs[batch['job_id']] = batch
                self.requests[batch['request_id']] = batch['job_id']
            except (ValueError, KeyError, OSError):
                continue

    def capabilities(self):
        return {'history': True, 'batch': True, 'schema_version': '1.0',
                'reaction_templates': self.catalog.describe(), 'max_workers': self.limits,
                'execution_modes': {'workflow_video': '完整动作与录像', 'numerical': '数值流程仿真（无录像）'}}

    def list_batches(self):
        with self.lock:
            return {'batches': [{key: deepcopy(batch.get(key)) for key in
                    ('job_id','created_at_utc','status','template_id','execution_mode','scenario_count','message')}
                    for batch in sorted(self.jobs.values(), key=lambda row: row['created_at_utc'], reverse=True)]}

    def busy(self):
        with self.lock:
            return any(job['status'] in {'running', 'paused', 'cancelling'} for job in self.jobs.values())

    def _persist(self, batch):
        save(self.root / (batch['job_id'] + '.batch.json'), batch)

    def start(self, payload, request_id, *, seed_override=None, source_run_id=None):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise ValueError('批量运行需要有效的请求编号，供重试去重')
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.coordinator.lock, self.lock:
            if request_id in self.requests:
                batch = self.jobs[self.requests[request_id]]
                if batch['fingerprint'] != fingerprint:
                    raise ValueError('同一批量请求编号的条件冲突')
                return self.status(batch['job_id'])
            if self.busy():
                raise ValueError('已有批次运行或等待状态核对，请先处理原批次')
            state = self.coordinator.state()
            if state['status'] in {'starting','running','finalizing','paused','awaiting_validation'}:
                raise ValueError('单次实验正在运行或等待核对')
            template, mode, rows = self.catalog.expand(payload, state['conditions'])
            workers = payload.get('max_workers', min(2, self.limits[mode]))
            if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= self.limits[mode]:
                raise ValueError(f'当前执行方式并发数须为 1–{self.limits[mode]}')
            batch_id = uuid4().hex
            seed = secrets.randbits(30)
            batch = {'job_id': batch_id, 'request_id': request_id, 'fingerprint': fingerprint,
                     'created_at_utc': now(), 'status': 'running', 'template_id': template,
                     'execution_mode': mode, 'max_workers': workers, 'scenario_count': len(rows),
                     'settings': deepcopy(payload), 'cases': [], 'message': '批量任务已接受'}
            if source_run_id:
                batch['source_numerical_run_id'] = source_run_id
            for index, row in enumerate(rows, 1):
                batch['cases'].append({**row, 'run_id': uuid4().hex, 'matrix_index': index,
                                       'status': 'queued', 'seed': seed_override if seed_override is not None else seed + index - 1,
                                       'source_numerical_run_id': source_run_id, 'failure': None})
            self.jobs[batch_id] = batch
            self.requests[request_id] = batch_id
            self._persist(batch)
            slots = queue.Queue()
            for slot in range(workers):
                slots.put(slot)
            self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cloud-batch')
            for index in range(len(rows)):
                self.executor.submit(self._run_case, batch_id, index, slots)
            # Do not retain an accepting executor after all work is submitted.
            self.executor.shutdown(wait=False)
            return self.status(batch_id)

    def promote_to_video(self, payload, request_id):
        if not isinstance(payload, dict):
            raise ValueError('视频复核请求格式无效')
        source_batch_id, source_run_id = payload.get('batch_id'), payload.get('run_id')
        if not isinstance(source_batch_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_batch_id):
            raise ValueError('数值批次编号无效')
        if not isinstance(source_run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_run_id):
            raise ValueError('数值运行编号无效')
        with self.lock:
            source_batch = self.jobs.get(source_batch_id)
            if not source_batch or source_batch.get('execution_mode') != 'numerical':
                raise ValueError('只能从数值仿真批次发起视频复核')
            source_case = next((row for row in source_batch['cases'] if row.get('run_id') == source_run_id), None)
            if not source_case or source_case.get('status') != 'completed':
                raise ValueError('所选数值组尚未成功完成')
            result_path = self.coordinator.runs_root / source_run_id / 'result.json'
            try:
                source_result = json.loads(result_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise ValueError('找不到所选数值组的已保存结果') from exc
            if source_result.get('run_id') != source_run_id or source_result.get('status') != 'completed':
                raise ValueError('所选数值结果无法用于视频复核')
            conditions = deepcopy(source_case['conditions'])
            template_id = source_batch['template_id']
            seed = int(source_case['seed'])
        video_payload = {'template_id': template_id, 'execution_mode': 'workflow_video',
                         'base_conditions': conditions, 'max_workers': 1, 'max_scenarios': 1}
        return self.start(video_payload, request_id, seed_override=seed, source_run_id=source_run_id)

    def _run_case(self, batch_id, index, slots):
        slot = slots.get()
        batch = self.jobs[batch_id]
        case = batch['cases'][index]
        process = None
        metadata = None
        try:
            with self.lock:
                if batch['status'] == 'cancelling':
                    case['status'] = 'cancelled'
                    self._finish(batch)
                    return
                case.update(status='running', started_at_utc=now(), worker_slot=slot)
                self._persist(batch)
            folder = self.coordinator.runs_root / case['run_id']
            metadata = {key: deepcopy(case[key]) for key in ('seed','conditions','matrix_index','replicate','replicate_count')}
            if case.get('source_numerical_run_id'):
                metadata['source_numerical_run_id'] = case['source_numerical_run_id']
            metadata.update(batch_id=batch_id, template_id=batch['template_id'], execution_mode=batch['execution_mode'],
                            created_at_utc=case['started_at_utc'])
            save(self.coordinator.runs_root / (case['run_id'] + '.metadata.json'), metadata)
            if batch['execution_mode'] == 'numerical':
                folder.mkdir(exist_ok=False)
                config = self.catalog.validate(batch['template_id'], case['conditions'], 'numerical')
                save(folder / 'conditions_config.json', config)
                argv = [self.coordinator.python, '-u', str(Path(__file__).with_name('numerical_worker.py')),
                        '--engine-root', str(self.coordinator.engine_root), '--config', str(folder / 'conditions_config.json'),
                        '--output', str(folder / 'result.json'), '--run-id', case['run_id'], '--seed', str(case['seed'])]
                with (folder / 'runner.log').open('wb') as log:
                    process = self.launcher(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                    with self.lock:
                        case['worker_pid'] = process.pid
                        self._persist(batch)
                    code = process.wait()
                result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
                if code != 0 or result.get('status') != 'completed':
                    raise ValueError(str(result.get('failure') or '数值工作进程未完成'))
            else:
                # Independent coordinator state per run; no shared robot session,
                # ledger, RNG, output directory or GPU between simultaneous jobs.
                jobs_dir = self.root / batch_id / case['run_id']
                workspace = jobs_dir / 'workspace'
                workspace.mkdir(parents=True, exist_ok=True)
                def launch(argv, **kwargs):
                    env = os.environ.copy()
                    # AppLauncher sets both RTX rendering and CUDA simulation
                    # to the selected physical GPU; CUDA_VISIBLE_DEVICES alone
                    # would not isolate the Vulkan/RTX renderer.
                    threads = max(2, min(8, (os.cpu_count() or 4) // batch['max_workers']))
                    env.update(ELECTROCHEM_GPU_ID=str(slot), ELECTROCHEM_CPU_THREADS=str(threads),
                               OMP_NUM_THREADS=str(threads))
                    return self.launcher(argv, env=env, cwd=str(workspace), **kwargs)
                from cloud_experiment_server import ExperimentCoordinator
                child = ExperimentCoordinator(self.coordinator.engine_root, self.coordinator.runner,
                    self.coordinator.python, self.coordinator.runs_root, jobs_dir, launcher=launch)
                # Runner itself creates its output folder; only metadata exists here.
                child.start(case['conditions'], 'batch_' + case['run_id'], run_id=case['run_id'], seed=case['seed'])
                with self.lock:
                    case['worker_pid'] = child.jobs[case['run_id']]['worker_pid']
                    self._persist(batch)
                while True:
                    state = child.state(case['run_id'])
                    # A renderer can publish failure before its GPU/Kit shutdown
                    # finishes. Keep its slot until the process has really exited.
                    if state['status'] not in {'starting','running','finalizing'} and child.process is None:
                        break
                    time.sleep(.5)
                if state['status'] != 'completed' or not state.get('full_experiment_completed'):
                    raise ValueError(str(state.get('failure') or '完整动作/录像未确认完成'))
                source_id = case.get('source_numerical_run_id')
                if source_id:
                    source = json.loads((self.coordinator.runs_root / source_id / 'result.json').read_text(encoding='utf-8'))
                    rendered = child._read_archive(case['run_id'])
                    rendered_errors = (rendered or {}).get('snapshot', {}).get('error_model_realized')
                    if rendered_errors != source.get('error_model_realized'):
                        raise ValueError('视频复核的误差抽样与来源数值组不一致；为避免条件不一致，已标记未通过')
            with self.lock:
                case['status'] = 'completed'
        except Exception as exc:
            with self.lock:
                case.update(status='failed', failure=str(exc))
        finally:
            slots.put(slot)
            with self.lock:
                case['finished_at_utc'] = now()
                if metadata is not None:
                    metadata.update(status=case['status'], failure=case.get('failure'), finished_at_utc=case['finished_at_utc'])
                    save(self.coordinator.runs_root / (case['run_id'] + '.metadata.json'), metadata)
                self._finish(batch)

    def _finish(self, batch):
        counts = {status: sum(case['status'] == status for case in batch['cases'])
                  for status in ('queued','running','completed','failed','cancelled','paused')}
        if not counts['queued'] and not counts['running'] and batch['status'] != 'paused':
            batch['status'] = 'completed' if not counts['failed'] and not counts['cancelled'] else ('completed_with_failures' if counts['failed'] else 'cancelled')
            batch['finished_at_utc'] = now()
        batch['message'] = f"完成 {counts['completed']}/{len(batch['cases'])} · 运行 {counts['running']} · 排队 {counts['queued']} · 失败 {counts['failed']}"
        self._persist(batch)

    def status(self, job_id=None):
        with self.lock:
            if job_id is None and self.jobs:
                job_id = max(self.jobs.values(), key=lambda row: row['created_at_utc'])['job_id']
            if job_id is None:
                return {'status': 'idle', 'job_id': None, 'message': '尚无批量任务'}
            if not re.fullmatch(r'[a-f0-9]{32}', job_id) or job_id not in self.jobs:
                raise KeyError('找不到批次')
            result = deepcopy(self.jobs[job_id])
            result.pop('fingerprint', None)
            result['completed_scenarios'] = sum(row['status'] in {'completed','failed','cancelled'} for row in result['cases'])
            result['failed_scenarios'] = sum(row['status'] == 'failed' for row in result['cases'])
            result['running_scenarios'] = sum(row['status'] == 'running' for row in result['cases'])
            if result['status'] != 'paused':
                queued = sum(row['status'] == 'queued' for row in result['cases'])
                completed = sum(row['status'] == 'completed' for row in result['cases'])
                result['message'] = f"完成 {completed}/{result['scenario_count']} · 运行 {result['running_scenarios']} · 排队 {queued} · 失败 {result['failed_scenarios']}"
            return result

    def cancel(self, job_id):
        with self.lock:
            self.status(job_id)
            batch = self.jobs[job_id]
            if batch['status'] == 'running':
                batch['status'] = 'cancelling'
                self._persist(batch)
            return self.status(job_id)

    def sweep(self, job_id=None):
        batch = self.status(job_id)
        records = [row for row in self.history.records() if row.get('batch_id') == batch.get('job_id')] if batch.get('job_id') else []
        return {'batch_type': 'experimental_design', 'available': bool(records), 'scenarios': [sweep_row(row) for row in records],
                'scenario_count': len(records), 'job_id': batch.get('job_id'), 'updated_at': batch.get('finished_at_utc', now()),
                'message': batch['message'], 'execution_mode': batch.get('execution_mode'),
                'template_id': batch.get('template_id'),
                'active_species_name': records[0].get('active_species_name') if records else None}

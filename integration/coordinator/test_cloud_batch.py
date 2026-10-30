"""CPU and HTTP tests; fake renderer receipts are never claimed as GPU evidence."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloud_experiment_server import DEFAULT_CONDITIONS, ExperimentCoordinator, create_server
from batch_jobs import CloudBatchService
from history_records import summarize_archive

ROOT = Path(__file__).resolve().parents[2]


class CloudBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.coordinator = ExperimentCoordinator(ROOT / 'electrochemistry_simulation',
            ROOT / 'integration/first_transfer/matterix_first_transfer.py', sys.executable,
            self.root / 'runs', self.root / 'jobs')
        self.service = CloudBatchService(self.coordinator, render_workers=2, numerical_workers=4)

    def tearDown(self):
        if self.service.executor:
            self.service.executor.shutdown(wait=True)
        self.temporary.cleanup()

    def payload(self, **updates):
        return {'execution_mode': 'numerical', 'template_id': 'copper_concentration_cell',
                'base_conditions': {**DEFAULT_CONDITIONS, 'left_volume_ml': 1.5, 'right_volume_ml': 1.5},
                'left_concentrations_m': [.005, .01], 'replicates': 2, 'max_workers': 2, **updates}

    def wait(self, batch_id):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status = self.service.status(batch_id)
            if status['status'] not in {'running', 'cancelling'}:
                return status
            time.sleep(.05)
        self.fail('Batch timed out')

    def test_shared_condition_validation_cartesian_volumes_and_configuration_isolation(self):
        payload = self.payload(left_volume_ml=[1.0, 1.5], right_volume_ml=[1.0, 1.5])
        original = deepcopy(payload)
        template, mode, rows = self.service.catalog.expand(payload, DEFAULT_CONDITIONS)
        self.assertEqual(len(rows), 16)
        self.assertEqual(payload, original)
        self.assertEqual(template, 'copper_concentration_cell')
        self.assertEqual(mode, 'numerical')
        rows[0]['conditions']['left_volume_ml'] = 99
        self.assertNotEqual(rows[1]['conditions']['left_volume_ml'], 99)
        for invalid in ({'left_concentrations_m': [1]}, {'temperature_c': [100], 'activity_correction_enabled': [True]},
                        {'max_scenarios': 1}, {'unknown_scan': [1]}, {'max_scenarios': True}):
            with self.assertRaises(ValueError):
                self.service.catalog.expand(self.payload(**invalid), DEFAULT_CONDITIONS)

    def test_real_numerical_workers_persist_independent_runs_and_all_results_are_queryable(self):
        batch = self.service.start(self.payload(), 'numerical_request_12345')
        end = self.wait(batch['job_id'])
        self.assertEqual(end['status'], 'completed', end)
        self.assertEqual(end['completed_scenarios'], 4)
        run_ids = {case['run_id'] for case in end['cases']}
        self.assertEqual(len(run_ids), 4)
        self.assertEqual(len({case['worker_pid'] for case in end['cases']}), 4)
        records = self.service.history.records()
        self.assertEqual({row['run_id'] for row in records}, run_ids)
        self.assertTrue(all(row['valid'] and row['conservation_passed'] for row in records))
        self.assertTrue(all(row['execution_mode'] == 'numerical' and not row['video_available'] for row in records))
        self.assertEqual(len(self.service.sweep(batch['job_id'])['scenarios']), 4)
        self.assertTrue(all(row['batch_id'] == batch['job_id'] for row in records))

    def test_idempotency_survives_service_restart_and_changed_request_is_rejected(self):
        payload = self.payload()
        first = self.service.start(payload, 'idempotency_request_12345')
        second = self.service.start(payload, 'idempotency_request_12345')
        self.assertEqual(first['job_id'], second['job_id'])
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.service.start(self.payload(replicates=3), 'idempotency_request_12345')
        self.wait(first['job_id'])
        restored = CloudBatchService(self.coordinator, 2, 4)
        self.assertEqual(restored.start(payload, 'idempotency_request_12345')['job_id'], first['job_id'])
        self.assertEqual(len(restored.jobs), 1)

    def test_other_reaction_uses_same_scheduler_and_refuses_unavailable_adapters(self):
        baseline = {**DEFAULT_CONDITIONS, 'errors_enabled': False}
        payload = self.payload(template_id='zinc_concentration_cell', base_conditions=baseline, replicates=1)
        with self.assertRaisesRegex(ValueError, '动作/录像'):
            self.service.catalog.expand({**payload, 'execution_mode': 'workflow_video'}, baseline)
        with self.assertRaisesRegex(ValueError, '误差档案'):
            self.service.catalog.expand({**payload, 'base_conditions': DEFAULT_CONDITIONS}, baseline)
        batch = self.service.start(payload, 'zinc_request_12345678')
        self.assertEqual(self.wait(batch['job_id'])['status'], 'completed')
        records = self.service.history.records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row['redox_couple'] == 'Zn²⁺/Zn' and row['active_species_name'] == 'Zn²⁺' for row in records))

    def test_parallel_limit_is_enforced_and_other_jobs_queue(self):
        lock = threading.Lock()
        live = set()
        peak = []
        original = self.service.launcher
        def launch(argv, **kwargs):
            with lock:
                live.add(threading.get_ident())
                peak.append(len(live))
            time.sleep(.15)  # Ensure both scheduler slots overlap in this test.
            process = original(argv, **kwargs)
            class Tracked:
                pid = process.pid
                def wait(self):
                    result = process.wait()
                    with lock:
                        live.discard(threading.get_ident())
                    return result
            return Tracked()
        self.service.launcher = launch
        batch = self.service.start(self.payload(replicates=3), 'parallel_request_12345')
        self.assertEqual(self.wait(batch['job_id'])['status'], 'completed')
        self.assertEqual(max(peak), 2)
        with self.assertRaises(ValueError):
            self.service.start(self.payload(max_workers=5), 'invalid_workers_12345')

    def test_failed_worker_is_preserved_and_does_not_block_other_cases(self):
        original = self.service.launcher
        failed = threading.Event()
        lock = threading.Lock()
        def launch(argv, **kwargs):
            with lock:
                if not failed.is_set():
                    failed.set()
                    raise OSError('injected worker launch failure')
            return original(argv, **kwargs)
        self.service.launcher = launch
        batch = self.service.start(self.payload(replicates=1), 'failure_request_12345')
        end = self.wait(batch['job_id'])
        self.assertEqual(end['status'], 'completed_with_failures')
        records = self.service.history.records()
        self.assertEqual(len(records), 2)
        failures = [row for row in records if not row['valid']]
        self.assertEqual(len(failures), 1)
        self.assertIsNone(failures[0]['voltage_mv'])
        self.assertIn('injected', failures[0]['failure'])
        self.assertEqual(sum(row['valid'] for row in records), 1)

    def test_cancel_does_not_interrupt_running_cases_and_cancels_the_queue(self):
        original = self.service.launcher
        released = threading.Event()
        def launch(argv, **kwargs):
            process = original(argv, **kwargs)
            class Held:
                pid = process.pid
                def wait(self):
                    released.wait(10)
                    return process.wait()
            return Held()
        self.service.launcher = launch
        batch = self.service.start(self.payload(replicates=3), 'cancel_request_12345')
        deadline = time.monotonic() + 5
        while self.service.status(batch['job_id'])['running_scenarios'] < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.service.cancel(batch['job_id'])
        released.set()
        end = self.wait(batch['job_id'])
        self.assertEqual(end['status'], 'cancelled')
        self.assertEqual(sum(case['status'] == 'completed' for case in end['cases']), 2)
        self.assertEqual(sum(case['status'] == 'cancelled' for case in end['cases']), 4)

    def test_partial_old_archive_never_becomes_a_complete_valid_voltage(self):
        snapshot = {'run_id': 'a'*32, 'status': 'completed', 'full_experiment_completed': False,
                    'conditions': DEFAULT_CONDITIONS, 'state': {}, 'voltage': {'valid': True,
                    'prediction': {'valid': True, 'voltage_mv': 9}}, 'video': {'status': 'ready'}}
        row = summarize_archive({'snapshot': snapshot, 'config': {}}, 'a'*32)
        self.assertFalse(row['valid'])
        self.assertIsNone(row['voltage_mv'])
        self.assertEqual(row['record_kind'], 'partial_workflow')

    def test_restart_pauses_unconfirmed_cases_and_never_relaunches(self):
        record = {'job_id': 'b'*32, 'request_id': 'restart_request_12345', 'fingerprint': 'x',
                  'created_at_utc': '2026-10-20', 'status': 'running', 'cases': [{'status': 'running'}, {'status':'queued'}]}
        (self.service.root / ('b'*32 + '.batch.json')).write_text(json.dumps(record), encoding='utf-8')
        restored = CloudBatchService(self.coordinator, 2, 4, launcher=lambda *a, **k: self.fail('Unexpected launch'))
        self.assertEqual(restored.status('b'*32)['status'], 'paused')
        self.assertTrue(restored.busy())

    def test_http_authentication_capabilities_history_and_batch_submission(self):
        token = 'test_batch_token_123456789012345'
        server = create_server('127.0.0.1', 0, self.coordinator, token, self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f'http://127.0.0.1:{server.server_port}'
        headers = {'Authorization': 'Bearer '+token, 'Content-Type':'application/json', 'Origin':'null', 'Idempotency-Key':'http_batch_request_12345'}
        try:
            for path in ('/history', '/capabilities', '/batch/status', '/sweep'):
                with self.assertRaises(HTTPError) as denied:
                    urlopen(endpoint + path)
                self.assertEqual(denied.exception.code, 401)
            with urlopen(Request(endpoint + '/capabilities', headers=headers)) as response:
                self.assertTrue(json.load(response)['batch'])
            with urlopen(Request(endpoint + '/batch/start', data=json.dumps(self.payload()).encode(), headers=headers)) as response:
                self.assertEqual(response.status, 202)
                batch_id = json.load(response)['job']['job_id']
            self.wait(batch_id)
            with urlopen(Request(endpoint + '/history', headers=headers)) as response:
                self.assertEqual(response.headers['Access-Control-Allow-Origin'], 'null')
                self.assertEqual(json.load(response)['count'], 4)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()

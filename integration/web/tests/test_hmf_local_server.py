import http.client
import json
import sys
import threading
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hmf_local_server import Handler
from hmf_history import save_run, load_run
from hmf_validation import assess_run
from electrochem_sim.simulator import load_config
from electrochem_sim.workflow_runner import run_workflow
from process_validation.repairs.hmf.planner import build_plan
from process_validation.repairs.hmf.execute import execute_repair
from copy import deepcopy


class LocalHmfServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.temporary = tempfile.TemporaryDirectory()
        cls.server.history_dir = cls.temporary.name
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = (response.status, response.read())
        conn.close()
        return result

    def test_stream_has_confirmed_steps_one_run_and_final_prediction(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        status, data = self.request("POST", "/api/hmf/run", b'{"seed":0}',
                                    {"Content-Type": "application/json", "Origin": origin})
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(events[0]["type"], "started")
        steps = [e for e in events if e["type"] == "step"]
        self.assertEqual([e["index"] for e in steps], list(range(1, 27)))
        self.assertEqual([e["revision"] for e in steps], list(range(1, 27)))
        self.assertEqual(len({e["run_id"] for e in events}), 1)
        self.assertEqual(events[-1]["status"], "completed")
        self.assertTrue(events[-1]["history_saved"])
        self.assertAlmostEqual(steps[-1]["event"]["prediction"]["fdca_amount_mol"], .00015)
        self.assertEqual(steps[-1]["certainty"]["status"], "uncertain")
        status, content = self.request("GET", "/api/hmf/history/" + events[0]["run_id"])
        self.assertEqual(status, 200)
        record = json.loads(content)
        self.assertEqual(record["model_id"], "hmf_tempo_fdca_endpoint_dargo2025_v1")
        self.assertEqual(record["result"]["run_id"], events[0]["run_id"])

    def test_origin_and_payload_restricted(self):
        status, _ = self.request("POST", "/api/hmf/run", b'{}',
                                 {"Content-Type": "application/json", "Origin": "https://example.com"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/hmf/run", b'{"config":{}}', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/../../secret.txt")
        self.assertEqual(status, 404)

    def test_user_conditions_change_prediction_without_changing_saved_example(self):
        def run(conditions):
            status, data = self.request("POST", "/api/hmf/run",
                json.dumps({"conditions": conditions}).encode(), {"Content-Type": "application/json"})
            self.assertEqual(status, 200)
            return [json.loads(line) for line in data.splitlines()]
        chosen = run({"hmf_mm": 37.5, "tempo_percent": 5, "koh_m": .1, "current_ma": 10})
        self.assertEqual(chosen[0]["conditions"]["hmf_mm"], 37.5)
        pred = [row for row in chosen if row.get("event", {}).get("action") == "electrolyze"][0]["event"]["prediction"]
        self.assertTrue(pred["valid"], pred)
        self.assertAlmostEqual(pred["yield_fraction"], .84)
        self.assertAlmostEqual(pred["input_hmf_mol"], .0001875)
        unsupported = run({"hmf_mm": 37.5, "tempo_percent": 7, "koh_m": .1, "current_ma": 10})
        self.assertEqual(unsupported[-1]["status"], "completed_prediction_unavailable")
        pred = [row for row in unsupported if row.get("event", {}).get("action") == "electrolyze"][0]["event"]["prediction"]
        self.assertIsNone(pred["fdca_amount_mol"])
        default = run({})
        self.assertEqual(default[0]["conditions"]["hmf_mm"], 31.25)
        bias = run({"pipette_bias_percent": -2})
        self.assertEqual(bias[-1]["status"], "completed_prediction_unavailable")
        issues = bias[-1]["validation"]["issues"]
        self.assertTrue(any(item["kind"] == "prediction_unavailable" for item in issues))
        status, _ = self.request("POST", "/api/hmf/run", b'{"conditions":{"hmf_mm":101}}', {"Content-Type":"application/json"})
        self.assertEqual(status, 400)

    def test_records_cannot_be_overwritten_or_read_outside_root(self):
        run_id = 'a' * 32
        result = {"run_id":run_id,"model_id":"hmf_tempo_fdca_endpoint_dargo2025_v1", "status":"completed"}
        save_run(self.temporary.name, result, {"hmf_mm":31.25}, {})
        with self.assertRaises(FileExistsError):
            save_run(self.temporary.name, result, {"hmf_mm":50}, {})
        self.assertEqual(load_run(self.temporary.name,run_id)["conditions"]["hmf_mm"],31.25)
        with self.assertRaises(ValueError):
            load_run(self.temporary.name,'../outside')

    def test_local_repair_preserves_prefix_rechecks_and_stops(self):
        config = load_config(Path(__file__).resolve().parents[3] / 'electrochemistry_simulation/config/hmf_fdca_single_cell_workflow.json')
        config['workflow'] = [s for s in config['workflow'] if not (s['action']=='grasp_electrode' and s['electrode_id']=='positive')]
        source = run_workflow(config)
        self.assertEqual(source['failure']['action'],'install_electrode')
        plan = build_plan(source)
        self.assertTrue(plan['eligible'], plan)
        source_before = deepcopy(source)
        branch = execute_repair(source, plan)
        self.assertEqual(source, source_before)
        self.assertEqual(branch['initial_state'], source['steps'][-1]['before'])
        self.assertEqual([s['event']['action'] for s in branch['steps']], ['grasp_electrode','install_electrode'])
        self.assertEqual(branch['status'], 'repair_segment_completed')
        self.assertTrue(branch['repair']['verification']['target_resolved'])
        self.assertTrue(branch['repair']['remaining_original_steps'])
        self.assertEqual(branch['final_state']['state_ledger']['workflow']['status'], 'paused')
        self.assertNotIn('FDCA',branch['final_state']['containers']['reactor']['amounts_mol'])
        conditions={'hmf_mm':31.25, 'tempo_percent':5, 'koh_m':.1, 'current_ma':10, 'pipette_bias_percent':0}
        save_run(self.temporary.name, source, conditions, assess_run(source))
        status, body = self.request('POST','/api/hmf/repair',json.dumps({'run_id':source['run_id']}).encode(), {'Content-Type':'application/json'})
        self.assertEqual(status,200,body)
        record=json.loads(body)
        self.assertEqual(record['result']['repair']['source_run_id'],source['run_id'])
        self.assertTrue(record['result']['repair']['verification']['target_resolved'])
        status, _ = self.request('POST','/api/hmf/repair',json.dumps({'run_id':source['run_id']}).encode(), {'Content-Type':'application/json'})
        self.assertEqual(status,409)

    def test_no_tip_repair_and_untrusted_or_unsupported_sources_blocked(self):
        config = load_config(Path(__file__).resolve().parents[3] / 'electrochemistry_simulation/config/hmf_fdca_single_cell_workflow.json')
        config['workflow'] = config['workflow'][1:]
        source = run_workflow(config)
        self.assertEqual(source['failure']['action'],'aspirate')
        plan = build_plan(source)
        self.assertTrue(plan['eligible'], plan)
        branch = execute_repair(source,plan)
        self.assertTrue(branch['repair']['verification']['target_resolved'])
        self.assertEqual([s['event']['action'] for s in branch['steps']], ['load_tip','aspirate'])
        forged = deepcopy(source); forged['config']['workflow'][0]['volume_ul'] = 999
        with self.assertRaises(ValueError): execute_repair(forged,plan)
        forged = deepcopy(source); forged['steps'][-1]['before']['state_ledger']['revision'] = 12
        self.assertFalse(build_plan(forged)['eligible'])
        enabled = deepcopy(source); enabled['config']['error_model']['enabled']=True
        self.assertFalse(build_plan(enabled)['eligible'])
        old = deepcopy(source); del old['config']
        self.assertFalse(build_plan(old)['eligible'])


if __name__ == "__main__": unittest.main()

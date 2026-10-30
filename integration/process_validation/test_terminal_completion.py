import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from process_validation.terminal_completion import terminal_correction_verified


class TerminalCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/('a'*32)
        (self.root/'process_validation').mkdir(parents=True)
        self.archive = {'snapshot':{'run_id':self.root.name,'status':'completed','state_uncertain':False,
                         'video':{'status':'ready'},'completed_steps':1,'state_revision':1,
                         'plan':[{'step_id':'measure','action':'measure_voltage'}]},
                        'pending_command':None,'events':[{'type':'step.completed','step_id':'measure'}]}
        self.manifest = {'run_id':self.root.name,'status':'segment_diagnostic_ready',
                         'correction':{'source_run_id':'b'*32},'diagnostic':{'executed_step_ids':['measure']}}
        checks = [('records','run_identity'),('records','protocol_order'),('ledger','ledger_revision'),
                  ('ledger','repair_lineage'),('ledger','repair_contract'),('ledger','conservation'),
                  ('video','timeline'),('video','full_decode'),('video','resolution'),('video','encoded_count'),
                  ('sync','trace_length'),('sync','confirmed_ledger'),('geometry','coverage'),
                  ('geometry','sampled_intersections'),('geometry','registered_repair_pose')]
        self.report={'run_id':self.root.name,'checks':[{'group':g,'code':c,'status':'pass'} for g,c in checks],
                     'issues':[{'group':'records','code':'scope','status':'review'},
                               {'group':'coverage','code':'not_implemented','status':'uncovered'}]}
        self.write()

    def write(self):
        for path,obj in [('run.json',self.archive),('video_manifest.json',self.manifest),('process_validation/report.json',self.report)]:
            (self.root/path).write_text(json.dumps(obj),encoding='utf-8')

    def test_completed_segment_recognized_without_full_video_claim(self):
        with patch('process_validation.terminal_completion.validate_report_binding',return_value=None):
            self.assertTrue(terminal_correction_verified(self.root))

    def test_partial_or_missing_or_failed_evidence_blocks(self):
        with patch('process_validation.terminal_completion.validate_report_binding',return_value=None):
            variants=[]
            a=deepcopy(self.archive); a['snapshot']['completed_steps']=0; variants.append((a,self.manifest,self.report))
            a=deepcopy(self.archive); a['pending_command']={'step':'x'}; variants.append((a,self.manifest,self.report))
            m=deepcopy(self.manifest); m['diagnostic']['executed_step_ids']=['other']; variants.append((self.archive,m,self.report))
            r=deepcopy(self.report); r['issues'].append({'group':'geometry','code':'collision','status':'fail'}); variants.append((self.archive,self.manifest,r))
            r=deepcopy(self.report); r['checks']=[c for c in r['checks'] if c['code']!='registered_repair_pose']; variants.append((self.archive,self.manifest,r))
            for a,m,r in variants:
                self.archive,self.manifest,self.report=a,m,r; self.write(); self.assertFalse(terminal_correction_verified(self.root))

    def test_unbound_report_blocks(self):
        with patch('process_validation.terminal_completion.validate_report_binding',return_value={'eligible':False}):
            self.assertFalse(terminal_correction_verified(self.root))


if __name__ == '__main__': unittest.main()

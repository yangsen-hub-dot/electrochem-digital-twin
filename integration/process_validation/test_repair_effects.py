"""Before/after evidence tests; all reports/frames here are explicit CPU doubles."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.test_repair_pipeline import fixture, seal, save, RUN, PICK, CONNECT
from process_validation.report import Report
from process_validation.repairs.dependency_plan import build_dependency_plan
from process_validation.repairs.effects.comparison import compare_records
from process_validation.repairs.effects.fingerprints import candidate_fingerprint
from process_validation.repairs.effects.storage import evaluate_repair
from process_validation.repairs.effects.history import repeated_candidate
from process_validation.repairs.cycle import record_recheck
from process_validation.repairs.effects.identities import failures

B = 'b'*32
POLICY = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))
CHECKS = [('records','run_identity'), ('records','protocol_order'), ('ledger','conservation'),
          ('geometry','coverage'), ('geometry','sampled_intersections'),
          ('sync','trace_length'), ('sync','confirmed_ledger')]


def report(run_id, issues):
    r = Report(run_id, POLICY)
    r.data.update(checker_fingerprint='c'*64, policy_fingerprint='d'*64)
    for group, code in CHECKS: r.check(group, code, 'pass', 'synthetic coverage')
    for issue in issues: r.issue(**issue)
    return r.finish()


def records(root):
    archive, _, _ = fixture(root/RUN)
    archive.update(config={'fixture': True}, seed=7)
    archive['snapshot'].update(conditions={'fixture': True}, state_uncertain=False,
        state=deepcopy(list(archive['accepted_receipts'].values())[-1]['result']['state']))
    for event in archive['events']: event['run_id'] = RUN
    fault = dict(group='geometry', code='fixture_intersection', status='fail',
                 step_id=CONNECT, body='hand', obstacle='support', message='synthetic collision')
    before = {'archive': archive, 'manifest': {'run_id': RUN, 'execution_fingerprint':'e'*64}, 'report': report(RUN,[fault])}
    dependency = build_dependency_plan(archive,[PICK,CONNECT])
    child = deepcopy(archive)
    child['events'] = child['events'][:5]
    child['accepted_receipts'] = {key: value for key,value in child['accepted_receipts'].items() if value['result']['state_revision']<=5}
    child['snapshot'].update(run_id=B, state_revision=5, completed_steps=5)
    child['snapshot']['state']=deepcopy(list(child['accepted_receipts'].values())[-1]['result']['state'])
    for event in child['events']:
        event['run_id'] = B
        event['payload']['executor_receipt']['run_id'] = B
    for row in child['accepted_receipts'].values(): row['receipt']['run_id'] = B
    correction = {'source_run_id': RUN, 'dependency_plan': dependency, 'routes': {PICK:{'candidate_id':'label','waypoints_m':[[.4,0.,.48],[.6,0.,.48]]},CONNECT:{}},
        'repair_contract': {'source_run_id': RUN, 'fault_step_ids':[CONNECT], 'dependency_plan': dependency}}
    after = {'archive': child, 'manifest': {'run_id': B, 'status':'segment_diagnostic_ready',
        'execution_fingerprint':'e'*64,'correction': correction,
        'diagnostic': {'executed_step_ids':[PICK,CONNECT], 'stop_after_step':CONNECT,
                       'restored_after_step':'ui_connect_salt_bridge','prior_actions_replayed':False},
        'frames':[{'step_id':PICK},{'step_id':CONNECT}]}, 'report': report(B,[])}
    return before,after,fault


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.before,self.after,self.fault=records(self.root)

    def compare(self): return compare_records(self.before,self.after)

    def persist(self,record):
        folder=self.root/record['archive']['snapshot']['run_id']
        save(folder/'run.json',record['archive']);save(folder/'video_manifest.json',record['manifest'])
        save(folder/'process_validation/geometry.json',{'synthetic':True})
        (folder/'process_validation/trace.jsonl').write_text('',encoding='utf-8')
        seal(folder,record['report'])
        return folder

    def test_resolution_requires_reexecution_and_positive_coverage(self):
        before=deepcopy((self.before,self.after))
        result=self.compare()
        self.assertEqual(result['status'],'improved',result)
        self.assertTrue(result['eligible_for_continuation'])
        self.assertEqual(len(result['resolved']),1)
        self.assertEqual((self.before,self.after),before)

    def test_fewer_occurrences_of_same_problem_is_not_resolution(self):
        self.before['report']['issues'][0]['occurrences']=100
        self.after['report']=report(B,[self.fault])
        self.after['report']['issues'][0]['occurrences']=1
        result=self.compare()
        self.assertEqual(result['status'],'not_improved')
        self.assertFalse(result['resolved'])

    def test_disappeared_problem_on_unexecuted_step_is_unverified(self):
        self.after['manifest']['diagnostic']['executed_step_ids']=[PICK]
        result=self.compare()
        self.assertEqual(result['status'],'inconclusive')
        self.assertTrue(result['unverified'])

    def test_missing_geometry_coverage_does_not_resolve_old_collision(self):
        self.after['report']['checks']=[r for r in self.after['report']['checks'] if r['code']!='sampled_intersections']
        self.assertEqual(self.compare()['status'],'inconclusive')

    def test_new_failure_overrides_reduced_total_failure_count(self):
        self.after['report']=report(B,[{**self.fault,'step_id':PICK,'obstacle':'new_support'}])
        result=self.compare()
        self.assertEqual(result['status'],'regressed')
        self.assertEqual(len(result['new_failures']),1)

    def test_changed_conditions_seed_or_prefix_blocks_success(self):
        good=deepcopy(self.after)
        for mutation in ('seed','conditions','prefix'):
            with self.subTest(mutation=mutation):
                self.after=deepcopy(good)
                if mutation=='seed': self.after['archive']['seed']+=1
                elif mutation=='conditions':self.after['archive']['snapshot']['conditions']['fixture']=False
                else:next(iter(self.after['archive']['accepted_receipts'].values()))['result']['state']['tip']['attached']=True
                result=self.compare()
                self.assertEqual(result['status'],'regressed')
                self.assertTrue(result['preservation_violations'])

    def test_version_changes_and_legacy_reports_remain_inconclusive(self):
        self.after['report']['checker_fingerprint']='different'
        self.assertEqual(self.compare()['status'],'inconclusive')
        self.after['report']['checker_fingerprint']='c'*64
        del self.before['report']['policy_fingerprint']
        self.assertEqual(self.compare()['status'],'inconclusive')

    def test_new_review_requires_review_instead_of_success(self):
        self.after['report']['issues'].append({**self.fault,'code':'near_fixture','status':'review'})
        self.assertEqual(self.compare()['status'],'needs_review')

    def test_known_limitations_remain_visible_without_becoming_passes(self):
        self.after['report']['checks'].append({'group':'coverage','code':'not_implemented','status':'uncovered','message':'contact force'})
        result=self.compare()
        self.assertEqual(result['status'],'improved')
        self.assertEqual(result['coverage_limits'][0]['status'],'uncovered')

    def test_wrong_execution_scope_or_uncertain_end_never_claims_success(self):
        self.after['manifest']['diagnostic']['executed_step_ids'].append('ui_measure_voltage')
        self.assertEqual(self.compare()['status'],'regressed')
        self.after['manifest']['diagnostic']['executed_step_ids']=[PICK,CONNECT]
        self.after['archive']['snapshot']['state_uncertain']=True
        self.assertEqual(self.compare()['status'],'inconclusive')

    def test_partial_resolution_is_separate_from_full_improvement(self):
        extra={**self.fault,'obstacle':'another_support'}
        self.before['report']=report(RUN,[self.fault,extra])
        self.after['report']=report(B,[extra])
        self.assertEqual(self.compare()['status'],'partially_improved')

    def test_file_binding_rejects_stale_or_foreign_reports(self):
        self.persist(self.before);folder=self.persist(self.after)
        self.assertEqual(evaluate_repair(folder)['status'],'improved')
        (folder/'run.json').write_text(json.dumps({**self.after['archive'],'seed':8}),encoding='utf-8')
        self.assertEqual(evaluate_repair(folder)['status'],'inconclusive')

    def test_missing_source_and_cyclic_source_are_not_success(self):
        folder=self.persist(self.after)
        self.assertEqual(evaluate_repair(folder)['status'],'inconclusive')
        self.after['manifest']['correction']['source_run_id']=B
        folder=self.persist(self.after)
        self.assertEqual(evaluate_repair(folder)['status'],'inconclusive')

    def test_sidecar_keeps_report_and_effect_separate(self):
        self.persist(self.before);folder=self.persist(self.after)
        original=(folder/'process_validation/report.json').read_bytes()
        cycle=record_recheck(folder,self.after['report'])
        self.assertEqual(cycle['effectiveness']['status'],'improved')
        self.assertEqual((folder/'process_validation/report.json').read_bytes(),original)

    def test_candidate_label_changes_do_not_evade_repeat_detection(self):
        profile=self.after['manifest']['correction']
        renamed=deepcopy(profile);renamed['candidate_id']='new_label'
        renamed['routes'][PICK]['candidate_id']='another_name'
        self.assertEqual(candidate_fingerprint(profile,self.after['archive']),candidate_fingerprint(renamed,self.after['archive']))
        self.after['report']=report(B,[self.fault])
        folder=self.persist(self.after)
        proposal={**renamed,'repair_step_ids':[PICK,CONNECT]}
        result=repeated_candidate(folder,proposal,self.after['archive'])
        self.assertTrue(result['blocked'],result)

    def test_changed_actual_route_is_a_distinct_candidate(self):
        self.after['report']=report(B,[self.fault]);folder=self.persist(self.after)
        proposal=deepcopy(self.after['manifest']['correction']);proposal['repair_step_ids']=[PICK,CONNECT]
        proposal['routes'][PICK]['waypoints_m'][0][0]+=.02
        self.assertFalse(repeated_candidate(folder,proposal,self.after['archive'])['blocked'])

    def test_pause_duplicate_does_not_double_count_one_failure(self):
        message='timeout '+json.dumps({'phase':'connect_lead/align'})
        self.before['archive']['events'].append({'type':'step.failed','step_id':CONNECT,'payload':{'reason':message}})
        item={'group':'records','code':'execution_failed','status':'fail','message':message,'step_id':CONNECT}
        self.before['report']=report(RUN,[item,{**item,'step_id':None}])
        self.assertEqual(len(failures(self.before['report'],self.before['archive'])),1)

    def test_new_timeout_phase_is_detected_as_a_new_problem(self):
        old={'group':'records','code':'execution_failed','status':'fail','step_id':CONNECT,
             'message':'timeout '+json.dumps({'phase':'connect_lead/align'})}
        self.before['report']=report(RUN,[old])
        self.after['report']=report(B,[{**old,'message':'timeout '+json.dumps({'phase':'connect_lead/retreat'})}])
        self.assertEqual(self.compare()['status'],'regressed')

    def test_cyclic_candidate_ancestry_stops(self):
        self.before['manifest']['correction']={'source_run_id':B}
        self.persist(self.before);folder=self.persist(self.after)
        proposal=deepcopy(self.after['manifest']['correction']);proposal['repair_step_ids']=[PICK,CONNECT]
        proposal['routes'][PICK]['waypoints_m'][0][0]+=.1
        self.assertTrue(repeated_candidate(folder,proposal,self.after['archive'])['blocked'])

    def test_altered_dependency_range_is_not_a_successful_comparison(self):
        self.persist(self.before)
        self.after['manifest']['correction']['dependency_plan']['deferred_step_ids']=[]
        folder=self.persist(self.after)
        self.assertEqual(evaluate_repair(folder)['status'],'inconclusive')

    def test_unfinished_audit_cannot_enable_external_comparison(self):
        self.persist(self.before);folder=self.persist(self.after)
        save(folder/'process_validation/audit_status.json',{'status':'running'})
        self.assertEqual(evaluate_repair(folder)['status'],'inconclusive')
        self.assertEqual(record_recheck(folder,self.after['report'])['effectiveness']['status'],'improved')
        save(self.root/RUN/'process_validation/audit_status.json',{'status':'running'})
        self.assertEqual(record_recheck(folder,self.after['report'])['effectiveness']['status'],'inconclusive')

    def test_pipeline_stops_identical_commands_despite_changed_candidate_labels(self):
        from process_validation.repairs import build_repair_plan
        from process_validation.repairs.strategies.measurement_lead import route_for_lead
        presentation={'measurement':{'targets_m':{'left':[.5,0.,.1]},
                                      'rack_grip_centers_m':{'left':[.65,-.24,.115]}}}
        self.after['manifest']['presentation']=presentation
        correction=self.after['manifest']['correction']
        correction['attempt_index']=1
        correction['candidate_id']='relabelled_old_commands'
        correction['routes']={step:route_for_lead(step,[],self.after['manifest'],2) for step in (PICK,CONNECT)}
        self.after['report']=report(B,[self.fault])
        self.persist(self.before);folder=self.persist(self.after)
        self.assertEqual(evaluate_repair(folder)['status'],'not_improved')
        candidate=build_repair_plan(folder)
        self.assertFalse(candidate['eligible'],candidate)
        self.assertEqual(candidate['status'],'repeated_or_invalid_history')

    def test_execution_version_change_is_not_parameter_improvement(self):
        self.after['manifest']['execution_fingerprint']='other-scene-version'
        self.assertEqual(self.compare()['status'],'inconclusive')


if __name__=='__main__':unittest.main()

"""Local fault-injection regressions for repair planning, without an executor."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.continuation import source_revisions
from process_validation.repairs import build_repair_plan
from process_validation.repairs.classifier import classify_failures
from process_validation.repairs.profile import validate_repair_profile
from process_validation.replay.runner import replay_case, _digest


RUN = 'a' * 32
SALT = 'ui_pick_salt_bridge'
CONNECT = 'ui_connect_left_lead'
PICK = 'ui_pick_left_lead'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def seal(folder, report):
    rows = report['checks'] + report['issues']
    counts = {status: sum(row['status'] == status for row in rows)
              for status in ('pass', 'fail', 'review', 'uncovered')}
    report.update(counts=counts,
                  status=next((s for s in ('fail', 'review', 'uncovered') if counts[s]), 'pass'),
                  source_revisions=source_revisions(folder))
    save(folder / 'process_validation/report.json', report)


def fixture(folder, step=SALT):
    ids = ['ui_mix_pair', SALT, 'ui_connect_salt_bridge', PICK, CONNECT,
           'ui_pick_right_lead', 'ui_connect_right_lead', 'ui_measure_voltage']
    archive = {'snapshot': {'run_id': RUN, 'plan': [{'step_id': i} for i in ids],
                            'completed_steps': len(ids), 'state_revision': len(ids)},
               'events': [{'type': 'step.completed', 'step_id': i} for i in ids]}
    actions = ['mix_pair', 'pick_salt_bridge', 'connect_salt_bridge', 'pick_lead', 'connect_lead',
               'pick_lead', 'connect_lead', 'measure_voltage']
    archive['accepted_receipts'] = {}
    for index, (plan_step, action, event) in enumerate(zip(archive['snapshot']['plan'], actions, archive['events']), 1):
        plan_step['action'] = action
        if 'lead' in action: plan_step['target'] = 'left_half_cell' if 'left' in plan_step['step_id'] else 'right_half_cell'
        receipt = {'run_id': RUN, 'command_id': 'confirmed_'+str(index), 'step_id': plan_step['step_id'], 'status': 'succeeded'}
        event['payload'] = {'executor_receipt': receipt}
        archive['accepted_receipts'][receipt['command_id']] = {'receipt': receipt, 'result': {
            'state_revision': index, 'state': {'cell_setup': {'held_component': None,
                'active_pipette_id': None, 'pipette_parked': True, 'left_electrode_placed': True,
                'right_electrode_placed': True, 'salt_bridge_connected': index>=3,
                'left_leads_connected':index>=5, 'right_leads_connected':index>=7}, 'tip': {'attached': False},
                'containers': {'left_half_cell': {'volume_ul':1500.},'right_half_cell':{'volume_ul':1500.}}},
            'error_model_realized': {'enabled': False}}}
    manifest = {'run_id': RUN, 'status': 'ready', 'presentation': {
        'salt_bridge': {'rack_center_m': [.5, -.1, .1], 'target_center_m': [.5, 0., .1]},
        'measurement': {'targets_m': {'left': [.5, 0., .1], 'right': [.6, 0., .1]},
                        'rack_grip_centers_m': {'left': [.65, -.24, .115],
                                               'right': [.7, -.24, .115]}}}}
    report = {'run_id': RUN, 'policy_id': 'synthetic_local_regression',
              'checks': [{'group': 'records', 'code': 'run_identity', 'status': 'pass'}],
              'issues': [{'group': 'geometry', 'code': 'fixture_intersection',
                          'status': 'fail', 'step_id': step, 'message': 'injected collision'}]}
    save(folder / 'run.json', archive)
    save(folder / 'video_manifest.json', manifest)
    save(folder / 'process_validation/geometry.json', {'run_id': RUN})
    (folder / 'process_validation/trace.jsonl').write_text('', encoding='utf-8')
    seal(folder, report)
    return archive, manifest, report


class RepairPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / RUN
        self.archive, self.manifest, self.report = fixture(self.folder)

    def write_sources(self):
        save(self.folder / 'run.json', self.archive)
        save(self.folder / 'video_manifest.json', self.manifest)
        seal(self.folder, self.report)

    def test_supported_candidate_is_bounded_and_does_not_mutate_sources(self):
        before = {str(p): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        plan = build_repair_plan(self.folder)
        self.assertTrue(plan['eligible'], plan['reason'])
        self.assertEqual(plan['checkpoint_step_id'], 'ui_mix_pair')
        self.assertEqual(plan['checkpoint_revision'], 1)
        validate_repair_profile(plan, RUN)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.folder.rglob('*') if p.is_file()})

    def test_connection_repairs_include_pick_dependency_and_earlier_checkpoint(self):
        self.report['issues'][0]['step_id'] = CONNECT
        seal(self.folder, self.report)
        plan = build_repair_plan(self.folder)
        self.assertTrue(plan['eligible'], plan['reason'])
        self.assertEqual(plan['repair_step_ids'], [PICK, CONNECT])
        self.assertEqual(plan['checkpoint_step_id'], 'ui_connect_salt_bridge')

    def test_report_from_other_run_is_blocked(self):
        self.report['run_id'] = 'b' * 32
        seal(self.folder, self.report)
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_changed_source_requires_new_audit(self):
        self.archive['seed'] = 42
        save(self.folder / 'run.json', self.archive)
        result = build_repair_plan(self.folder)
        self.assertFalse(result['eligible'])
        self.assertIn('证据', result['reason'])

    def test_missing_geometry_cannot_authorize_candidate(self):
        (self.folder / 'process_validation/geometry.json').unlink()
        seal(self.folder, self.report)
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_running_audit_blocks_saved_report(self):
        save(self.folder / 'process_validation/audit_status.json', {'status': 'running'})
        result = build_repair_plan(self.folder)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['status'], 'waiting')

    def test_in_memory_report_cannot_replace_saved_report(self):
        injected = deepcopy(self.report)
        injected['issues'][0]['step_id'] = CONNECT
        self.assertFalse(build_repair_plan(self.folder, injected)['eligible'])

    def test_failed_summary_check_is_not_ignored(self):
        self.report['checks'].append({'group': 'ledger', 'code': 'conservation', 'status': 'fail'})
        seal(self.folder, self.report)
        result = build_repair_plan(self.folder)
        self.assertFalse(result['eligible'])
        self.assertIn('conservation', result['reason'])

    def test_inconsistent_report_counts_are_blocked(self):
        self.report['counts']['fail'] = 0
        save(self.folder / 'process_validation/report.json', self.report)
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_unknown_report_status_is_blocked(self):
        self.report['checks'][0]['status'] = 'unknown'
        seal(self.folder, self.report)
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_empty_report_is_not_treated_as_success(self):
        self.report.update(checks=[], issues=[])
        seal(self.folder, self.report)
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_successful_run_does_not_generate_another_repair(self):
        self.report['issues'] = []
        seal(self.folder, self.report)
        result = build_repair_plan(self.folder)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['status'], 'not_applicable')

    def test_noncontiguous_confirmed_prefix_is_blocked(self):
        self.archive['events'][1], self.archive['events'][2] = self.archive['events'][2], self.archive['events'][1]
        self.write_sources()
        self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def test_attempt_cap_and_malformed_counter_are_blocked(self):
        for value in (3, '2', True, -1):
            with self.subTest(value=value):
                self.manifest['correction'] = {'attempt_index': value}
                self.write_sources()
                result = build_repair_plan(self.folder)
                self.assertFalse(result['eligible'])
                if value == 3:
                    self.assertEqual(result['status'], 'limit_reached')

    def test_invalid_route_target_cannot_escape_profile_contract(self):
        for target in ([100., 0., .1], ['bad', 0., .1], [float('nan'), 0., .1]):
            with self.subTest(target=target):
                self.manifest['presentation']['salt_bridge']['rack_center_m'] = target
                self.write_sources()
                self.assertFalse(build_repair_plan(self.folder)['eligible'])

    def timeout_case(self):
        failure = {'phase': 'pick_lead/align', 'failed_waypoint_index': 0,
                   'actual_ee_position_m': [.66, -.31, .2],
                   'failed_waypoint_position_m': [.674, -.24, .3]}
        message = '机械臂动作超时: ' + json.dumps(failure)
        manifest = {'motion_failure': failure, 'correction': {'routes': {
            PICK: {'expected_approach_axis_world': [0., 0., -1.]}}}}
        archive = {'snapshot': {'state': {'cell_setup': {'held_component': None}}},
                   'events': [{'type': 'step.failed', 'step_id': PICK,
                               'payload': {'executor_receipt': {'reason': message}}}]}
        issue = {'group': 'records', 'code': 'execution_failed', 'status': 'fail',
                 'message': message, 'step_id': PICK}
        report = {'run_id': RUN, 'checks': [{'group': 'records', 'code': 'scope', 'status': 'fail'}],
                  'issues': [issue, {**issue, 'step_id': None}]}
        return report, manifest, archive

    def test_known_timeout_is_bound_and_duplicate_pause_is_attributed(self):
        report, manifest, archive = self.timeout_case()
        targets, failure = classify_failures(report, RUN, manifest=manifest, archive=archive)
        self.assertIsNone(failure)
        self.assertEqual([target.step_id for target in targets], [PICK])
        self.assertEqual(len(targets[0].issues), 2)

    def test_timeout_requires_same_failed_event_and_measured_positions(self):
        for mutation in ('no_event', 'position', 'phase', 'boolean_position'):
            with self.subTest(mutation=mutation):
                report, manifest, archive = self.timeout_case()
                if mutation == 'no_event':
                    archive['events'] = []
                elif mutation == 'position':
                    manifest['motion_failure']['actual_ee_position_m'][0] += .1
                elif mutation == 'phase':
                    manifest['motion_failure']['phase'] = 'pick_lead/other'
                else:
                    manifest['motion_failure']['actual_ee_position_m'][0] = True
                targets, failure = classify_failures(report, RUN, manifest=manifest, archive=archive)
                self.assertIsNone(targets)
                self.assertFalse(failure['eligible'])

    def test_replay_missing_geometry_is_explicit_and_source_tampering_fails(self):
        case = {'case_id': 'synthetic', 'label': 'synthetic collision report', 'run_id': RUN,
                'directory': RUN, 'expected': {'classification': {'status': 'classified',
                    'families': ['salt_bridge'], 'step_ids': [SALT]},
                    'geometry': {'status': 'uncovered'}},
                'sha256': {name: _digest(self.folder / name) for name in
                           ('run.json', 'video_manifest.json', 'process_validation/report.json')}}
        result = replay_case(self.folder.parent, case, {})
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['geometry']['status'], 'uncovered')
        save(self.folder / 'run.json', {'changed': True})
        with self.assertRaisesRegex(ValueError, '已变化'):
            replay_case(self.folder.parent, case, {})


if __name__ == '__main__':
    unittest.main()

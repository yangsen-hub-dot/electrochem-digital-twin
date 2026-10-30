"""Ordering and report-scope regression tests."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from process_validation.repairs.models import RepairTarget
from process_validation.repairs.sequence import select_cohort, scoped_electrode_report
from process_validation.repairs.lineage_queue import previous_observations
from process_validation.test_repair_pipeline import RepairPipelineTests, seal, SALT, CONNECT
from process_validation.repairs import build_repair_plan


class SequenceTests(unittest.TestCase):
    def test_order_and_dependencies(self):
        ids = ['ui_pick_left_copper_electrode', 'ui_place_left_copper_electrode',
               'ui_pick_right_copper_electrode', SALT]
        archive = {'snapshot': {'plan': [{'step_id': i} for i in ids]}}
        targets = [RepairTarget(ids[i], family, ()) for i, family in
                   [(3, 'salt_bridge'), (2, 'electrode'), (1, 'electrode'), (0, 'electrode')]]
        selected, deferred = select_cohort(archive, targets)
        self.assertEqual([t.step_id for t in selected], ids[:2])
        self.assertEqual([t.step_id for t in deferred], ids[2:])

    def test_report_scope_does_not_change_original(self):
        left = 'ui_pick_left_copper_electrode'
        report = {'issues': [{'status':'fail', 'step_id':left},
                             {'status':'fail', 'step_id':SALT},
                             {'status':'review', 'step_id':SALT}], 'checks': []}
        original = deepcopy(report)
        scoped = scoped_electrode_report(report, [RepairTarget(left, 'electrode', ())])
        self.assertEqual(len(scoped['issues']), 2)
        self.assertEqual(report, original)

    def test_unknown_step_cannot_be_ordered(self):
        with self.assertRaises(ValueError):
            select_cohort({'snapshot': {'plan': [{'step_id': SALT}]}},
                          [RepairTarget(CONNECT, 'measurement_lead', ())])

    def test_intervening_family_is_not_skipped(self):
        ids = [SALT, CONNECT, 'ui_connect_salt_bridge']
        archive = {'snapshot': {'plan': [{'step_id': i} for i in ids]}}
        targets = [RepairTarget(ids[0], 'salt_bridge', ()),
                   RepairTarget(ids[1], 'measurement_lead', ()),
                   RepairTarget(ids[2], 'salt_bridge', ())]
        selected, deferred = select_cohort(archive, targets)
        self.assertEqual([t.step_id for t in selected], ids[:1])
        self.assertEqual([t.step_id for t in deferred], ids[1:])


class SequenceIntegrationTests(RepairPipelineTests):
    def test_electrode_strategy_receives_only_first_side_failures(self):
        left = 'ui_pick_left_copper_electrode'
        right = 'ui_pick_right_copper_electrode'
        for step in (left, right):
            self.archive['snapshot']['plan'].insert(1, {'step_id': step})
        self.archive['snapshot']['plan'][1:3] = [
            {'step_id': left}, {'step_id': right}]
        self.report['issues'] = [
            {'group': 'geometry', 'code': 'vertical_grasp_axis', 'status': 'fail', 'step_id': left},
            {'group': 'geometry', 'code': 'vertical_grasp_axis', 'status': 'fail', 'step_id': right},
            {'group': 'geometry', 'code': 'fixture_intersection', 'status': 'fail', 'step_id': SALT}]
        self.write_sources()
        with patch('process_validation.correction.build_correction_plan',
                   return_value={'eligible': False, 'reason': 'test'}) as strategy:
            result = build_repair_plan(self.folder)
        scoped = strategy.call_args.args[1]
        self.assertEqual([row['step_id'] for row in scoped['issues']], [left])
        self.assertEqual(result['repair_sequence']['deferred_step_ids'], [right, SALT])

    def test_later_family_is_deferred_with_original_report_preserved(self):
        self.report['issues'].append({'group':'geometry', 'code':'fixture_intersection',
            'status':'fail', 'step_id':CONNECT, 'message':'later collision'})
        seal(self.folder, self.report)
        before = (self.folder / 'process_validation/report.json').read_bytes()
        result = build_repair_plan(self.folder)
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['repair_sequence']['selected_step_ids'], [SALT])
        self.assertEqual(result['repair_sequence']['deferred_step_ids'], [CONNECT])
        self.assertTrue(result['repair_sequence']['deferred_are_unverified'])
        self.assertEqual((self.folder / 'process_validation/report.json').read_bytes(), before)

    def test_new_family_after_continuation_has_independent_attempt_budget(self):
        self.manifest['continuation'] = {'source_run_id': 'b' * 32}
        self.manifest['correction'] = {'attempt_index': 18,
                                       'strategy_family': 'electrode_pose_path'}
        self.write_sources()
        result = build_repair_plan(self.folder)
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['attempt_index'], 1)

    def test_root_observations_remain_unverified_after_a_local_retry(self):
        self.report['issues'].append({'group':'geometry', 'code':'fixture_intersection',
            'status':'fail', 'step_id':CONNECT, 'message':'previously observed'})
        seal(self.folder, self.report)
        child = self.folder.parent / ('b' * 32)
        child.mkdir()
        observed = previous_observations(child, self.archive,
                    {'correction': {'root_run_id': self.folder.name}},
                    [RepairTarget(SALT, 'salt_bridge', ())])
        self.assertEqual(observed, [CONNECT])
        (self.folder / 'run.json').write_text('{}')
        self.assertIsNone(previous_observations(child, self.archive,
                    {'correction': {'root_run_id': self.folder.name}},
                    [RepairTarget(SALT, 'salt_bridge', ())]))

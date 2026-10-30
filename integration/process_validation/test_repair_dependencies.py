"""Bounded dependency planning over synthetic protocol/receipt contracts."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.test_repair_pipeline import fixture, PICK, CONNECT, SALT, RUN
from process_validation.repairs.dependency_plan import build_dependency_plan, validate_dependency_profile
from process_validation.repairs.dependency_graph import build_graph


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.archive, _, _ = fixture(Path(self.temp.name)/RUN)

    def test_connect_includes_its_pick_and_stops_after_release(self):
        plan = build_dependency_plan(self.archive, [CONNECT])
        self.assertEqual(plan['checkpoint_step_id'], 'ui_connect_salt_bridge')
        self.assertEqual(plan['rerun_step_ids'], [PICK, CONNECT])
        self.assertEqual(plan['parameter_step_ids'], [PICK, CONNECT])
        self.assertEqual(plan['stop_after_step'], CONNECT)
        self.assertIn('ui_measure_voltage', plan['deferred_step_ids'])

    def test_pick_requires_terminal_installation_verification(self):
        plan = build_dependency_plan(self.archive, [SALT])
        self.assertEqual(plan['rerun_step_ids'], [SALT, 'ui_connect_salt_bridge'])
        self.assertEqual(plan['checkpoint_revision'], 1)

    def test_right_lead_does_not_replay_left_lead_or_measurement(self):
        plan = build_dependency_plan(self.archive, ['ui_connect_right_lead'])
        self.assertEqual(plan['rerun_step_ids'], ['ui_pick_right_lead', 'ui_connect_right_lead'])
        self.assertEqual(plan['checkpoint_step_id'], CONNECT)
        self.assertEqual(plan['deferred_step_ids'], ['ui_measure_voltage'])

    def test_registered_pose_cohort_expands_only_related_parameters(self):
        plan = build_dependency_plan(self.archive, [PICK], cohort='measurement_lead_pose')
        self.assertEqual(plan['parameter_step_ids'], [PICK, CONNECT, 'ui_pick_right_lead', 'ui_connect_right_lead'])
        self.assertEqual(plan['stop_after_step'], 'ui_connect_right_lead')

    def test_multi_fault_contiguous_segment_explains_unmodified_bridge_steps(self):
        plan = build_dependency_plan(self.archive, [SALT, 'ui_connect_right_lead'])
        self.assertEqual(plan['bridging_step_ids'], [PICK, CONNECT])
        self.assertEqual(plan['stop_after_step'], 'ui_connect_right_lead')

    def test_missing_or_wrong_component_producer_is_rejected(self):
        for mutation in ('missing', 'wrong_target'):
            archive = deepcopy(self.archive)
            if mutation == 'missing': archive['snapshot']['plan'].pop(3)
            else: archive['snapshot']['plan'][3]['target'] = 'right_half_cell'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                build_dependency_plan(archive, [CONNECT])

    def test_shared_gripper_cannot_hold_two_components(self):
        plan = self.archive['snapshot']['plan']
        plan[2], plan[3] = plan[3], plan[2]
        with self.assertRaisesRegex(ValueError, '尚未释放'):
            build_graph(plan)

    def test_unknown_action_and_measurement_repair_are_rejected(self):
        with self.assertRaises(ValueError): build_dependency_plan(self.archive, ['ui_measure_voltage'])
        self.archive['snapshot']['plan'][0]['action'] = 'unknown'
        with self.assertRaises(ValueError): build_dependency_plan(self.archive, [SALT])

    def test_no_crossing_confirmed_liquid_handling_to_bridge_faults(self):
        # Replace the intervening left-lead pair with non-gripper liquid operations.
        plan = self.archive['snapshot']['plan']
        plan[3]['action'], plan[4]['action'] = 'aspirate', 'dispense'
        with self.assertRaisesRegex(ValueError, '未授权重放'):
            build_dependency_plan(self.archive, [SALT, 'ui_connect_right_lead'])

    def test_unconfirmed_prefix_or_held_tool_checkpoint_is_rejected(self):
        self.archive['snapshot']['completed_steps'] -= 1
        with self.assertRaises(ValueError): build_dependency_plan(self.archive, [PICK])
        self.archive['snapshot']['completed_steps'] += 1
        row = next(row for row in self.archive['accepted_receipts'].values() if row['result']['state_revision']==3)
        row['result']['state']['cell_setup']['held_component'] = 'salt_bridge'
        with self.assertRaisesRegex(ValueError, '尚未释放'):
            build_dependency_plan(self.archive, [PICK])

    def test_runner_recomputes_dependency_plan_and_rejects_changed_bounds(self):
        dependency = build_dependency_plan(self.archive, [PICK, CONNECT])
        profile = {'actions': {}, 'routes': {PICK: {}, CONNECT: {}}, 'dependency_plan': dependency}
        validate_dependency_profile(profile, self.archive, dependency['checkpoint_step_id'], CONNECT)
        with self.assertRaises(ValueError):
            validate_dependency_profile(profile, self.archive, dependency['checkpoint_step_id'], 'ui_measure_voltage')
        profile['dependency_plan']['deferred_step_ids'] = []
        with self.assertRaises(ValueError): validate_dependency_profile(profile, self.archive)

    def test_assembly_and_solution_preconditions_require_checkpoint_evidence(self):
        good = deepcopy(self.archive)
        for mutation in ('bridge','electrode','solution'):
            self.archive = deepcopy(good)
            revision = 1 if mutation=='solution' else 3
            row = next(r for r in self.archive['accepted_receipts'].values() if r['result']['state_revision']==revision)
            if mutation=='bridge': row['result']['state']['cell_setup']['salt_bridge_connected']=False
            elif mutation=='electrode': row['result']['state']['cell_setup']['left_electrode_placed']=False
            else:
                self.archive['snapshot']['plan'][1]['action']='pick_electrode'
                self.archive['snapshot']['plan'][1]['target']='left_half_cell'
                self.archive['snapshot']['plan'][2]['action']='place_electrode'
                self.archive['snapshot']['plan'][2]['target']='left_half_cell'
                row['result']['state']['containers']['left_half_cell']['volume_ul']=0.
            with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'未证明所需状态'):
                build_dependency_plan(self.archive,[SALT if mutation=='solution' else PICK])


if __name__ == '__main__': unittest.main()

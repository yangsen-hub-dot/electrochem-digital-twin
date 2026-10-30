"""Disposal contract tests; all receipts are test doubles, never cloud evidence."""
import unittest
from copy import deepcopy
from transfer_session import TransferSession
from test_transfer_session import ENGINE_ROOT, conditions, successful_receipt
import test_transfer_session as legacy_tests
from tip_disposal_visuals import (falling_center, WASTE_FLOOR_Z, WASTE_XY,
                                  WASTE_RIM_Z, DISCARDED_TIP_LENGTH_M)
from video_presentation import completed_message


class DisposalTests(unittest.TestCase):
    def before_disposal(self, **updates):
        s = TransferSession(ENGINE_ROOT, conditions(**updates), seed=7,
                            executor='test_stub', include_discard_tip=True)
        legacy_tests.TransferTests().fixed_errors(s)
        for i in range(len(s.actions)):
            if s.actions[s.index].kind == 'discard_tip':
                break
            c = s.prepare()
            s.accept_receipt(successful_receipt(c, i + 1))
        return s

    def receipt(self, c, **updates):
        return successful_receipt(c, 4, tip_attached=False,
                                  waste_position_verified=True, actual_source='command_assumed', **updates)

    def test_disposal_commits_existing_residual_once(self):
        s = self.before_disposal()
        before = s.snapshot()['state']
        rng = s.rng.getstate()
        profiles = deepcopy(s.errors.snapshot())
        c = s.prepare()
        self.assertEqual(c['action'], 'discard_tip')
        self.assertAlmostEqual(c['actual_volume_ul'], 1)
        self.assertIsNone(c['error']['factor'])
        self.assertEqual(s.snapshot()['state'], before)
        self.assertEqual(c, s.prepare())
        self.assertEqual(rng, s.rng.getstate())
        self.assertEqual(profiles, s.errors.snapshot())
        receipt = self.receipt(c)
        result = s.accept_receipt(receipt)
        self.assertEqual(result['completed_steps'], len(s.actions))
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['state']['tip']['attached'])
        self.assertEqual(result['state']['tip']['liquid']['volume_ul'], 0)
        self.assertAlmostEqual(result['state']['waste']['volume_ul'], 1)
        self.assertEqual(result['state']['waste']['amounts_mol'], before['tip']['liquid']['amounts_mol'])
        self.assertEqual(result['state']['containers'], before['containers'])
        self.assertTrue(result['conservation']['passed'])
        self.assertFalse(result['voltage']['valid'])
        self.assertEqual(s.accept_receipt(receipt), result)
        self.assertEqual(s.revision, len(s.actions))

    def test_bad_confirmation_rejected_without_commit(self):
        for updates in ({'tip_attached': True}, {'waste_position_verified': False},
                        {'actual_volume_ul': 1.1}, {'actual_volume_ul': float('nan')},
                        {'actual_source': 'simulated_error'}):
            s = self.before_disposal()
            c = s.prepare()
            receipt = self.receipt(c)
            receipt.update(updates)
            with self.assertRaises(ValueError):
                s.accept_receipt(receipt)
            self.assertEqual(s.revision, len(s.actions) - 1)
            self.assertTrue(s.snapshot()['state']['tip']['attached'])

    def test_zero_residual_and_multiple_aliquots(self):
        s = TransferSession(ENGINE_ROOT, conditions(errors_enabled=False, left_volume_ml=2, left_concentration_m=.06),
                            seed=7, executor='test_stub', include_discard_tip=True)
        self.assertEqual(s.actions[-1].kind, 'discard_tip')
        self.assertGreater(len(s.actions), 4)
        for i in range(len(s.actions)):
            c = s.prepare()
            if c['action'] == 'discard_tip':
                receipt = self.receipt(c)
                receipt['simulation_time_s'] = i + 1
                self.assertEqual(c['actual_volume_ul'], 0)
            else:
                receipt = successful_receipt(c, i + 1)
            s.accept_receipt(receipt)
        self.assertFalse(s.state.tip.loaded)
        self.assertTrue(s.snapshot()['conservation']['passed'])

    def test_water_tip_disposal_is_the_final_left_preparation_step(self):
        s = TransferSession(ENGINE_ROOT, conditions(), seed=7, executor='test_stub',
                            include_discard_tip=True, include_left_water=True)
        self.assertEqual(s.actions[-1].kind, 'discard_tip')
        self.assertEqual(s.actions[-1].step_id, 'ui_left_water_discard_tip')
        water_dispenses = [a for a in s.actions if a.kind == 'dispense'
                           and '_water_' in a.step_id]
        self.assertTrue(water_dispenses)
        self.assertTrue(all(a.target == 'left_half_cell' for a in water_dispenses))
        self.assertTrue(all(a.target != 'right_half_cell' for a in s.actions
                            if a.kind == 'dispense'))
        self.assertEqual(s.scope, 'left_solution_preparation')

    def test_first_three_actions_are_numerically_unchanged(self):
        a = TransferSession(ENGINE_ROOT, conditions(), seed=7, executor='test_stub')
        b = TransferSession(ENGINE_ROOT, conditions(), seed=7, executor='test_stub', include_discard_tip=True)
        for i in range(3):
            ca, cb = a.prepare(), b.prepare()
            self.assertEqual(ca['actual_volume_ul'], cb['actual_volume_ul'])
            self.assertEqual(ca['error'], cb['error'])
            a.accept_receipt(successful_receipt(ca, i + 1))
            b.accept_receipt(successful_receipt(cb, i + 1))
            self.assertEqual(a.snapshot()['state'], b.snapshot()['state'])

    def test_visual_drop_finishes_inside_box_and_message(self):
        start = (*WASTE_XY, .195)
        self.assertEqual(falling_center(start, 0), start)
        end = falling_center(start, 1)
        self.assertAlmostEqual(end[2] - DISCARDED_TIP_LENGTH_M/2, WASTE_FLOOR_Z)
        self.assertLess(end[2] + DISCARDED_TIP_LENGTH_M/2, WASTE_RIM_Z)
        self.assertAlmostEqual(WASTE_RIM_Z, .096)
        self.assertEqual(end, falling_center(start, 2))
        self.assertIn('弃头完成', completed_message({'action': 'discard_tip', 'actual_volume_ul': 0.0}))

    def test_partial_disposal_pauses_and_does_not_dispatch_another_action(self):
        s = self.before_disposal()
        before = s.snapshot()['state']
        c = s.prepare()
        r = self.receipt(c)
        r.update(status='partial', reason='test: tool released but drop not confirmed')
        result = s.accept_receipt(r)
        self.assertEqual(result['status'], 'paused')
        self.assertTrue(result['state_uncertain'])
        self.assertEqual(result['state'], before)
        with self.assertRaises(ValueError):
            s.prepare()


if __name__ == '__main__':
    unittest.main()

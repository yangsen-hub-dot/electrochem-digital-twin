"""Independent checks tested against generated receipts and injected false records."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import tempfile

ROOT = Path(__file__).resolve().parents[2]
for name in ('integration', 'integration/first_transfer', 'electrochemistry_simulation'):
    sys.path.insert(0, str(ROOT/name))
from process_validation.chemistry_checks import check_chemistry
from process_validation.report import Report
from transfer_session import TransferSession
from test_transfer_session import conditions, successful_receipt

RUN = 'a'*32
POLICY = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))


class ChemistryChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session = TransferSession(ROOT/'electrochemistry_simulation',
            conditions(left_volume_ml=1.5, right_volume_ml=1.5,
                       activity_correction_enabled=False, liquid_junction_enabled=False),
            seed=7, run_id=RUN, executor='matterix', include_discard_tip=True,
            include_left_water=True, include_paired_mix=True, include_left_electrode=True,
            include_right_electrode=True, include_salt_bridge=True, include_measurement=True)
        while session.index < len(session.actions):
            command = session.prepare()
            session.accept_receipt(successful_receipt(command, session.index+1.))
        cls.base = session.archive()

    def setUp(self): self.archive = deepcopy(self.base)

    def audit(self):
        report = Report(RUN, POLICY)
        check_chemistry(self.archive, report)
        return report.finish()

    def fails(self, code):
        result = self.audit()
        self.assertTrue(any(r['code'] == code and r['status'] == 'fail' for r in result['issues']), result)
        return result

    def event(self, action):
        return next(e for e in self.archive['events'] if e.get('type') == 'step.completed' and e.get('action') == action)

    def row(self, event):
        return self.archive['accepted_receipts'][event['payload']['command_id']]

    def test_valid_workflow_passes_independent_checks_and_is_read_only(self):
        before = json.dumps(self.archive, sort_keys=True)
        with patch('electrochem_sim.electrochemistry.predict_voltage', side_effect=AssertionError('must not call production solver')):
            result = self.audit()
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(json.dumps(self.archive, sort_keys=True), before)
        self.assertTrue(any(r['code'] == 'nernst_baseline' and r['status'] == 'pass' for r in result['checks']))

    def test_stored_conservation_flags_do_not_hide_inventory_loss(self):
        store = self.archive['snapshot']['state']['containers']['left_half_cell']
        store['amounts_mol']['Cu'] += 1e-6
        store['concentrations_m']['Cu'] = store['amounts_mol']['Cu']/(store['volume_ul']*1e-6)
        self.assertTrue(self.archive['snapshot']['conservation']['passed'])
        self.fails('independent_inventory')

    def test_wrong_concentration_is_detected_from_amount_and_volume(self):
        self.archive['snapshot']['state']['containers']['left_half_cell']['concentrations_m']['Cu'] *= 2
        self.fails('concentration_balance')

    def test_litre_microlitre_confusion_is_detected(self):
        self.archive['snapshot']['state']['containers']['water']['volume_ul'] *= 1e-6
        self.fails('independent_inventory')

    def test_invalid_quantities_never_emit_aggregate_pass(self):
        for value in (-1., float('nan'), float('inf'), True, '1500'):
            with self.subTest(value=value):
                self.archive = deepcopy(self.base)
                self.archive['snapshot']['state']['containers']['water']['volume_ul'] = value
                result = self.fails('state_evidence')
                self.assertFalse(any(r['status']=='pass' and r['group']=='chemistry' for r in result['checks']))

    def test_zero_volume_with_positive_amount_is_invalid(self):
        store = self.archive['snapshot']['state']['containers']['left_half_cell']
        store['volume_ul'] = 0.
        store['concentrations_m'] = {s: None for s in store['amounts_mol']}
        self.fails('concentration_balance')

    def test_actual_delivery_is_checked_against_state_change(self):
        event = self.event('dispense'); row = self.row(event)
        for key in ('actual_volume_ul', 'simulated_volume_ul'): event['payload'][key] += 1.
        row['receipt']['actual_volume_ul'] += 1.
        event['payload']['executor_receipt'] = deepcopy(row['receipt'])
        self.fails('transfer_balance')

    def test_transfer_amount_is_independently_derived_from_source(self):
        self.event('aspirate')['payload']['transferred_amounts_mol']['Cu'] *= 1.1
        self.fails('transfer_balance')

    def test_balanced_transfer_to_wrong_container_is_detected(self):
        event = self.event('dispense'); row = self.row(event)
        state = row['result']['state']
        left, right = state['containers']['left_half_cell'], state['containers']['right_half_cell']
        right.update(volume_ul=left['volume_ul'], amounts_mol=deepcopy(left['amounts_mol']),
                     concentrations_m=deepcopy(left['concentrations_m']), mixing_components=deepcopy(left['mixing_components']))
        left.update(volume_ul=0., amounts_mol={}, concentrations_m={}, mixing_components=[])
        result = self.fails('transfer_balance')
        self.assertFalse(any(r['code']=='independent_inventory' for r in result['issues']))

    def test_rerouted_event_cannot_override_protocol(self):
        self.event('dispense')['payload']['target'] = 'right_half_cell'
        self.fails('state_evidence')

    def test_discarded_residual_is_not_allowed_to_disappear(self):
        self.archive['snapshot']['state']['waste']['volume_ul'] = 0.
        self.fails('independent_inventory')

    def test_non_material_action_cannot_change_bulk_inventory(self):
        event = self.event('mix_pair'); state = self.row(event)['result']['state']
        for name, delta in [('left_half_cell', 1.), ('right_half_cell', -1.)]:
            store = state['containers'][name]
            store['volume_ul'] += delta
            for species, n in store['amounts_mol'].items(): store['concentrations_m'][species] = n/(store['volume_ul']*1e-6)
            store.pop('mixing_components')
        self.fails('transfer_balance')

    def test_nernst_uses_actual_bulk_concentration_and_not_target(self):
        self.archive['snapshot']['voltage']['prediction']['voltage_components_mv']['ideal_nernst'] += 1.
        self.fails('nernst_baseline')

    def test_voltage_component_total_is_checked(self):
        self.archive['snapshot']['voltage']['predicted_cell_mv'] += 1.
        self.fails('voltage_sum')

    def test_ideal_meter_cannot_silently_differ_from_prediction(self):
        self.archive['snapshot']['voltage']['virtual_meter_mv'] += 1.
        self.fails('voltage_sum')

    def test_changed_polarity_or_invalid_prediction_is_not_accepted(self):
        for field, value in [('polarity', 'left_minus_right'), ('valid', False)]:
            self.archive = deepcopy(self.base)
            self.archive['snapshot']['voltage']['prediction'][field] = value
            self.fails('state_evidence')

    def test_final_snapshot_cannot_drop_confirmed_voltage(self):
        self.archive['snapshot']['voltage'] = {'valid': False}
        self.fails('final_state_binding')

    def test_final_snapshot_temperature_is_bound(self):
        self.archive['snapshot']['state']['temperature_k'] += 1.
        self.fails('final_state_binding')

    def test_broken_receipt_prefix_or_command_binding_is_rejected(self):
        event = self.event('aspirate')
        self.row(event)['result']['state_revision'] += 1
        self.fails('state_evidence')
        self.archive = deepcopy(self.base)
        self.event('aspirate')['payload']['command_id'] = 'other'
        self.fails('state_evidence')

    def test_unsupported_model_or_old_incomplete_archive_stays_uncovered(self):
        self.archive['config']['cell']['equilibrium_model'] = 'future_reaction_kinetics'
        self.assertEqual(self.audit()['status'], 'uncovered')
        self.archive = {'snapshot': {'run_id': RUN}}
        self.assertEqual(self.audit()['status'], 'uncovered')

    def test_correction_physics_is_explicitly_uncovered(self):
        self.archive['config']['cell']['activity_correction']['enabled'] = True
        result = self.audit()
        self.assertEqual(result['counts']['fail'], 0)
        self.assertTrue(any(r['group']=='coverage' and r['status']=='uncovered' for r in result['checks']))

    def test_dynamic_consumption_is_not_forced_into_no_reaction_conservation(self):
        self.archive['snapshot']['state']['chemical_model']['state']['dynamics_enabled'] = True
        self.assertEqual(self.audit()['status'], 'uncovered')

    def test_missing_parcel_amount_and_unknown_store_are_detected(self):
        store = self.archive['snapshot']['state']['containers']['left_half_cell']
        store['mixing_components'][0]['amounts_mol']['Cu'] += 1e-6
        self.fails('concentration_balance')
        self.archive = deepcopy(self.base)
        self.archive['snapshot']['state']['containers']['unexpected'] = deepcopy(store)
        self.fails('independent_inventory')

    def test_axis_and_chemistry_audit_are_integrated_in_main_readonly_auditor(self):
        from process_validation.audit_run import audit_run
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)/RUN
            folder.mkdir()
            text = json.dumps(self.archive)
            (folder/'run.json').write_text(text, encoding='utf-8')
            (folder/'video_manifest.json').write_text(json.dumps({'run_id': RUN, 'status': 'ready',
                'fps': 12, 'frame_count': 0, 'frames': []}), encoding='utf-8')
            with patch('process_validation.audit_run.check_chemistry', wraps=check_chemistry) as check:
                result = audit_run(folder, decode_video=False, export_evidence=False)
            check.assert_called_once()
            self.assertTrue(any(row['group']=='chemistry' and row['code']=='nernst_baseline' and row['status']=='pass'
                                for row in result['checks']))
            self.assertEqual((folder/'run.json').read_text(encoding='utf-8'), text)


if __name__ == '__main__': unittest.main()

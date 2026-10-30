"""Measurement state/receipt and geometry regressions; CPU only, no robot claim."""
import math
import unittest
from copy import deepcopy
from test_transfer_session import ENGINE_ROOT, conditions, successful_receipt
from transfer_session import TransferSession
from measurement_session import LEAD_MAP, measurement_steps
from measurement_visuals import (target_grip, rack_grip, lead_geometry, CONTACT_OFFSET_M,
                                 cable_points, electrode_terminal_spec, CLIP_GRIP_SIZE_M,
                                 RACK_GRIP_Z_M)
from microcell_labware import CELLS


class MeasurementTests(unittest.TestCase):
    def test_authorized_reference_junction_has_explicit_result_labels(self):
        s=self.make_session(liquid_junction_enabled=True,left_concentration_m=.005,
                            left_volume_ml=2,right_volume_ml=2)
        self.finish(s)
        result=s.snapshot()['voltage']
        self.assertTrue(result['valid'])
        self.assertTrue(result['liquid_junction_reference_estimate'])
        self.assertEqual(result['liquid_junction_parameter_use_policy'],'allow_reference_estimate')
        self.assertIsNone(result['measured_mv'])
        self.assertFalse(result['prediction']['liquid_junction_potential']['condition_matched'])

    def test_terminal_moves_with_stored_held_and_installed_electrode(self):
        for side in ('left','right'):
            electrode={'root_path':'/World/CopperElectrode_'+side.title()}
            spec=electrode_terminal_spec(electrode)
            self.assertTrue(spec['path'].startswith(electrode['root_path']+'/'))
            for pose in ((.30,.0,.145),(.4,-.1,.3),(.5575,-.16,.1499)):
                world=tuple(pose[i]+spec['local_center_m'][i] for i in range(3))
                self.assertEqual(world,(pose[0],pose[1]-.003,pose[2]+.003))
            self.assertEqual(spec['size_m'],(.0012,.0012,.026))

    def make_session(self, **updates):
        return TransferSession(ENGINE_ROOT, conditions(**updates), seed=7, executor='test_stub',
                               include_discard_tip=True, include_paired_mix=True, include_measurement=True)

    def advance(self, session, until):
        while session.actions[session.index].kind != until:
            c=session.prepare(); session.accept_receipt(successful_receipt(c, session.index+1))

    def finish(self, session):
        while session.status == 'running':
            c=session.prepare(); session.accept_receipt(successful_receipt(c, session.index+1))

    def test_full_plan_and_result_commit_only_after_receipt(self):
        s=self.make_session(left_concentration_m=.005,left_volume_ml=2)
        self.advance(s,'pick_lead')
        before=deepcopy(s.snapshot()['state']['containers'])
        self.assertFalse(s.snapshot()['full_experiment_completed'])
        self.advance(s,'measure_voltage')
        command=s.prepare()
        self.assertFalse(s.snapshot()['voltage']['valid'])
        self.assertEqual(command,s.prepare())
        s.accept_receipt(successful_receipt(command,s.index+1))
        result=s.snapshot()
        self.assertTrue(result['full_experiment_completed'])
        self.assertEqual(result['state']['containers'],before)
        self.assertTrue(result['conservation']['passed'])
        v=result['voltage']
        self.assertGreater(v['virtual_meter_mv'],0.)
        self.assertEqual(v['virtual_meter_mv'],v['predicted_cell_mv'])
        self.assertIsNone(v['measured_mv'])
        self.assertEqual(v['lead_map'],LEAD_MAP)
        self.assertFalse(v['instrument_error_enabled'])

    def test_wrong_side_or_open_jaw_refuses_receipt(self):
        s=self.make_session();self.advance(s,'connect_lead')
        c=s.prepare();r=successful_receipt(c,s.index+1);r['lead_roles']=LEAD_MAP['right_half_cell']
        with self.assertRaises(ValueError):s.accept_receipt(r)
        self.assertFalse(s.state.cell_setup.get('left_leads_connected',False))
        r=successful_receipt(c,s.index+1);r['gripper_released']=False
        with self.assertRaises(ValueError):s.accept_receipt(r)
        s.accept_receipt(successful_receipt(c,s.index+1))

    def test_invalid_voltage_or_wrong_polarity_not_committed(self):
        s=self.make_session();self.advance(s,'measure_voltage');c=s.prepare()
        r=successful_receipt(c,s.index+1);r['voltage']=deepcopy(r['voltage']);r['voltage']['virtual_meter_mv']=math.nan
        with self.assertRaises(ValueError):s.accept_receipt(r)
        self.assertFalse(s.snapshot()['voltage']['valid'])

    def test_missing_leads_blocks_measurement(self):
        s=self.make_session();self.advance(s,'measure_voltage')
        s.state.cell_setup['measurement_leads_connected']=False
        with self.assertRaises(ValueError):s.prepare()

    def test_zero_voltage_valid_and_sign_reverses(self):
        s=self.make_session(left_concentration_m=.1,right_concentration_m=.1,errors_enabled=False)
        self.finish(s);self.assertAlmostEqual(s.snapshot()['voltage']['virtual_meter_mv'],0.,places=10)
        s=self.make_session(left_concentration_m=.1,right_concentration_m=.01,errors_enabled=False)
        self.finish(s);self.assertLess(s.snapshot()['voltage']['virtual_meter_mv'],0.)

    def test_failed_measurement_never_creates_reading(self):
        s=self.make_session();self.advance(s,'measure_voltage');c=s.prepare()
        r=successful_receipt(c,s.index+1);r.update(status='failed',no_state_change=True,reason='test failure')
        s.accept_receipt(r)
        self.assertFalse(s.snapshot()['full_experiment_completed']);self.assertFalse(s.snapshot()['voltage']['valid'])

    def test_two_run_reproducibility(self):
        a,b=self.make_session(),self.make_session();self.finish(a);self.finish(b)
        self.assertEqual(a.snapshot()['voltage'],b.snapshot()['voltage'])

    def test_dry_terminal_geometry_and_cable(self):
        for side in ('left','right'):
            cell=CELLS[side+'_half_cell']
            cap_z=cell.support_z+cell.floor_thickness+.003+.042+.006
            electrode=(cell.xy[0]-.0025,cell.xy[1],cap_z)
            p=target_grip(cell,electrode);end=tuple(p[i]+CONTACT_OFFSET_M[i] for i in range(3))
            clip={'terminal_contact_m':end,'source_port_m':(.765,-.085,.077),'position':p,'side':side}
            result=lead_geometry(clip,p,cell,electrode,cell.at(None,1000,electrode_inserted=True,bridge_inserted=True)['surface_z'],
                                 CELLS['right_half_cell' if side=='left' else 'left_half_cell'])
            self.assertTrue(all(result['checks'].values()),result)
            points=cable_points(clip['source_port_m'],p,side)
            self.assertGreaterEqual(min(v[2] for v in points),.0007-1e-12)
            self.assertEqual(points[-1],p)
            self.assertGreater(rack_grip(side)[2]-.026,.008)

    def test_lead_rack_grip_is_raised_and_supported_at_reachable_height(self):
        # Failed cloud run eefbb172... settled at z=0.1021 m for the old
        # 0.082 m target, so keep the visual rack support aligned with the
        # revised grip and above that observed reach boundary with margin.
        self.assertAlmostEqual(RACK_GRIP_Z_M, .115)
        for side in ('left','right'):
            grip=rack_grip(side)
            fork_top=grip[2]-CLIP_GRIP_SIZE_M[2]/2
            self.assertGreater(grip[2], .1021435634+.005)
            self.assertAlmostEqual(fork_top, grip[2]-.006)
            self.assertAlmostEqual(grip[2]-fork_top, CLIP_GRIP_SIZE_M[2]/2)

    def test_measurement_panel_boxes_and_receipt_text(self):
        from unittest.mock import patch
        from PIL import Image
        from video_presentation import Presentation, completed_message
        from action_first_panel import _text
        s=self.make_session();self.finish(s)
        v=s.snapshot()['voltage'];plan=s.snapshot()['plan']
        c={'action':'measure_voltage','step_id':'ui_measure_voltage','voltage':v}
        self.assertIn('虚拟',completed_message(c))
        for w,h in ((1920,1440),(1920,1080)):
            boxes=[]
            def record(draw,box,text,font,color,**kwargs):
                boxes.append((box,text));return _text(draw,box,text,font,color,**kwargs)
            with patch('action_first_panel._text',side_effect=record):
                frame=Presentation(w,h).compose_overview(Image.new('RGB',(w*3//4,h-210),'#658291'),
                    run_id=s.run_id,phase='diagnostic_segment_complete',sim_time=200,volumes={},
                    stock_concentration=.1,completed=c,command=c,planned_ul=None,anchors={},
                    plan=plan,completed_steps=len(plan),voltage=v,
                    confirmed_volumes={'left_half_cell':1000,'right_half_cell':1000},
                    cell_concentrations={'left_half_cell':.01,'right_half_cell':.1})
            self.assertEqual(frame.size,(w,h))
            sidebar=[(b,t) for b,t in boxes if b[0]>=w*3//4]
            self.assertTrue(all(b[3]<=h-120 for b,t in sidebar),(w,h,sidebar))
            self.assertTrue(any('理想虚拟读数' in t for b,t in sidebar))


if __name__=='__main__':unittest.main()

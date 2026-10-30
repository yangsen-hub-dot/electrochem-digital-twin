"""Fault-injection regressions for diagnostic capability, not physical calibration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from copy import deepcopy
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from process_validation.geometry import transform, triangles_in_box
from process_validation.geometry_checks import check_trace, should_check_electrode_grasp_axes
from process_validation.record_checks import check_records
from process_validation.report import Report
from process_validation.video_checks import check_timeline
from process_validation.video_checks import check_video
from process_validation.scene_trace import body_ancestor_path

POLICY=json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf8'))
RUN='a'*32


class ValidationTests(unittest.TestCase):
    def test_placement_orientation_is_checked_only_during_settled_insertion(self):
        self.assertFalse(should_check_electrode_grasp_axes(
            'place_electrode', 'place_electrode/align_and_insert/safe_staging'))
        self.assertFalse(should_check_electrode_grasp_axes(
            'place_electrode', 'place_electrode/align_and_insert/orientation_probe'))
        self.assertFalse(should_check_electrode_grasp_axes(
            'place_electrode', 'place_electrode/align_and_insert/high_transit_probe'))
        self.assertTrue(should_check_electrode_grasp_axes(
            'place_electrode', 'place_electrode/align_and_insert'))
        self.assertTrue(should_check_electrode_grasp_axes(
            'pick_electrode', 'pick_electrode/align/close_gripper'))

    def test_mesh_with_same_name_as_rigid_body_keeps_parent_scale_and_rotation(self):
        path='/World/robot/panda_hand/visuals/panda_hand'
        self.assertEqual(body_ancestor_path(path,['panda_hand']),('/World/robot/panda_hand',0))

    def test_triangle_box_refines_broad_phase_and_does_not_call_aabb_overlap_collision(self):
        matrix=np.eye(4)
        self.assertTrue(triangles_in_box([[-.1,-.1,0],[.1,-.1,0],[0,.1,0]],[[0,1,2]],matrix,1,1e-6))
        self.assertFalse(triangles_in_box([[0,2,0],[2,0,0],[2,2,0]],[[0,1,2]],matrix,1,1e-6))
        self.assertFalse(triangles_in_box([[0,0,.5],[.1,0,.5],[0,.1,.5]],[[0,1,2]],matrix,1,1e-6))

    def test_usd_row_vector_transform_and_nonuniform_box_scale(self):
        m=np.eye(4);m[0,0]=2;m[3,:3]=[1,2,3]
        self.assertTrue(np.allclose(transform([[1,0,0]],m),[[3,2,3]]))
        world=transform([[-.1,-.1,0],[.1,-.1,0],[0,.1,0]],m)
        self.assertTrue(triangles_in_box(world,[[0,1,2]],np.linalg.inv(m),1,1e-6))

    def test_old_video_missing_trace_never_gets_geometry_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            report=Report(RUN,POLICY)
            check_trace(Path(folder),{}, {},POLICY,report)
            self.assertEqual(report.finish()['status'],'uncovered')

    def scene(self,folder,*,collision=False,axis=(1,0,0),mismatch=False):
        root=Path(folder)
        matrix=np.eye(4);matrix[3,0]=0 if collision else 2
        geometry={'run_id':RUN,'pose_source':'physx_articulation_body_poses','bound_body_count':1,
                  'bodies':{'finger':{'points':[[-.1,-.1,0],[.1,-.1,0],[0,.1,0]],'triangles':[[0,1,2]]}},
                  'obstacles':[{'path':'crossbar','size':1,'matrix':np.eye(4).tolist(),'inverse_matrix':np.eye(4).tolist(),
                                'corners':[[x,y,z] for x in (-.5,.5) for y in (-.5,.5) for z in (-.5,.5)]}]}
        (root/'geometry.json').write_text(json.dumps(geometry))
        frame={'frame':0,'simulation_time_s':0.,'video_time_s':0.,'step_id':'place','phase':'place_electrode/align_and_insert','gripper_gap_m':.01}
        sample={'run_id':RUN,'frame_index':0,'simulation_time_s':0.,'step_id':'place','phase':frame['phase'],
                'state_revision':0,'completed_steps':1 if mismatch else 0,'voltage_valid':False,'action':'place_electrode',
                'closing_axis_world':axis,'body_transforms':{'finger':matrix.tolist()},'moved_obstacles':[]}
        (root/'trace.jsonl').write_text(json.dumps(sample)+'\n')
        return root,{'fps':12,'frames':[frame]}

    def test_known_collision_is_failed_and_located_in_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root,manifest=self.scene(folder,collision=True)
            report=Report(RUN,POLICY);check_trace(root,{},manifest,POLICY,report)
            issue=next(row for row in report.data['issues'] if row['code']=='fixture_intersection')
            self.assertEqual(issue['status'],'fail');self.assertEqual(issue['frame_index'],0)
            self.assertEqual(issue['obstacle'],'crossbar')

    def test_centimetres_mistaken_for_metres_never_become_a_collision_or_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root,manifest=self.scene(folder,collision=True)
            p=root/'geometry.json';data=json.loads(p.read_text())
            data['bodies']['finger']['points']=(np.asarray(data['bodies']['finger']['points'])*100).tolist()
            p.write_text(json.dumps(data))
            r=Report(RUN,POLICY);check_trace(root,{},manifest,POLICY,r)
            self.assertTrue(any(i['code']=='mesh_units' and i['status']=='uncovered' for i in r.data['issues']))
            self.assertFalse(any(i['code']=='fixture_intersection' for i in r.data['issues']))
            self.assertFalse(any(i['code']=='sampled_intersections' and i['status']=='pass' for i in r.data['checks']))

    def test_vertical_closure_is_rejected_but_horizontal_closure_is_not(self):
        for axis,expected in (((0,0,1),True),((1,0,0),False)):
            with tempfile.TemporaryDirectory() as folder:
                root,manifest=self.scene(folder,axis=axis)
                report=Report(RUN,POLICY);check_trace(root,{},manifest,POLICY,report)
                self.assertEqual(any(row['code']=='vertical_grasp_axis' for row in report.data['issues']),expected)

    def test_future_result_before_receipt_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root,manifest=self.scene(folder,mismatch=True)
            report=Report(RUN,POLICY);check_trace(root,{},manifest,POLICY,report)
            self.assertTrue(any(row['code']=='confirmed_ledger' for row in report.data['issues']))

    def test_open_gripper_approach_does_not_count_as_vertical_grasp(self):
        with tempfile.TemporaryDirectory() as folder:
            root,manifest=self.scene(folder,axis=(0,0,1))
            manifest['frames'][0]['gripper_gap_m']=.08
            report=Report(RUN,POLICY);check_trace(root,{},manifest,POLICY,report)
            self.assertFalse(any(row['code']=='vertical_grasp_axis' for row in report.data['issues']))

    def test_wrong_run_and_frame_gap_are_failed(self):
        report=Report(RUN,POLICY)
        check_records({'snapshot':{'run_id':'b'*32}},{'run_id':RUN},report)
        self.assertEqual(report.finish()['status'],'fail')
        report=Report(RUN,POLICY)
        check_timeline({'fps':12,'frame_count':1,'frames':[{'frame':3,'video_time_s':0.,'simulation_time_s':0.}]},report)
        self.assertEqual(report.finish()['status'],'fail')

    def test_paused_failure_reason_is_preserved_without_editing_source(self):
        archive={'snapshot':{'run_id':RUN,'status':'paused','plan':[],'completed_steps':0,'full_experiment_completed':False},
                 'events':[{'type':'step.failed','step_id':'left_electrode','payload':{'executor_receipt':{'reason':'stir_bar_clearance=false'}}}]}
        before=deepcopy(archive);report=Report(RUN,POLICY)
        check_records(archive,{'run_id':RUN,'status':'failed'},report)
        self.assertEqual(archive,before)
        self.assertTrue(any('stir_bar_clearance=false' in row['message'] for row in report.data['issues']))


class MixingReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(cls.root/'electrochemistry_simulation'))
        sys.path.insert(0,str(cls.root/'integration/first_transfer'))

    def numerical(self,duration):
        from electrochem_sim.simulator import load_config,run_experiment
        from electrochem_sim.condition_input import build_condition_config
        from test_transfer_session import conditions
        payload=conditions(left_volume_ml=1.5,right_volume_ml=1.5,mixing_duration_s=duration)
        config=build_condition_config(load_config(self.root/'electrochemistry_simulation/config/copper_concentration_cell.json'),payload)
        return payload,run_experiment(config,77)

    def test_duration_changes_efficiency_with_same_k_and_material_balance(self):
        _,short=self.numerical(2);_,long=self.numerical(20)
        a,b=short['error_model_realized']['mixing'],long['error_model_realized']['mixing']
        self.assertEqual(a['sampled_paired_rates_s_inv'],b['sampled_paired_rates_s_inv'])
        for side in a['sampled_paired_efficiencies']:
            self.assertLess(a['sampled_paired_efficiencies'][side],b['sampled_paired_efficiencies'][side])
        self.assertTrue(short['conservation']['passed']);self.assertTrue(long['conservation']['passed'])

    def test_numerical_and_video_session_preserve_exact_draws(self):
        from transfer_session import TransferSession
        from test_transfer_session import successful_receipt
        payload,numerical=self.numerical(7)
        session=TransferSession(self.root/'electrochemistry_simulation',payload,seed=77,executor='test_stub',
                                include_discard_tip=True,include_paired_mix=True,include_measurement=True)
        while session.status=='running':
            command=session.prepare();session.accept_receipt(successful_receipt(command,sim_time=session.index+1))
        self.assertEqual(session.snapshot()['error_model_realized'],numerical['error_model_realized'])


class VideoScreeningTests(unittest.TestCase):
    def screen(self,pixels,*,moving=False,decode_fails=False):
        def frames(*args,**kwargs):
            yield {'source_size':(1920,1080),'size':(160,90),'fps':12,'duration':2.5}
            for _ in range(30):yield pixels.tobytes()
        fake=SimpleNamespace(get_ffmpeg_exe=lambda:'test-decoder',read_frames=frames)
        entries=[{'frame':i,'step_id':'motion','robot_grasp_frame_m':[i*.005 if moving else 0,0,0]} for i in range(30)]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'first_transfer.mp4').write_bytes(b'fixture')
            report=Report(RUN,POLICY)
            with patch.dict(sys.modules,{'imageio_ffmpeg':fake}),patch('process_validation.video_checks.subprocess.run',return_value=SimpleNamespace(returncode=int(decode_fails),stderr='decode error' if decode_fails else '')):
                check_video(root,{'fps':12,'frames':entries},POLICY,report)
            return report

    def test_black_overview_is_reviewed(self):
        r=self.screen(np.zeros((90,160,3),dtype=np.uint8))
        self.assertTrue(any(i['code']=='black_screen' for i in r.data['issues']))

    def test_deliberately_dark_sidebar_does_not_make_overview_black(self):
        p=np.zeros((90,160,3),dtype=np.uint8);p[:,:112]=255
        r=self.screen(p)
        self.assertFalse(any(i['code']=='black_screen' for i in r.data['issues']))

    def test_stationary_wait_is_allowed_but_frozen_picture_with_moving_pose_is_reviewed(self):
        p=np.full((90,160,3),255,dtype=np.uint8)
        self.assertFalse(any(i['code']=='frozen_while_moving' for i in self.screen(p).data['issues']))
        self.assertTrue(any(i['code']=='frozen_while_moving' for i in self.screen(p,moving=True).data['issues']))

    def test_decoder_error_is_not_marked_playable(self):
        r=self.screen(np.zeros((90,160,3),dtype=np.uint8),decode_fails=True)
        self.assertTrue(any(i['code']=='full_decode' and i['status']=='fail' for i in r.data['issues']))


if __name__=='__main__':unittest.main()

"""Check meaningful length/contact failures without claiming force validation."""
import math
import unittest
import numpy as np
from cable_constraints import (ConstrainedCable, segment_hits, SCENE_LENGTH_M,
                               project_coupled_links, project_link_outside_box,
                               polyline_self_clearance, polylines_clearance,
                               segment_distance, MODEL_ID,
                               conductor_length_violations)
from repair_execution import latest_approach_axis


class CableTests(unittest.TestCase):
    def test_nonadjacent_self_crossing_is_rejected_by_clearance_rule(self):
        crossing=np.array([[0.,0.,0.],[1.,1.,0.],[0.,1.,0.],[1.,0.,0.]])
        self.assertAlmostEqual(segment_distance(*crossing[0:2],*crossing[2:4]),0.)
        self.assertTrue(polyline_self_clearance(crossing))
        self.assertEqual(MODEL_ID,'fixed_length_gravity_relaxation_v2')

    def test_self_contact_solver_accepts_known_noncontacting_path(self):
        cable=ConstrainedCable((.765,-.085,.0724),(.674,-.246,.115))
        points=cable.update((.674,-.246,.115))
        self.assertFalse(polyline_self_clearance(points))
        self.assertFalse(cable.metrics['self_contact_violations'])

    def test_separate_parallel_conductors_keep_declared_diameter_clearance(self):
        first=np.array([[0.,0.,0.],[.1,0.,0.],[.2,0.,0.]])
        second=first.copy();second[:,1]+=.0005
        self.assertTrue(polylines_clearance(first,second))
        second[:,1]=first[:,1]+.002
        self.assertFalse(polylines_clearance(first,second))

    def test_paired_offset_cannot_silently_change_declared_conductor_length(self):
        base=np.linspace([0.,0.,.1],[.65,0.,.1],65)
        shifted=base.copy();shifted[:,1]=.1*np.sin(np.linspace(0.,np.pi,65))
        self.assertTrue(conductor_length_violations(shifted,.65))

    def test_corner_projection_clears_entire_link_and_keeps_pinned_end(self):
        box=(np.array([0.,0.,-.1]),np.array([1.,1.,.1]))
        p=np.array([[-.2,.8,.02],[.8,1.2,.02]])
        fixed=p[0].copy()
        self.assertEqual(segment_hits(p,*box)[0].tolist(),[0])
        project_link_outside_box(p,*box,np.array([0.,1.]))
        self.assertFalse(len(segment_hits(p,*box)[0]))
        self.assertTrue(np.array_equal(p[0],fixed))

    def test_opposed_corner_contacts_are_resolved_together_without_widening_gap(self):
        boxes=[(np.array([.4899,-.3511,.0024]),np.array([.6501,-.0809,.0316])),
               (np.array([.6534,-.2676,-.0016]),np.array([.7266,-.2124,.0096]))]
        points=np.array([[.653854,-.215369,.009659],[.649165,-.210255,.002242]])
        self.assertTrue(all(len(segment_hits(points,*box)[0]) for box in boxes))
        project_coupled_links(points,boxes,np.ones(2))
        self.assertTrue(all(not len(segment_hits(points,*box)[0]) for box in boxes))
        self.assertTrue(np.all(points[:,0]>.6501))
        self.assertTrue(np.all(points[:,0]<.6534))

    def test_large_trace_sample_keeps_same_frame_direction_check(self):
        sample={'run_id':'r','frame_index':7,'gripper_approach_axis_world':[0.,0.,-1.],
                'measurement_cables':{'rendered_evidence':'x'*30000}}
        self.assertEqual(latest_approach_axis(sample,frame_index=7,run_id='r'),[0.,0.,-1.])
        with self.assertRaises(ValueError):
            latest_approach_axis(sample,frame_index=8,run_id='r')
        with self.assertRaises(ValueError):
            latest_approach_axis(sample,frame_index=7,run_id='another_run')

    def test_same_cable_retains_length_across_rack_lift_and_connection(self):
        box = {'path':'IKA_proxy', 'lower_m':[.49,-.34,0], 'upper_m':[.65,-.07,.0849]}
        cable = ConstrainedCable((.765,-.085,.0724),(.674,-.246,.115),[box])
        for end in ((.674,-.246,.115),(.674,-.246,.30),(.5575,-.190,.34),(.5575,-.190,.1799)):
            points = cable.update(end)
            length = sum(math.dist(a,b) for a,b in zip(points,points[1:]))
            self.assertLessEqual(abs(length-SCENE_LENGTH_M),.0005)
            self.assertTrue(np.array_equal(points[-1],end))
            self.assertFalse(cable.metrics['envelope_intersections'])
            self.assertGreaterEqual(min(points[:,2]),.0016)
            self.assertGreater(length-math.dist(points[0],points[-1]),.15)

    def test_overlong_endpoint_is_rejected_instead_of_stretching(self):
        c=ConstrainedCable((.765,-.085,.0724),(.674,-.246,.115))
        with self.assertRaisesRegex(RuntimeError,'不延长'):
            c.update((.674,-.246,.85))
        self.assertEqual(c.length_m,.65)

    def test_supported_tray_cannot_push_slack_below_table(self):
        box={'path':'LeadTray/Base','lower_m':[.655,-.266,0.],'upper_m':[.725,-.214,.008]}
        c=ConstrainedCable((.765,-.085,.0724),(.674,-.246,.115),[box])
        c.update((.674,-.246,.115))
        self.assertGreaterEqual(c.metrics['minimum_height_m'],.0016)
        self.assertFalse(c.metrics['envelope_intersections'])
        self.assertLess(abs(c.metrics['length_error_m']),.0005)

    def test_segment_collision_is_detected_with_endpoints_outside_box(self):
        ids,_=segment_hits(np.array([[0.,0.,0.],[2.,0.,0.]]),np.array([.9,-.1,-.1]),np.array([1.1,.1,.1]))
        self.assertEqual(ids.tolist(),[0])

    def test_small_endpoint_motion_does_not_regenerate_a_different_length(self):
        c=ConstrainedCable((.765,-.085,.0724),(.674,-.246,.115))
        previous=c.update((.674,-.246,.115)).copy()
        for z in (.120,.125,.130,.135):
            current=c.update((.674,-.246,z)).copy()
            self.assertLess(np.max(np.linalg.norm(current-previous,axis=1)),.035)
            self.assertLess(abs(c.metrics['length_error_m']),.0005)
            previous=current


if __name__=='__main__': unittest.main()

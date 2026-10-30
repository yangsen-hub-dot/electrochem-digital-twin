import unittest
import numpy as np
from joint_aware_ik import SerialKinematics,posture_preference


class KinematicsTests(unittest.TestCase):
    def model(self):
        a,b=.3,-.6
        p=np.array([np.cos(a)+np.cos(a+b),np.sin(a)+np.sin(a+b),0.])
        j=np.array([[-np.sin(a)-np.sin(a+b),-np.sin(a+b)],
                    [np.cos(a)+np.cos(a+b),np.cos(a+b)],[0.,0.],
                    [0.,0.],[0.,0.],[1.,1.]])
        return SerialKinematics([a,b],p,[np.cos((a+b)/2),0.,0.,np.sin((a+b)/2)],j)

    def test_forward_and_bounded_solution_match_independent_two_link_geometry(self):
        q=np.array([.6,-.5]);angle=sum(q)
        target=[np.cos(q[0])+np.cos(angle),np.sin(q[0])+np.sin(angle),0.]
        model=self.model()
        np.testing.assert_allclose(model.forward(q)[:3,3],target,atol=1e-10)
        result=model.plan([target],[np.cos(angle/2),0.,0.,np.sin(angle/2)],
                          [[-2.,2.],[-2.,2.]])
        np.testing.assert_allclose(result['joint_targets_rad'][0],q,atol=.001)

    def test_unreachable_target_is_rejected_before_motion(self):
        with self.assertRaisesRegex(ValueError,'可达姿态'):
            self.model().plan([[4.,0.,0.]],[1.,0.,0.,0.],[[-2.,2.],[-2.,2.]])

    def test_rotated_tool_offset_matches_independent_forward_geometry(self):
        a,b=.3,-.6;angle=a+b
        offset=.107*np.array([np.cos(angle),np.sin(angle),0.])
        body=np.array([np.cos(a)+np.cos(angle),np.sin(a)+np.sin(angle),0.])
        j=np.array([[-np.sin(a)-np.sin(angle),-np.sin(angle)],
                    [np.cos(a)+np.cos(angle),np.cos(angle)],[0.,0.],
                    [0.,0.],[0.,0.],[1.,1.]])
        j[:3]+=np.cross(j[3:].T,offset).T
        model=SerialKinematics([a,b],body+offset,[np.cos(angle/2),0.,0.,np.sin(angle/2)],j)
        q=[.7,-.2];angle=sum(q)
        expected=[np.cos(q[0])+1.107*np.cos(angle),np.sin(q[0])+1.107*np.sin(angle),0.]
        np.testing.assert_allclose(model.forward(q)[:3,3],expected,atol=1e-10)

    def test_task_preferences_allow_both_high_and_low_reachable_elbows(self):
        posture={'upstream_joints':1,'point_base':[np.cos(.3),np.sin(.3),0.],
                 'world_up_base':[0.,1.,0.],'base_height_world':0.,'shoulder_point_base':[0.,0.,0.],
                 'preference':posture_preference('ui_pick_right_lead')}
        for q in ([.7,-1.3],[-.3,.4]):
            angle=sum(q);position=[np.cos(q[0])+np.cos(angle),np.sin(q[0])+np.sin(angle),0.]
            result=self.model().plan([position],[np.cos(angle/2),0.,0.,np.sin(angle/2)],
                                      [[-2.,2.],[-2.,2.]],posture)
            np.testing.assert_allclose(result['joint_targets_rad'][0],q,atol=.001)
        electrode=posture_preference('ui_pick_left_copper_electrode')
        self.assertGreater(electrode['elbow_extension_preference_weight'],0.)
        self.assertFalse(electrode['fixed_elbow_height'])
        self.assertFalse(electrode['fixed_elbow_direction'])


if __name__=='__main__': unittest.main()

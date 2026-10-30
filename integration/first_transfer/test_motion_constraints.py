"""CPU fault cases for runtime policy, not a renderer or hardware acceptance."""
from pathlib import Path
from types import SimpleNamespace
import sys
import math
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from motion_constraints import (load_policy,prepare_waypoints,PhysicalConstraintError,
    MotionMonitor,CorrectionBudget,HeldOrientation,check_joint_limits,choose_overhead_route,
    require_self_collision)
from runtime_scene_constraints import RuntimeSceneConstraints


class MotionConstraintTests(unittest.TestCase):
    def test_startup_requires_physx_self_collision(self):
        require_self_collision(True)
        for value in (False,None,1):
            with self.assertRaises(PhysicalConstraintError):
                require_self_collision(value)

    def setUp(self): self.policy=load_policy();self.q=[1.,0.,0.,0.]

    def test_trial_loop_rejected_and_only_exact_duplicates_removed(self):
        self.assertEqual(prepare_waypoints([[0,0,0],[0,0,0],[.02,0,0],[.04,0,0]]),
                         [(0.,0.,0.),(.02,0.,0.),(.04,0.,0.)])
        with self.assertRaises(PhysicalConstraintError):
            prepare_waypoints([[0,0,0],[1,0,0],[0,0,0],[1,0,0]])
        for value in ([float('nan'),0,0],[True,0,0],['1',0,0]):
            with self.assertRaises(PhysicalConstraintError): prepare_waypoints([value])

    def test_oscillation_stops_before_action_timeout(self):
        monitor=MotionMonitor(self.policy)
        with self.assertRaisesRegex(PhysicalConstraintError,'反复摆动'):
            for x in (0.,.02,-.02,.02,-.02,.02): monitor.observe('target',[x,0,0],self.q)

    def test_progress_and_small_control_noise_are_allowed(self):
        monitor=MotionMonitor(self.policy)
        for x in (0.,.01,.02,.03,.031,.030,.031,.030): monitor.observe('target',[x,0,0],self.q)
        monitor.observe('next_waypoint',[.02,0,0],self.q)

    def test_orientation_oscillation_is_not_hidden_by_stationary_tcp(self):
        monitor=MotionMonitor(self.policy)
        turn=[math.cos(math.radians(5)),0,0,math.sin(math.radians(5))]
        with self.assertRaises(PhysicalConstraintError):
            for q in (self.q,turn,self.q,turn,self.q,turn): monitor.observe('target',[0,0,0],q)

    def test_held_pose_reused_and_unrequested_turn_blocked(self):
        lock=HeldOrientation(self.policy)
        self.assertEqual(lock.choose(('lead','left'),self.q,None),self.q)
        with self.assertRaises(PhysicalConstraintError):
            lock.choose(('lead','left'),self.q,[.70710678,.70710678,0,0])
        self.assertEqual(lock.choose(None,self.q,[.70710678,.70710678,0,0]),[.70710678,.70710678,0,0])

    def test_only_converging_bounded_local_corrections_allowed(self):
        budget=CorrectionBudget(self.policy,.0001)
        self.assertFalse(budget.observe([.02,0,0]));self.assertFalse(budget.observe([.008,0,0]))
        self.assertTrue(budget.observe([.00005,0,0]))
        budget=CorrectionBudget(self.policy,.0001);budget.observe([.02,0,0])
        with self.assertRaises(PhysicalConstraintError): budget.observe([.03,0,0])
        budget=CorrectionBudget(self.policy,.0001)
        with self.assertRaises(PhysicalConstraintError):
            for value in (.03,.02,.01): budget.observe([value,0,0])

    def test_actual_joint_limits_required_and_enforced(self):
        check_joint_limits([0.,0.],[.1,.1],[[-1,1],[-1,1]],[2.,2.])
        for position,velocity,bounds in (([2.,0.],[.1,.1],[[-1,1],[-1,1]]),
                                          ([0.,0.],[3.,.1],[[-1,1],[-1,1]]),
                                          ([0.,0.],[.1,.1],[[float('nan'),1],[-1,1]])):
            with self.assertRaises(PhysicalConstraintError):
                check_joint_limits(position,velocity,bounds,[2.,2.])

    def plan(self,obstacles,start=(.3,0,.42),target=(.7,0),low=(-.03,-.03,-.08)):
        return choose_overhead_route(start,target,low,(.03,.03,.03),obstacles,self.policy)

    def test_height_is_derived_from_obstacle_and_carried_extent(self):
        obstacle={'path':'wall','lower_m':[.45,-.3,0],'upper_m':[.55,.3,.4]}
        plan=self.plan([obstacle])
        self.assertGreater(plan['height_m'],.485)
        self.assertLess(plan['height_m'],.486)
        self.assertEqual(plan['waypoints_m'][-1][:2],(.7,0))
        farther={'path':'away','lower_m':[.8,.1,0],'upper_m':[.81,.15,.4]}
        self.assertEqual(self.plan([farther])['height_m'],.42)

    def test_no_route_does_not_silently_raise_workspace_ceiling(self):
        wall={'path':'closed_wall','lower_m':[.45,-.6,0],'upper_m':[.55,.5,.6]}
        with self.assertRaises(PhysicalConstraintError): self.plan([wall])

    def test_supported_start_can_retreat_out_of_clearance_band(self):
        support={'path':'support','lower_m':[.28,-.02,0.],'upper_m':[.32,.02,.004]}
        plan=self.plan([support],start=(.3,0,.1),low=(-.01,-.01,-.095))
        self.assertGreaterEqual(plan['height_m'],.42)

    def scene(self,name,points):
        scene=object.__new__(RuntimeSceneConstraints);scene.policy=self.policy;scene.check_count=0
        scene.planned_path_checks=0
        scene.robot=SimpleNamespace(data=SimpleNamespace(joint_pos=np.zeros((1,2)),joint_vel=np.zeros((1,2)),
            joint_pos_limits=np.array([[[-1,1],[-1,1]]]),joint_vel_limits=np.array([[2.,2.]])))
        scene.hardware=[{'path':'hardware','lower_m':[-.05,-.05,.15],
                         'upper_m':[.05,.05,.25],'basis':'synthetic_solid'}]
        mesh={'path':'robot/'+name,'name':name,'triangles':np.array([[0,1,2]])}
        scene.world_meshes=lambda:iter([(mesh,np.array(points))])
        return scene

    def test_arm_link_collision_stops_not_only_finger_collision(self):
        scene=self.scene('panda_link5',[[-.01,-.01,.2],[.01,-.01,.2],[0.,.01,.2]])
        with self.assertRaises(PhysicalConstraintError): scene.check('transfer')
        scene=self.scene('panda_link5',[[.3,-.01,.2],[.32,-.01,.2],[.31,.01,.2]])
        scene.check('transfer');self.assertEqual(scene.check_count,1)

    def test_swept_hand_path_blocked_when_both_endpoints_are_clear(self):
        scene=self.scene('panda_hand',[[-.31,-.01,.2],[-.29,-.01,.2],[-.30,.01,.2]])
        with self.assertRaises(PhysicalConstraintError):
            scene.check_path([-.3,0,.2],[[.3,0,.2]],self.q,self.q)


if __name__=='__main__':unittest.main()

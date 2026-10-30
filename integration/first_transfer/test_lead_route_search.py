import unittest
from lead_route_search import search_approach
from motion_constraints import PhysicalConstraintError


class RouteSearchTests(unittest.TestCase):
    def test_selects_feasible_lower_height_and_retains_vertical_descent(self):
        start=[.4,0.,.4236];goal=[.706,-.24,.115];seen=[]
        def above_height(points):
            return next(p[2] for p in points if abs(p[0]-goal[0])<1e-8 and abs(p[1]-goal[1])<1e-8)
        def geometry(points):
            if above_height(points)<.379:raise ValueError('test obstacle rejects low approach')
        def reachable(points):
            seen.append(points)
            if above_height(points)>.421:raise ValueError('test joint workspace rejects high approach')
            return {'maximum_kinematic_error_m':0.,'minimum_joint_limit_margin_rad':.2}
        result=search_approach(start,goal,.30,.55,geometry,reachable)
        self.assertAlmostEqual(result['height_m'],.38)
        self.assertEqual(result['physical_trial_motions'],0)
        self.assertEqual(result['waypoints_m'][-1][:2],goal[:2])
        points=seen[-1]
        index=next(i for i,p in enumerate(points) if abs(p[0]-goal[0])<1e-8 and abs(p[1]-goal[1])<1e-8)
        for p in points[index:]:self.assertEqual(p[:2],goal[:2])
        self.assertEqual(points[-1],goal)

    def test_no_candidate_is_dispatched_when_all_are_rejected(self):
        def blocked(points):raise ValueError('test blocked workspace')
        with self.assertRaises(PhysicalConstraintError):
            search_approach([.4,0.,.42],[.7,-.24,.115],.3,.55,blocked,lambda points:{})


if __name__=='__main__':unittest.main()

"""Local lead travel preserves terminals and the registered pose envelope."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.repairs.strategies.measurement_lead import route_for_lead
from process_validation.repairs.profile import validate_repair_profile
from first_transfer.repair_execution import expand_route


class DirectLeadRouteTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'presentation': {'measurement': {
            'targets_m': {'left': [.5575, -.184, .1799]},
            'rack_grip_centers_m': {'left': [.674, -.24, .115]}}}}

    def test_held_lead_moves_within_source_target_horizontal_bounds(self):
        step = 'ui_connect_left_lead'
        route = route_for_lead(step, [], self.manifest, 2)
        validate_repair_profile({'source_run_id': 'a'*32, 'routes': {step: route}}, 'a'*32)
        source = [.674, -.24, .403]
        path = expand_route(source, [[source[0], source[1], .44], *route['waypoints_m']], .02)
        for point in path:
            self.assertTrue(.5575-1e-9 <= point[0] <= .674+1e-9)
            self.assertTrue(-.24-1e-9 <= point[1] <= -.184+1e-9)
        self.assertEqual(route['waypoints_m'][-1][:2], [.5575, -.184])
        self.assertTrue(route['direct_overhead'])

    def test_pick_uses_rack_and_connection_uses_terminal(self):
        for step, expected in [('ui_pick_left_lead', [.674, -.24]),
                               ('ui_connect_left_lead', [.5575, -.184])]:
            route = route_for_lead(step, [], self.manifest, 2)
            self.assertTrue(all(p[:2] == expected for p in route['waypoints_m']))
            self.assertEqual(route['expected_approach_axis_world'], [0., 0., -1.])

    def test_direct_mode_rejects_unregistered_pose_and_lateral_descent(self):
        step = 'ui_connect_left_lead'
        original = route_for_lead(step, [], self.manifest, 2)
        for mutation in ('lateral', 'orientation', 'flag'):
            route = deepcopy(original)
            if mutation == 'lateral': route['waypoints_m'][-1][0] += .05
            elif mutation == 'orientation': route['orientation_wxyz'] = [.5, .5, -.5, .5]
            else: route['direct_overhead'] = 'true'
            with self.assertRaises(ValueError):
                validate_repair_profile({'source_run_id': 'a'*32, 'routes': {step: route}}, 'a'*32)


if __name__ == '__main__': unittest.main()

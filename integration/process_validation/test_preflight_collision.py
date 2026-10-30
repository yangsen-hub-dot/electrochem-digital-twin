import sys
from pathlib import Path
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_validation.preflight_collision import prepare_scene, sample_intersections


class PreflightCollisionTests(unittest.TestCase):
    def setUp(self):
        matrix = np.eye(4).tolist()
        corners = [[x, y, z] for x in (-.05, .05)
                   for y in (-.05, .05) for z in (-.05, .05)]
        self.geometry = {
            'run_id': 'a' * 32,
            'bodies': {'/robot/hand': {
                'points': [[-.01, -.01, 0.], [.01, -.01, 0.], [0., .01, 0.]],
                'triangles': [[0, 1, 2]],
            }},
            'obstacles': [{'path': '/fixture/rack', 'shape': 'exact_usd_cube',
                           'size': .1, 'matrix': matrix, 'inverse_matrix': matrix,
                           'corners': corners}],
        }
        self.sample = {'run_id': 'a' * 32, 'body_transforms': {'/robot/hand': matrix}}
        self.prepared = prepare_scene(self.geometry, 'a' * 32)

    def test_detects_hand_mesh_intersection_with_rack(self):
        self.assertEqual(sample_intersections(self.prepared, self.sample), [
            {'body': '/robot/hand', 'obstacle': '/fixture/rack'}])

    def test_clear_pose_has_no_intersection(self):
        matrix = np.eye(4)
        matrix[3, 0] = .5
        self.sample['body_transforms']['/robot/hand'] = matrix.tolist()
        self.assertEqual(sample_intersections(self.prepared, self.sample), [])

    def test_missing_transform_fails_closed(self):
        with self.assertRaises(ValueError):
            sample_intersections(self.prepared, {'body_transforms': {}})


if __name__ == '__main__':
    unittest.main()

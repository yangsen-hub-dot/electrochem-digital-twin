import unittest
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from swept_mesh_constraints import swept_triangles_in_aabb
from runtime_scene_constraints import RuntimeSceneConstraints
from motion_constraints import load_policy,PhysicalConstraintError


class SweptMeshTests(unittest.TestCase):
    def setUp(self):
        self.vertices=np.array([[0.,0.,.2],[1.,0.,.2],[0.,1.,.2]])
        self.faces=np.array([[0,1,2]])

    def test_empty_bounding_box_corner_is_not_solid(self):
        self.assertFalse(swept_triangles_in_aabb(self.vertices,self.faces,[0,0,-.4],
                                               [.7,.7,-.01],[.8,.8,.01]))

    def test_middle_of_motion_collision_is_detected_with_clear_endpoints(self):
        low=[.2,.2,-.01];high=[.3,.3,.01]
        self.assertFalse(swept_triangles_in_aabb(self.vertices,self.faces,[0,0,0],low,high))
        self.assertFalse(swept_triangles_in_aabb(self.vertices-[0,0,.4],self.faces,[0,0,0],low,high))
        self.assertTrue(swept_triangles_in_aabb(self.vertices,self.faces,[0,0,-.4],low,high))

    def test_thin_obstacle_between_translation_samples_is_detected(self):
        vertices=np.array([[0.,0.,0.],[0.,.01,0.],[0.,0.,.01]])
        self.assertTrue(swept_triangles_in_aabb(vertices,self.faces,[1.,0.,0.],
                                              [.5001,.002,.002],[.5002,.003,.003]))

    def scene(self,low,high):
        scene=object.__new__(RuntimeSceneConstraints);scene.policy=load_policy();scene.planned_path_checks=0
        scene.hardware=[{'path':'support','lower_m':low,'upper_m':high,'basis':'test_cube'}]
        mesh={'name':'panda_rightfinger','path':'finger','triangles':self.faces}
        scene.world_meshes=lambda:iter([(mesh,self.vertices)])
        return scene

    def test_runtime_refines_and_clears_empty_corner(self):
        scene=self.scene([.7,.7,-.01],[.8,.8,.01])
        scene.check_path([0,0,.2],[[0,0,-.2]],[1,0,0,0],[1,0,0,0])
        self.assertEqual(scene.path_box_false_positives_cleared,1)

    def test_runtime_retains_actual_mesh_collision_and_object_identity(self):
        scene=self.scene([.2,.2,-.01],[.3,.3,.01])
        with self.assertRaises(PhysicalConstraintError) as error:
            scene.check_path([0,0,.2],[[0,0,-.2]],[1,0,0,0],[1,0,0,0])
        self.assertEqual(error.exception.code,'planned_hardware_mesh_intersection')
        self.assertEqual(error.exception.evidence['obstacle'],'support')

    def test_rotation_overlap_is_not_cleared_by_translation_only_test(self):
        scene=self.scene([.7,.7,-.01],[.8,.8,.01])
        with self.assertRaises(PhysicalConstraintError) as error:
            scene.check_path([0,0,.2],[[0,0,-.2]],[2**-.5,0,0,2**-.5],[1,0,0,0])
        self.assertEqual(error.exception.code,'planned_rotation_clearance_unresolved')


if __name__=='__main__':unittest.main()

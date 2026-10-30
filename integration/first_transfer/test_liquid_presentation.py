"""Closed-liquid topology and volume checks; these do not validate optics."""
import math
import inspect
import unittest
from collections import Counter
from liquid_presentation import (CELL_SOLUTION_REFERENCE_DISTANCE_M,
                                 CLEAR_LIQUID_DIFFUSE_WEIGHT,
                                 COLOURED_SOLUTION_DIFFUSE_WEIGHT,
                                 create_liquid_material, liquid_mesh_data,
                                 liquid_diffuse_weight, solution_color,
                                 stirred_liquid_mesh_data)


class LiquidMeshTests(unittest.TestCase):
    def mesh(self, volume_ul):
        r = .006
        return liquid_mesh_data({'xy': (.55,-.16), 'radius':r, 'floor_z':.010,
            'liquid_depth':volume_ul*1e-9/(math.pi*r*r)})

    def test_closed_mesh_has_two_opposite_uses_of_every_edge(self):
        mesh = self.mesh(100.)
        edges = Counter()
        cursor = 0
        for count in mesh['counts']:
            face = mesh['indices'][cursor:cursor+count]
            cursor += count
            for a,b in zip(face, face[1:]+face[:1]):
                edges[(a,b)] += 1
        for (a,b), count in edges.items():
            self.assertEqual(count,1)
            self.assertEqual(edges[(b,a)],1)

    def test_signed_volume_matches_actual_aliquot_and_final_fill(self):
        for ul in (.1, 100., 1995.607):
            mesh = self.mesh(ul)
            points = [tuple(p[i]-(.55,-.16,.01)[i] for i in range(3))
                      for p in mesh['points']]
            volume = 0.
            cursor = 0
            for count in mesh['counts']:
                ids = mesh['indices'][cursor:cursor+count]
                cursor += count
                a = points[ids[0]]
                for j in range(1,len(ids)-1):
                    b,c = points[ids[j]],points[ids[j+1]]
                    cross = (b[1]*c[2]-b[2]*c[1], b[2]*c[0]-b[0]*c[2],
                             b[0]*c[1]-b[1]*c[0])
                    volume += sum(a[k]*cross[k] for k in range(3))/6
            self.assertAlmostEqual(volume/(ul*1e-9),1.,places=8)

    def test_normals_are_unit_length_and_match_face_corners(self):
        mesh = self.mesh(100.)
        self.assertEqual(len(mesh['normals']),len(mesh['indices']))
        for n in mesh['normals']:
            self.assertAlmostEqual(sum(v*v for v in n),1.,places=12)

    def test_water_remains_clear_and_copper_is_blue(self):
        water = solution_color(0.)
        blue = solution_color(.005)
        self.assertGreater(water[0],.9)
        self.assertGreater(blue[2],blue[1])
        self.assertGreater(blue[1],blue[0])

    def test_cell_liquid_absorption_distance_avoids_excessive_path_band(self):
        self.assertEqual(CELL_SOLUTION_REFERENCE_DISTANCE_M, .040)
        self.assertEqual(inspect.signature(create_liquid_material).parameters[
            'reference_distance_m'].default, CELL_SOLUTION_REFERENCE_DISTANCE_M)
        self.assertEqual(liquid_diffuse_weight(0.), CLEAR_LIQUID_DIFFUSE_WEIGHT)
        self.assertEqual(liquid_diffuse_weight(.01), COLOURED_SOLUTION_DIFFUSE_WEIGHT)
        self.assertAlmostEqual(COLOURED_SOLUTION_DIFFUSE_WEIGHT+
                               (1.-COLOURED_SOLUTION_DIFFUSE_WEIGHT), 1.)

    def test_liquid_boundary_does_not_coincide_with_glass_inner_wall(self):
        mesh = self.mesh(100.)
        radii = [math.hypot(x-.55,y+.16) for x,y,_ in mesh['points']]
        self.assertLess(max(radii),.006)
        self.assertGreater(max(radii),.00597)
        self.assertGreater(min(p[2] for p in mesh['points']),.010)

    def test_stirred_surface_moves_without_changing_volume_or_opening_mesh(self):
        geometry={'xy':(.55,-.16),'radius':.006,'floor_z':.0949,
                  'liquid_depth':2000e-9/(math.pi*.006**2)}
        previous=None
        for phase in (0.,.25,.5):
            mesh=stirred_liquid_mesh_data(geometry,phase)
            if previous is not None:
                self.assertNotEqual(previous,mesh['points'])
            previous=mesh['points']
            points=[(p[0]-.55,p[1]+.16,p[2]-.0949) for p in mesh['points']]
            edges=Counter(); cursor=0; terms=[]
            for count in mesh['counts']:
                ids=mesh['indices'][cursor:cursor+count]; cursor+=count
                for a,b in zip(ids,ids[1:]+ids[:1]): edges[a,b]+=1
                a=points[ids[0]]
                for j in range(1,len(ids)-1):
                    b,c=points[ids[j]],points[ids[j+1]]
                    cross=(b[1]*c[2]-b[2]*c[1],b[2]*c[0]-b[0]*c[2],b[0]*c[1]-b[1]*c[0])
                    terms.append(sum(a[k]*cross[k] for k in range(3))/6)
            self.assertAlmostEqual(math.fsum(terms)/2000e-9,1.,places=8)
            self.assertTrue(all(edges[b,a]==count==1 for (a,b),count in edges.items()))
            self.assertEqual(len(mesh['normals']),len(mesh['indices']))


if __name__ == '__main__':
    unittest.main()

"""Small-volume half-cell geometry, fixtures and future operation interfaces.

All dimensions are metres; liquid volumes enter in microlitres. The hollow
glass mesh and operating poses use the same cavity dimensions. This is a
project-designed vessel, not manufacturer CAD or validated contact mechanics.
No chemistry, error sampling or inventory changes occur in this module.
"""
from dataclasses import dataclass
import math

BRIDGE_TIP_CLEARANCE_M = .004
BRIDGE_OUTER_RADIUS_M = .0006


@dataclass(frozen=True)
class Microcell:
    xy: tuple
    support_z: float = .008
    inner_radius: float = .006
    wall: float = .001
    floor_thickness: float = .002
    cavity_height: float = .035
    headspace: float = .004
    nozzle_offset_m: float = .172

    @property
    def working_capacity_ul(self):
        return math.pi * self.inner_radius**2 * (self.cavity_height-self.headspace) * 1e9

    def at(self, position, volume_ul, electrode_inserted=False, bridge_inserted=False):
        if not math.isfinite(volume_ul) or not 0 <= volume_ul <= self.working_capacity_ul:
            raise ValueError(f'小体积电化学池工作容量为 {self.working_capacity_ul/1000:.3f} mL，不能容纳 {volume_ul/1000:.3f} mL')
        floor = self.support_z + self.floor_thickness
        depth = volume_ul * 1e-9 / (math.pi * self.inner_radius**2)
        if electrode_inserted or bridge_inserted:
            # Each solid has its own starting height. Solve the piecewise
            # displaced volume without adding liquid to the authority ledger.
            solids = ([(.0004 * .003, .003)] if electrode_inserted else [])
            if bridge_inserted:
                solids.append((math.pi * BRIDGE_OUTER_RADIUS_M**2, BRIDGE_TIP_CLEARANCE_M))
            pool_area = math.pi * self.inner_radius**2
            for _ in range(3):
                submerged = [(area, start) for area, start in solids if depth > start]
                depth = ((volume_ul * 1e-9 - sum(area * start for area, start in submerged))
                         / (pool_area - sum(area for area, start in submerged)))
        rim = floor + self.cavity_height
        return {'xy': self.xy, 'floor_z': floor, 'rim_z': rim,
                'radius': self.inner_radius, 'liquid_depth': depth,
                'surface_z': floor+depth,
                'aspirate_ee_z': floor+max(.0003, depth*.5)+self.nozzle_offset_m,
                # Dispense above the highest permitted liquid level, inside rim.
                'dispense_ee_z': rim-.002+self.nozzle_offset_m,
                'working_capacity_ul': self.working_capacity_ul,
                # Controller stop tolerance and actual mouth clearance are
                # separate: this robot settles with ~3.6 mm vertical offset.
                # Motion completion follows the controller's 5 mm IK tolerance;
                # the independent 2 mm horizontal aperture guard below the
                # action still decides whether the observed pose is safe.
                'position_tolerance_m': .005,
                'horizontal_tolerance_m': .002,
                'dispense_nozzle_max_z': rim+.004}

    def interfaces(self):
        """Reserved poses, not already installed electrodes/bridge or actions."""
        x, y = self.xy
        floor = self.support_z+self.floor_thickness
        rim = floor+self.cavity_height
        return {'dispense_axis_xy_m': [x, y],
                'electrode_axis_xy_m': [x-.0025, y],
                'salt_bridge_axis_xy_m': [x+.0025, y],
                'proposed_electrode_radius_m': .0005,
                'proposed_bridge_outer_radius_m': .001,
                'insertion_limits_z_m': [floor+.001, rim],
                'status': 'reserved_geometry_not_executed'}


# For the next continuous workflow, the left cell sits on a thin adapter that
# contacts the IKA plate top. Both pools stay on one common adapter above the
# housing; this is a visual setup decision, not evidence of magnetic coupling.
IKA_PLATE_TOP_Z_M = .0849
CELL_ADAPTER_HEIGHT_M = .008
CELLS = {'left_half_cell': Microcell((.56, -.16), support_z=IKA_PLATE_TOP_Z_M+CELL_ADAPTER_HEIGHT_M),
         'right_half_cell': Microcell((.58, -.16), support_z=IKA_PLATE_TOP_Z_M+CELL_ADAPTER_HEIGHT_M)}


def create_microcells(stage, root_path, glass):
    """Create two truly hollow, open vessels on one table-contacting holder."""
    from pxr import Gf, UsdGeom, UsdShade
    parent = str(root_path)+'/Microcells'

    def solid(name, position, scale, color):
        cube = UsdGeom.Cube.Define(stage, parent+'/'+name)
        cube.CreateSizeAttr(1)
        cube.AddTranslateOp().Set(Gf.Vec3d(*position))
        cube.AddScaleOp().Set(Gf.Vec3f(*scale))
        cube.CreateDisplayColorAttr([Gf.Vec3f(*color)])
        return cube

    # A common adapter supports both pools; leaving the right pool at bench
    # height would put it inside the full-size IKA housing footprint.
    support_top = CELLS['left_half_cell'].support_z
    solid('Holder', (.57, -.16, support_top-.004), (.080, .040, .008), (.66, .77, .80))
    # Keep the matte backdrop behind both cells from the presentation camera's
    # negative-Y side; it must not occlude the liquid or the cell interiors.
    solid('Backdrop', (.57, -.143, support_top+.022), (.075, .001, .044), (.68, .72, .73))
    records = {}
    for name, cell in CELLS.items():
        x, y = cell.xy
        inner, outer = cell.inner_radius, cell.inner_radius+cell.wall
        bottom = cell.support_z
        floor, rim = bottom+cell.floor_thickness, bottom+cell.floor_thickness+cell.cavity_height
        points = []
        segments = 128
        for radius, z in ((outer,bottom), (outer,rim), (inner,rim), (inner,floor)):
            points.extend((x+radius*math.cos(i*math.tau/segments),
                           y+radius*math.sin(i*math.tau/segments), z) for i in range(segments))
        indices, counts, normals = [], [], []
        for ring in range(3):
            for i in range(segments):
                j = (i+1) % segments
                indices.extend((ring*segments+i, ring*segments+j,
                                (ring+1)*segments+j, (ring+1)*segments+i))
                counts.append(4)
                # Refraction needs smooth cylindrical normals, with a sharp rim.
                if ring == 1:
                    normals.extend([(0., 0., 1.)]*4)
                else:
                    sign = 1. if ring == 0 else -1.
                    for k in (i, j, j, i):
                        a = k*math.tau/segments
                        normals.append((sign*math.cos(a), sign*math.sin(a), 0.))
        points.extend(((x,y,floor),(x,y,bottom)))
        for i in range(segments):
            j = (i+1) % segments
            indices.extend((4*segments,3*segments+i,3*segments+j,
                            4*segments+1,j,i))
            counts.extend((3,3))
            normals.extend([(0.,0.,1.)]*3+[(0.,0.,-1.)]*3)
        mesh = UsdGeom.Mesh.Define(stage, parent+'/'+name)
        mesh.CreatePointsAttr([Gf.Vec3f(*p) for p in points])
        mesh.CreateFaceVertexCountsAttr(counts)
        mesh.CreateFaceVertexIndicesAttr(indices)
        mesh.CreateSubdivisionSchemeAttr('none')
        mesh.CreateDoubleSidedAttr(False)
        mesh.CreateNormalsAttr([Gf.Vec3f(*v) for v in normals])
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(glass)
        # A shallow support collar contacts the holder and only grips the base.
        # Glass bottom remains at exactly the holder's top plane.
        records[name] = {'model': 'project_designed_hollow_microcell_v1',
                         'inner_diameter_m': inner*2, 'outer_diameter_m': outer*2,
                         'floor_z_m': floor, 'rim_z_m': rim,
                         'glass_bottom_z_m': bottom, 'support_top_z_m': cell.support_z,
                         'working_capacity_ul': cell.working_capacity_ul,
                         'interfaces': cell.interfaces(), 'prim_path': str(mesh.GetPath())}
    return {'cells': records,
            'holders': {name: {'bottom_z_m': cell.support_z-.008,
                               'top_z_m': cell.support_z,
                               'support_contact_verified_by_geometry': True}
                        for name, cell in CELLS.items()},
            'collision_model': 'fixed_visual_work_area_checks',
            'reuse_review': 'ChemLab-Assets catalogue checked; no standalone 1-2 mL twin half-cell found; Matterix source vessels retained'}

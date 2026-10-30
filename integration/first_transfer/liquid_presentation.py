"""Closed, refractive liquid visuals driven by the authoritative volume ledger.

Coordinates are metres and concentration is mol/L. This module changes only
rendering: no inventory, operation errors, fluid dynamics or chemistry. Colours
are qualitative visibility cues, not calibrated copper absorbance measurements.
"""
import math
from runtime_assets import kit_material_path

CELL_SOLUTION_REFERENCE_DISTANCE_M = .040
CLEAR_LIQUID_DIFFUSE_WEIGHT = .025
COLOURED_SOLUTION_DIFFUSE_WEIGHT = .14


def solution_color(concentration_m):
    if not math.isfinite(concentration_m) or concentration_m < 0:
        raise ValueError('Invalid concentration for liquid appearance')
    if concentration_m == 0:
        return (.96, .985, 1.0)
    fraction = min(1., math.sqrt(concentration_m / .1))
    return tuple(a + (b-a)*fraction for a, b in
                 zip((.52, .84, .98), (.35, .75, .96)))


def liquid_diffuse_weight(concentration_m):
    """Keep clear water nearly dielectric; make dissolved Cu tint legible throughout."""
    solution_color(concentration_m)  # validates the concentration
    return (COLOURED_SOLUTION_DIFFUSE_WEIGHT if concentration_m > 0
            else CLEAR_LIQUID_DIFFUSE_WEIGHT)


def create_liquid_material(stage, path, concentration_m=0.,
                           reference_distance_m=CELL_SOLUTION_REFERENCE_DISTANCE_M):
    """Full-opacity MDL dielectric; transparency comes from light transmission.

    No alpha blending or self illumination. The reference distance is a visual
    choice in metres, chosen for readability at the vessel scale. The previous
    10 mm cell value produced an overly strong path-length color band in the
    shallow microcells. 40 mm softens that angular contrast; a small
    concentration-dependent diffuse contribution keeps the full solution
    column visibly tinted, while clear water remains mostly transmissive. Both
    are presentation choices, not absorbance measurements, and change no
    quantities.
    """
    from pxr import Gf, Sdf, UsdShade
    mdl = kit_material_path('OmniSurface.mdl')
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path+'/Surface')
    shader.CreateImplementationSourceAttr().Set('sourceAsset')
    shader.SetSourceAsset(Sdf.AssetPath(str(mdl)), 'mdl')
    shader.SetSourceAssetSubIdentifier('OmniSurface', 'mdl')
    for name, value in {
        'diffuse_reflection_weight': liquid_diffuse_weight(concentration_m),
        'specular_reflection_weight': 1.,
        'specular_reflection_roughness': .055,
        'specular_reflection_ior': 1.333,
        'specular_transmission_weight': 1.-liquid_diffuse_weight(concentration_m),
        'specular_transmission_scattering_depth': reference_distance_m,
        'geometry_opacity': 1.,
        'emission_weight': 0.,
    }.items():
        shader.CreateInput(name, Sdf.ValueTypeNames.Float).Set(value)
    shader.CreateInput('enable_specular_transmission', Sdf.ValueTypeNames.Bool).Set(True)
    shader.CreateInput('thin_walled', Sdf.ValueTypeNames.Bool).Set(False)
    shader.CreateInput('specular_transmission_scattering_color',
                       Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.))
    shader.CreateOutput('out', Sdf.ValueTypeNames.Token)
    material.CreateSurfaceOutput('mdl').ConnectToSource(shader.ConnectableAPI(), 'out')
    update_liquid_material(material, concentration_m)
    return material


def update_liquid_material(material, concentration_m):
    from pxr import Gf, Sdf, UsdShade
    shader = UsdShade.Shader(material.GetPrim().GetStage().GetPrimAtPath(
        str(material.GetPath())+'/Surface'))
    color = Gf.Vec3f(*solution_color(concentration_m))
    for name in ('specular_transmission_color', 'diffuse_reflection_color'):
        shader.CreateInput(name, Sdf.ValueTypeNames.Color3f).Set(color)
    diffuse_weight = liquid_diffuse_weight(concentration_m)
    shader.CreateInput('diffuse_reflection_weight', Sdf.ValueTypeNames.Float).Set(diffuse_weight)
    shader.CreateInput('specular_transmission_weight', Sdf.ValueTypeNames.Float).Set(1.-diffuse_weight)


def liquid_mesh_data(geometry, segments=128):
    """Watertight mesh, smooth side normals, and a volume-neutral meniscus.

    A small wall rise improves the visible liquid edge. Its extra volume is
    removed from the central surface. Polygon area is compensated too, so the
    signed mesh volume equals pi*r^2*ledger_depth rather than a fake fill level.
    Face-varying normals preserve the sharp bottom and smooth cylinder walls.
    """
    if segments < 32:
        raise ValueError('At least 32 liquid segments are required')
    ledger_radius = float(geometry['radius'])
    depth = float(geometry['liquid_depth'])
    if ledger_radius <= 0 or depth < 0 or not math.isfinite(ledger_radius+depth):
        raise ValueError('Invalid liquid dimensions')
    # A coincident glass inner wall and liquid boundary makes the RTX ray choose
    # the glass hit and omit liquid absorption. Keep a microscopic optical gap;
    # compensate its volume exactly in the height, rather than exaggerating fill.
    radius = ledger_radius-min(.00002, ledger_radius*.004)
    x, y = geometry['xy']
    bottom = float(geometry['floor_z'])+min(.00001, depth*.01)
    n = segments
    polygon_area = .5*n*radius**2*math.sin(math.tau/n)
    height = math.pi*ledger_radius**2*depth / polygon_area
    q = .90
    rise = min(.00018, height*.12, radius*.025)
    mean_rise = (2/3-q+q**3/3)/(1-q)
    central_z = bottom+height-rise*mean_rise
    outer_z = central_z+rise
    points = []
    for r, z in ((radius, bottom), (radius, outer_z), (q*radius, central_z)):
        points.extend((x+r*math.cos(i*math.tau/n),
                       y+r*math.sin(i*math.tau/n), z) for i in range(n))
    top_center, bottom_center = 3*n, 3*n+1
    points.extend(((x,y,central_z), (x,y,bottom)))
    counts, indices, normals = [], [], []

    def radial(i, slope=0.):
        a = i*math.tau/n
        if slope:
            length = math.sqrt(1+slope*slope)
            return (-slope*math.cos(a)/length, -slope*math.sin(a)/length, 1/length)
        return (math.cos(a), math.sin(a), 0.)

    def face(ids, ns):
        counts.append(len(ids))
        indices.extend(ids)
        normals.extend(ns)

    slope = rise/(radius*(1-q))
    for i in range(n):
        j = (i+1) % n
        face((i,j,n+j,n+i), (radial(i),radial(j),radial(j),radial(i)))
        face((n+i,n+j,2*n+j,2*n+i),
             (radial(i,slope),radial(j,slope),(0.,0.,1.),(0.,0.,1.)))
        face((top_center,2*n+i,2*n+j), ((0.,0.,1.),)*3)
        face((bottom_center,j,i), ((0.,0.,-1.),)*3)
    return {'points': points, 'counts': counts, 'indices': indices, 'normals': normals}


def create_liquid_visual(stage, path, material):
    from pxr import UsdGeom, UsdShade
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDoubleSidedAttr(False)
    mesh.CreateOrientationAttr(UsdGeom.Tokens.rightHanded)
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return mesh


def stirred_liquid_mesh_data(geometry, phase_s, strength=1.0, segments=128, rings=12):
    """Illustrative rotating free surface; exact mesh volume stays ledger-driven.

    A shallow central depression and moving three-lobed surface produce visible
    optical motion without changing tint, inventory, or the mixing efficiency.
    This is authored animation, not a fluid or magnetic-coupling calculation.
    """
    radius = geometry['radius'] - min(.00002, geometry['radius']*.004)
    x, y = geometry['xy']
    depth = geometry['liquid_depth']
    if depth <= 0 or not 0 <= strength <= 1 or rings < 2 or segments < 32:
        raise ValueError('Invalid stirring surface geometry')
    bottom = geometry['floor_z'] + min(.00001, depth*.01)
    area = .5*segments*radius**2*math.sin(math.tau/segments)
    height = math.pi*geometry['radius']**2*depth/area
    amplitude = min(.00065, height*.12)*strength
    wave = min(.0012, height*.15)*strength
    points = [(x+radius*math.cos(i*math.tau/segments),
               y+radius*math.sin(i*math.tau/segments), bottom)
              for i in range(segments)]
    top_normals = {}
    for ring in range(1, rings+1):
        u = ring/rings
        for i in range(segments):
            a = i*math.tau/segments
            phase = 3*a + 7*u - math.tau*phase_s*.9
            z = bottom+height + amplitude*(2*u*u-1) + wave*u**3*(1-u*u)*math.sin(phase)
            dr = (4*amplitude*u + wave*((3*u*u-5*u**4)*math.sin(phase)
                   + 7*u**3*(1-u*u)*math.cos(phase)))/radius
            da = 3*wave*u**3*(1-u*u)*math.cos(phase)
            nx = -dr*math.cos(a)+da*math.sin(a)/(u*radius)
            ny = -dr*math.sin(a)-da*math.cos(a)/(u*radius)
            length = math.sqrt(nx*nx+ny*ny+1)
            top_normals[len(points)] = (nx/length,ny/length,1/length)
            points.append((x+u*radius*math.cos(a),y+u*radius*math.sin(a),z))
    top_center, bottom_center = len(points), len(points)+1
    points.extend(((x,y,bottom+height-amplitude),(x,y,bottom)))
    top_normals[top_center] = (0.,0.,1.)
    top_faces=[]
    for i in range(segments):
        j=(i+1)%segments
        top_faces.append((top_center,segments+i,segments+j))
        for ring in range(1,rings):
            inner,outer=ring*segments,(ring+1)*segments
            top_faces.append((inner+i,outer+i,outer+j,inner+j))
    # Integrate each planar top triangle; one uniform correction removes the
    # discretisation error and the vortex's mean displacement exactly.
    integrals=[]
    for ids in top_faces:
        for k in range(1,len(ids)-1):
            p0,p1,p2=(points[ids[q]] for q in (0,k,k+1))
            triangle_area=((p1[0]-p0[0])*(p2[1]-p0[1])-(p1[1]-p0[1])*(p2[0]-p0[0]))*.5
            integrals.append(triangle_area*((p0[2]+p1[2]+p2[2])/3-bottom))
    correction=(math.pi*geometry['radius']**2*depth-math.fsum(integrals))/area
    for i in list(top_normals):
        px,py,pz=points[i]
        points[i]=(px,py,pz+correction)
    counts,indices,normals=[],[],[]
    def face(ids,ns):
        counts.append(len(ids)); indices.extend(ids); normals.extend(ns)
    for ids in top_faces:
        face(ids,[top_normals[i] for i in ids])
    outer=rings*segments
    for i in range(segments):
        j=(i+1)%segments
        radial=lambda k:(math.cos(k*math.tau/segments),math.sin(k*math.tau/segments),0.)
        face((i,j,outer+j,outer+i),[radial(k) for k in (i,j,j,i)])
        face((bottom_center,j,i),[(0.,0.,-1.)]*3)
    return {'points':points,'counts':counts,'indices':indices,'normals':normals}


def update_liquid_visual(mesh, geometry, visible=True, mixing_phase_s=None, mixing_strength=1.0):
    from pxr import Gf, UsdGeom
    if not visible or geometry['liquid_depth'] <= 0:
        mesh.GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
        return
    data = (stirred_liquid_mesh_data(geometry, mixing_phase_s, mixing_strength)
            if mixing_phase_s is not None else liquid_mesh_data(geometry))
    mesh.CreatePointsAttr([Gf.Vec3f(*p) for p in data['points']])
    mesh.CreateFaceVertexCountsAttr(data['counts'])
    mesh.CreateFaceVertexIndicesAttr(data['indices'])
    mesh.CreateNormalsAttr([Gf.Vec3f(*v) for v in data['normals']])
    mesh.GetVisibilityAttr().Set(UsdGeom.Tokens.inherited)


def configure_liquid_lighting(stage, root_path):
    """Give transparent surfaces real area-light reflections, without emission."""
    import carb
    from pxr import Gf, UsdLux
    settings = carb.settings.get_settings()
    # Keys provided by this node's omni.rtx.settings.core extension. Glass front
    # + liquid + glass back require several transmission hits along one ray.
    settings.set('/rtx/translucency/enabled', True)
    settings.set('/rtx/translucency/maxRefractionBounces', 16)
    settings.set('/rtx/rtpt/maxSpecularAndTransmissionBounces', 16)
    settings.set('/rtx/pathtracing/maxSpecularAndTransmissionBounces', 16)
    for name, eye, target, width, height, intensity in (
        ('LiquidKey', (.18,-.40,.65), (.52,.02,.04), .45,.35,850.),
        ('LiquidStrip', (.83,.24,.42), (.52,.02,.04), .12,.38,650.),
    ):
        light = UsdLux.RectLight.Define(stage, str(root_path)+'/'+name)
        light.CreateWidthAttr(width)
        light.CreateHeightAttr(height)
        light.CreateIntensityAttr(intensity)
        light.AddTransformOp().Set(Gf.Matrix4d().SetLookAt(
            Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(0,0,1)).GetInverse())
    return {'model': 'closed_MDL_dielectric_with_area_lights',
            'liquid_ior': 1.333, 'alpha_opacity': 1., 'emission': 0.,
            'transmission_bounces': 16, 'renderer': settings.get('/rtx/rendermode'),
            'beer_lambert_reference_distance_m': CELL_SOLUTION_REFERENCE_DISTANCE_M,
            'clear_liquid_diffuse_weight': CLEAR_LIQUID_DIFFUSE_WEIGHT,
            'coloured_solution_diffuse_weight': COLOURED_SOLUTION_DIFFUSE_WEIGHT,
            'volume_source': 'same_authoritative_ledger',
            'colour': 'qualitative_not_absorbance_calibrated'}

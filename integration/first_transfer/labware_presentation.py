"""Asset-bounded vessel geometry and render-only contours, separate from chemistry."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class VesselGeometry:
    lower: tuple
    upper: tuple
    scale: float = 1.4
    floor_thickness_m: float = .003
    nozzle_offset_m: float = .172

    def at(self, position, volume_ul):
        if volume_ul < 0 or not math.isfinite(volume_ul):
            raise ValueError('Invalid vessel volume')
        lo = [position[i] + self.lower[i] * self.scale for i in range(3)]
        hi = [position[i] + self.upper[i] * self.scale for i in range(3)]
        # Inscribed circle conservatively excludes the spout and wall thickness.
        radius = min(hi[0] - lo[0], hi[1] - lo[1]) * .40
        floor, rim = lo[2] + self.floor_thickness_m, hi[2]
        depth = volume_ul * 1e-9 / (math.pi * radius * radius)
        if floor + depth >= rim - .004:
            raise ValueError('Ledger volume exceeds illustrative vessel geometry')
        xy = tuple((lo[i] + hi[i]) / 2 for i in range(2))
        return {'xy': xy, 'floor_z': floor, 'rim_z': rim, 'radius': radius,
                'liquid_depth': depth, 'surface_z': floor + depth,
                'aspirate_ee_z': floor + max(.0006, depth * .5) + self.nozzle_offset_m,
                # 22 mm insertion leaves clearance for the verified 15 mm
                # position tolerance; geometry must still pass runtime checks.
                'dispense_ee_z': rim - .022 + self.nozzle_offset_m}


def read_asset_geometry(path, scale):
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.Open(path)
    box = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
             [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]).ComputeUntransformedBound(
                 stage.GetDefaultPrim()).ComputeAlignedRange()
    return VesselGeometry(tuple(box.GetMin()), tuple(box.GetMax()), scale)


def optical_material(stage, path, color=(1., 1., 1.), ior=1.49, depth=.001, thin_walled=False):
    """RTX MDL transmission, not alpha/diffuse-painted glass."""
    from runtime_assets import kit_material_path
    from pxr import Gf, Sdf, UsdShade
    mdl = kit_material_path('OmniGlass.mdl')
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + '/Surface')
    shader.CreateImplementationSourceAttr().Set('sourceAsset')
    shader.SetSourceAsset(Sdf.AssetPath(str(mdl)), 'mdl')
    shader.SetSourceAssetSubIdentifier('OmniGlass', 'mdl')
    shader.CreateInput('glass_color', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
    shader.CreateInput('glass_ior', Sdf.ValueTypeNames.Float).Set(ior)
    shader.CreateInput('frosting_roughness', Sdf.ValueTypeNames.Float).Set(.015)
    shader.CreateInput('thin_walled', Sdf.ValueTypeNames.Bool).Set(thin_walled)
    shader.CreateInput('depth', Sdf.ValueTypeNames.Float).Set(depth)
    shader.CreateOutput('out', Sdf.ValueTypeNames.Token)
    material.CreateSurfaceOutput('mdl').ConnectToSource(shader.ConnectableAPI(), 'out')
    return material


def translucent_liquid_material(stage, path, color=(.30, .65, .96), opacity=.82,
                                emission=(.10, .24, .42), roughness=.18):
    """Use a visibly blue, lightly self-lit liquid volume separate from vessel glass."""
    from pxr import Gf, Sdf, UsdShade
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + '/Surface')
    shader.CreateIdAttr('UsdPreviewSurface')
    shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
    shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(roughness)
    shader.CreateInput('metallic', Sdf.ValueTypeNames.Float).Set(0.)
    shader.CreateInput('opacity', Sdf.ValueTypeNames.Float).Set(opacity)
    shader.CreateInput('specularColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(.78, .90, 1.0))
    shader.CreateInput('emissiveColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*emission))
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
    return material


def create_liquid_mesh(stage, path, segments=96):
    """Create separate liquid sidewall and top-surface meshes for an open vessel."""
    from pxr import Gf, UsdGeom
    if segments < 16:
        raise ValueError('Liquid mesh needs at least 16 radial segments')
    body = UsdGeom.Mesh.Define(stage, path + '/Body')
    surface = UsdGeom.Mesh.Define(stage, path + '/Surface')
    body.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    surface.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    body.CreateDoubleSidedAttr(True)
    surface.CreateDoubleSidedAttr(True)

    side_counts = [4] * segments + [3] * segments
    side_indices = []
    for i in range(segments):
        j = (i + 1) % segments
        side_indices.extend((i, j, segments + j, segments + i))
    bottom_center = 2 * segments
    for i in range(segments):
        j = (i + 1) % segments
        side_indices.extend((bottom_center, j, i))
    body.CreateFaceVertexCountsAttr().Set(side_counts)
    body.CreateFaceVertexIndicesAttr().Set(side_indices)

    surface_counts = [3] * segments
    surface_indices = []
    for i in range(segments):
        j = (i + 1) % segments
        surface_indices.extend((0, i + 1, j + 1))
    surface.CreateFaceVertexCountsAttr().Set(surface_counts)
    surface.CreateFaceVertexIndicesAttr().Set(surface_indices)
    body.CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    surface.CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    return {'body': body, 'surface': surface, 'segments': segments}


def update_liquid_mesh(visual, geometry, visible):
    """Update real-scale mesh dimensions and the distinct liquid surface plane."""
    from pxr import Gf, UsdGeom
    n = visual['segments']
    x, y = geometry['xy']
    radius = geometry['radius']
    bottom = geometry['floor_z']
    top = geometry['surface_z']
    bottom_points, top_points = [], []
    surface_points = [Gf.Vec3f(x, y, top)]
    for i in range(n):
        angle = i * math.tau / n
        px, py = x + radius * math.cos(angle), y + radius * math.sin(angle)
        bottom_points.append(Gf.Vec3f(px, py, bottom))
        top_points.append(Gf.Vec3f(px, py, top))
        surface_points.append(Gf.Vec3f(px, py, top))
    body_points = bottom_points + top_points + [Gf.Vec3f(x, y, bottom)]
    visual['body'].GetPointsAttr().Set(body_points)
    visual['surface'].GetPointsAttr().Set(surface_points)
    token = UsdGeom.Tokens.inherited if visible and geometry['liquid_depth'] > 0 else UsdGeom.Tokens.invisible
    visual['body'].GetVisibilityAttr().Set(token)
    visual['surface'].GetVisibilityAttr().Set(token)


def copper_solution_blue(concentration_m):
    """Qualitative pale-to-deeper blue cue; never changes chemistry state."""
    if not math.isfinite(concentration_m) or concentration_m < 0:
        raise ValueError('Invalid copper concentration for liquid display')
    fraction = max(0., min(1., math.sqrt(concentration_m / .1)))
    dilute = (.34, .67, .97)
    concentrated = (.14, .43, .88)
    return tuple(a + (b - a) * fraction for a, b in zip(dilute, concentrated))


def decorate_vessel(stage, parent, asset_prim_path):
    """Retain existing mesh/collision; override appearance in this run only."""
    from pxr import UsdShade
    material = optical_material(stage, parent + '/ClearGlass',
                                color=(1., 1., 1.), depth=.02)
    prim = stage.GetPrimAtPath(asset_prim_path)
    if not prim:
        raise ValueError('Cannot find vessel prim: ' + asset_prim_path)
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, bindingStrength=UsdShade.Tokens.strongerThanDescendants)
    # Deliberately no added outline/marks: v4 curves did not match the asset.
    return None


def update_contours(curves, geometry):
    if curves is None:
        return
    from pxr import Gf
    x, y = geometry['xy']
    floor, rim = geometry['floor_z'], geometry['rim_z']
    radius = geometry['radius'] * 1.12
    points, counts = [], []
    for z in (floor, rim):
        ring = [Gf.Vec3f(x + radius * math.cos(a*math.tau/64),
                        y + radius * math.sin(a*math.tau/64), z) for a in range(65)]
        points.extend(ring)
        counts.append(65)
    # Side contours and unnumbered marks are readability aids, not calibrated graduations.
    for angle in (0, math.pi):
        px, py = x + radius * math.cos(angle), y + radius * math.sin(angle)
        points.extend([Gf.Vec3f(px, py, floor), Gf.Vec3f(px, py, rim)])
        counts.append(2)
    for i in range(1, 5):
        z = floor + (rim-floor) * i/5
        points.extend([Gf.Vec3f(x + radius, y-.004, z), Gf.Vec3f(x+radius, y+.004, z)])
        counts.append(2)
    curves.GetPointsAttr().Set(points)
    curves.GetCurveVertexCountsAttr().Set(counts)

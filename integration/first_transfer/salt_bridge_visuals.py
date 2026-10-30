"""Microcell salt-bridge adapter, metres. No transport, CFD or force model.

The cap reuses CHEM-016 geometry; the 1.2 mm OD capillary, prefilled gel and
grounded fixtures are project geometry. Both legs use the existing reserved
cell axes. The gel is an independent preloaded component, not extra cell fluid.
"""
import math
from pathlib import Path

from electrode_visuals import _copy_fitted_component, _cube, CELL_AXIS_X_OFFSET_M
from microcell_labware import BRIDGE_TIP_CLEARANCE_M, BRIDGE_OUTER_RADIUS_M
from bench_layout import BRIDGE_RACK_CENTER_M, FIXTURE_COLOR, FORK_COLOR

BRIDGE_RADIUS_M = BRIDGE_OUTER_RADIUS_M
BRIDGE_INNER_RADIUS_M = .0004
BRIDGE_TIP_OFFSET_M = -.075
BRIDGE_GRIP_SIZE_M = (.010, .010, .012)


def bridge_target(cells):
    left, right = cells['left_half_cell'], cells['right_half_cell']
    if abs(left.xy[1] - right.xy[1]) > 1e-8:
        raise ValueError('盐桥适配要求两池轴线 Y 坐标相同')
    floor = left.support_z + left.floor_thickness
    return ((left.xy[0] + right.xy[0]) / 2 + .0025, left.xy[1],
            floor + BRIDGE_TIP_CLEARANCE_M - BRIDGE_TIP_OFFSET_M)


def bridge_geometry(center, cells, volumes):
    desired = bridge_target(cells)
    span = cells['right_half_cell'].xy[0] - cells['left_half_cell'].xy[0]
    bottom = center[2] + BRIDGE_TIP_OFFSET_M
    ends, immersed, checks = {}, {}, {}
    for side, dx in (('left', -span / 2), ('right', span / 2)):
        cell_id = f'{side}_half_cell'
        cell = cells[cell_id]
        end = (center[0] + dx, center[1], bottom)
        liquid = cell.at(None, volumes[cell_id], electrode_inserted=True, bridge_inserted=True)
        ends[side] = list(end)
        immersed[side] = liquid['surface_z'] - bottom
        checks[f'{side}_end_inside_cell'] = math.dist(end[:2], cell.xy) + BRIDGE_RADIUS_M < cell.inner_radius
        checks[f'{side}_end_immersed'] = immersed[side] >= .001
    checks['electrodes_clear'] = all(
        abs(ends[side][0] - (cells[f'{side}_half_cell'].xy[0] + CELL_AXIS_X_OFFSET_M))
        - .004 - BRIDGE_RADIUS_M >= .0002 for side in ends)
    checks['stir_bars_clear'] = all(
        bottom - (cell.support_z + cell.floor_thickness) >= .0029 for cell in cells.values())
    checks['holder_contact'] = max(abs(center[i] - desired[i]) for i in range(3)) <= .001
    return {'ends_m': ends, 'immersed_depths_m': immersed, 'checks': checks,
            'grip_bottom_z_m': center[2] - BRIDGE_GRIP_SIZE_M[2] / 2,
            'holder_contact_z_m': desired[2] - BRIDGE_GRIP_SIZE_M[2] / 2}


def _path(span):
    half, radius, top = span / 2, .004, -.016
    points = [(-half, 0., BRIDGE_TIP_OFFSET_M), (-half, 0., top - radius)]
    points += [(-half + radius + radius * math.cos(math.pi - i * math.pi / 24),
                0., top - radius + radius * math.sin(math.pi - i * math.pi / 24))
               for i in range(1, 13)]
    points.append((half - radius, 0., top))
    points += [(half - radius + radius * math.cos(math.pi / 2 - i * math.pi / 24),
                0., top - radius + radius * math.sin(math.pi / 2 - i * math.pi / 24))
               for i in range(1, 13)]
    points.append((half, 0., BRIDGE_TIP_OFFSET_M))
    return points


def _sweep(stage, path, centers, radii, material):
    from pxr import Gf, UsdGeom, UsdShade
    points, normals, faces, counts = [], [], [], []
    segments, n = 32, len(centers)
    for ring, radius in enumerate(radii):
        for i, center in enumerate(centers):
            before, after = centers[max(0, i - 1)], centers[min(n - 1, i + 1)]
            tx, tz = after[0] - before[0], after[2] - before[2]
            length = math.hypot(tx, tz)
            nx, nz = tz / length, -tx / length
            for j in range(segments):
                a = j * math.tau / segments
                normal = (nx * math.cos(a), math.sin(a), nz * math.cos(a))
                points.append(Gf.Vec3f(*(center[k] + radius * normal[k] for k in range(3))))
                normals.append(Gf.Vec3f(*(v * (-1 if ring else 1) for v in normal)))
        offset = ring * n * segments
        for i in range(n - 1):
            for j in range(segments):
                a, b = offset + i * segments + j, offset + i * segments + (j + 1) % segments
                faces.extend((a, b, b + segments, a + segments) if not ring
                             else (a + segments, b + segments, b, a))
                counts.append(4)
    if len(radii) == 2:
        for i in (0, n - 1):
            for j in range(segments):
                a, b = i * segments + j, i * segments + (j + 1) % segments
                faces.extend((a, a + n * segments, b + n * segments, b))
                counts.append(4)
    else:
        for i in (0, n - 1):
            faces.extend(i * segments + j for j in (range(segments - 1, -1, -1) if i == 0 else range(segments)))
            counts.append(segments)
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(points)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateFaceVertexIndicesAttr(faces)
    mesh.CreateSubdivisionSchemeAttr('none')
    mesh.CreateDoubleSidedAttr(True)
    # USD vertex normals keep the tiny transparent tube optically smooth.
    mesh.CreateNormalsAttr(normals)
    mesh.SetNormalsInterpolation('vertex')
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)


def create_salt_bridge(stage, root_path, asset_usd, cells):
    from pxr import Gf, Usd, UsdGeom
    from labware_presentation import optical_material
    target = bridge_target(cells)
    span = cells['right_half_cell'].xy[0] - cells['left_half_cell'].xy[0]
    root = UsdGeom.Xform.Define(stage, root_path + '/SaltBridge')
    translate = root.AddTranslateOp()
    translate.Set(Gf.Vec3d(*BRIDGE_RACK_CENTER_M))
    parent = str(root.GetPath())
    glass = optical_material(stage, root_path + '/BridgeGlass', color=(1., 1., 1.), depth=.002, thin_walled=False)
    gel = optical_material(stage, root_path + '/BridgeGel', color=(.96, .98, 1.), depth=.03, thin_walled=False)
    centers = _path(span)
    _sweep(stage, parent + '/Capillary', centers, (BRIDGE_RADIUS_M, BRIDGE_INNER_RADIUS_M), glass)
    _sweep(stage, parent + '/PrefilledGel', centers, (BRIDGE_INNER_RADIUS_M - .000002,), gel)
    source = Usd.Stage.Open(str(Path(asset_usd).resolve()))
    cap = _copy_fitted_component(stage, source, parent, 'GripCap', 'electrode_cap',
                                 BRIDGE_GRIP_SIZE_M, (.78, .84, .86))
    _cube(stage, parent + '/CapConnector', (0., 0., -.011), (.004, .004, .010), (.78, .84, .86))
    for side, dx in (('Left', -span / 2), ('Right', span / 2)):
        plug = UsdGeom.Cylinder.Define(stage, parent + f'/{side}PorousEnd')
        plug.CreateRadiusAttr(BRIDGE_RADIUS_M)
        plug.CreateHeightAttr(.001)
        plug.AddTranslateOp().Set(Gf.Vec3d(dx, 0., BRIDGE_TIP_OFFSET_M + .0005))
        plug.CreateDisplayColorAttr([Gf.Vec3f(.83, .88, .88)])
    # Both supports start from a real support plane, not a floating coordinate.
    holder_z = target[2] - BRIDGE_GRIP_SIZE_M[2] / 2
    stand = root_path + '/SaltBridgeRack'
    rx, ry, rz = BRIDGE_RACK_CENTER_M
    rack_top = rz - BRIDGE_GRIP_SIZE_M[2] / 2
    _cube(stage, stand + '/Post', (rx, ry + .013, (.008 + rack_top) / 2),
          (.004, .004, rack_top - .008), FIXTURE_COLOR)
    for sign in (-1, 1):
        _cube(stage, stand + ('/ForkLeft' if sign < 0 else '/ForkRight'), (rx + sign * .004, ry + .002, rack_top - .001),
              (.0018, .027, .002), FORK_COLOR)
    holder = root_path + '/SaltBridgeHolder'
    base_z = cells['left_half_cell'].support_z
    y = target[1] + .011
    for side, x in (('Left', target[0] - .035), ('Right', target[0] + .031)):
        _cube(stage, holder + f'/{side}Foot', (x, y, base_z + .001),
              (.006, .006, .002), (.48, .59, .65))
        _cube(stage, holder + f'/{side}Post', (x, y, (base_z + holder_z) / 2),
              (.003, .003, holder_z - base_z), (.48, .59, .65))
    _cube(stage, holder + '/Crossbar', (target[0] - .002, y, holder_z - .0015),
          (.066, .003, .003), (.48, .59, .65))
    for sign in (-1, 1):
        _cube(stage, holder + ('/ForkLeft' if sign < 0 else '/ForkRight'), (target[0] + sign * .004, y - .007, holder_z - .001),
              (.0018, .019, .002), (.76, .85, .88))
    return {'translate_op': translate, 'rack_center_m': list(BRIDGE_RACK_CENTER_M),
            'target_center_m': list(target), 'source_cap': cap,
            'outer_diameter_m': BRIDGE_RADIUS_M * 2, 'inner_diameter_m': BRIDGE_INNER_RADIUS_M * 2,
            'leg_spacing_m': span, 'rack_contact_z_m': 0., 'holder_base_contact_z_m': base_z,
            'rack_uses_shared_tool_bank': True,
            'holder_top_z_m': holder_z, 'source_asset_id': 'CHEM-016_cap_only',
            'capillary_and_fixtures': 'project_designed_microcell_adapter',
            'gel_preloaded': True, 'ion_transport_or_contact_physics_validated': False}


def set_bridge_center(bridge, position):
    from pxr import Gf
    bridge['translate_op'].Set(Gf.Vec3d(*position))

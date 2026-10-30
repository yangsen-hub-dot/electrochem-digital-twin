"""CHEM-016-derived electrode geometry and grounded microcell fixtures.

The imported IKA package supplies the cap and graphite-plate mesh shapes. The
plate is resized and recolored as a copper teaching proxy; no electrode
chemistry, force grasp, collision validation, or electrochemical connection is
inherited from the source asset.
"""

from __future__ import annotations

from pathlib import Path

from pipette_visuals import pipette_stand_contact_z
from bench_layout import (PIPETTE_STAND_XY, PIPETTE_STAND_XY_BY_ID, PIPETTE_DOCK_EE_Z_M, COPPER_RACK_XY,
                          COPPER_RACK_CAP_CENTER_Z_M, FIXTURE_COLOR, FORK_COLOR,
                          create_tool_bank)


CELL_AXIS_X_OFFSET_M = -0.0025
ELECTRODE_PLATE_M = (0.0004, 0.003, 0.039)  # thickness X, width Y, length Z
ELECTRODE_CAP_M = (0.008, 0.008, 0.012)
ELECTRODE_PLATE_CENTER_OFFSET_Z_M = -(
    ELECTRODE_CAP_M[2] / 2 + ELECTRODE_PLATE_M[2] / 2)
ELECTRODE_INSERTION_M = 0.042
# Preserve the cap/support endpoint while increasing clearance above the
# microcell floor: the virtual working plate is shortened by the same 3 mm.
# The placement checker still enforces its independent 2.9 mm minimum.
TIP_CLEARANCE_M = 0.006
COPPER_RACK_SLOT_X_OFFSET_M = 0.012


def electrode_stack_geometry(cap_center_z_m):
    """Return the aligned cap/plate Z spans for the adapted electrode proxy."""
    cap_bottom = float(cap_center_z_m) - ELECTRODE_CAP_M[2] / 2
    plate_center = float(cap_center_z_m) + ELECTRODE_PLATE_CENTER_OFFSET_Z_M
    plate_bottom = plate_center - ELECTRODE_PLATE_M[2] / 2
    plate_top = plate_center + ELECTRODE_PLATE_M[2] / 2
    return {'cap_bottom_z_m': cap_bottom, 'plate_bottom_z_m': plate_bottom,
            'plate_top_z_m': plate_top}


def _component_mesh(source_stage, token):
    from pxr import UsdGeom

    excludes = {"electrode_cap": ("__cap_grip", "__working_electrode"),
                "working_electrode": ("__cap_grip",),
                "cap_grip": ("__working_electrode",)}.get(token, ())
    matches = [prim for prim in source_stage.Traverse()
               if prim.IsA(UsdGeom.Mesh) and token in str(prim.GetPath()).lower()
               and not any(part in str(prim.GetPath()).lower() for part in excludes)]
    if not matches:
        raise ValueError(f"CHEM-016 USD 中找不到复用网格 {token}")
    # GLB converters may split a component into several material meshes.
    return matches


def _canonical_asset_axes(source_stage):
    """Map converter output axes to CHEM-016's declared X/Y/Z proportions."""
    from pxr import Usd, UsdGeom

    root = source_stage.GetDefaultPrim()
    if not root:
        roots = [p for p in source_stage.GetPseudoRoot().GetChildren() if p.IsA(UsdGeom.Xform)]
        if not roots:
            raise ValueError("CHEM-016 USD 没有可读取的场景根")
        root = roots[0]
    bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                               [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    size = bounds.ComputeWorldBound(root).ComputeAlignedRange().GetSize()
    axes = sorted(range(3), key=lambda i: float(size[i]))
    if min(float(size[i]) for i in range(3)) <= 0:
        raise ValueError("CHEM-016 USD 场景尺寸无效")
    # Catalog size is X=small, Z=middle, Y=long. Converter may swap Y/Z.
    return axes[0], axes[2], axes[1], [float(v) for v in size]


def _copy_fitted_component(stage, source_stage, parent, name, token, dimensions, color,
                           local_translation=(0.0, 0.0, 0.0)):
    from pxr import Gf, Usd, UsdGeom

    axis_x, axis_y, axis_z, _ = _canonical_asset_axes(source_stage)
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    source_points, counts, indices = [], None, None
    for prim in _component_mesh(source_stage, token):
        mesh = UsdGeom.Mesh(prim)
        transform = cache.GetLocalToWorldTransform(prim)
        for point in mesh.GetPointsAttr().Get():
            p = transform.Transform(Gf.Vec3d(*point))
            source_points.append((float(p[axis_x]), float(p[axis_y]), float(p[axis_z])))
        local_counts = list(mesh.GetFaceVertexCountsAttr().Get())
        local_indices = list(mesh.GetFaceVertexIndicesAttr().Get())
        if counts is None:
            counts, indices = [], []
        offset = len(source_points) - len(mesh.GetPointsAttr().Get())
        counts.extend(local_counts)
        indices.extend(offset + int(i) for i in local_indices)
    if not source_points:
        raise ValueError(f"CHEM-016 网格 {token} 没有顶点")
    lower = [min(p[i] for p in source_points) for i in range(3)]
    upper = [max(p[i] for p in source_points) for i in range(3)]
    span = [upper[i] - lower[i] for i in range(3)]
    target = tuple(float(v) for v in dimensions)
    if min(span) <= 0 or min(target) <= 0:
        raise ValueError(f"CHEM-016 网格 {token} 包围尺寸异常")
    fitted = [Gf.Vec3f(*(
        (p[i] - lower[i]) / span[i] * target[i] - target[i] / 2
        for i in range(3))) for p in source_points]
    mesh = UsdGeom.Mesh.Define(stage, parent + "/" + name)
    mesh.CreatePointsAttr(fitted)
    mesh.CreateFaceVertexCountsAttr(counts)
    mesh.CreateFaceVertexIndicesAttr(indices)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDoubleSidedAttr(True)
    mesh.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    if any(abs(float(value)) > 0 for value in local_translation):
        UsdGeom.Xformable(mesh).AddTranslateOp().Set(Gf.Vec3d(*local_translation))
    return {"prim_path": str(mesh.GetPath()), "source_component": token,
            "source_meshes": len(_component_mesh(source_stage, token)),
            "source_bounds_m_after_axis_mapping": span,
            "adapted_dimensions_m": list(target),
            "local_translation_m": [float(value) for value in local_translation]}


def _cube(stage, path, center, size, color):
    from pxr import Gf, UsdGeom

    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    cube.AddTranslateOp().Set(Gf.Vec3d(*center))
    cube.AddScaleOp().Set(Gf.Vec3f(*size))
    cube.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    return cube


def create_electrode_supports(stage, root_path, cells, holder_top_z_m):
    """Build table-contacting fixtures for two rack electrodes and both cells."""
    from pxr import Gf, UsdGeom

    if set(cells) != {"left", "right"}:
        raise ValueError("电极承托夹具需要左右两个小池")
    supports = {}
    for side, cell in cells.items():
        x, y = cell.xy
        axis_x = x + CELL_AXIS_X_OFFSET_M
        plate_bottom = cell.support_z + cell.floor_thickness + TIP_CLEARANCE_M
        cap_bottom = plate_bottom + ELECTRODE_PLATE_M[2]
        rim = cell.support_z + cell.floor_thickness + cell.cavity_height
        if abs((rim + .010) - cap_bottom) > 1e-7:
            raise ValueError(f"{side}池铜电极长度与池底/池口接口不匹配")
        if abs(cell.support_z - holder_top_z_m) > 1e-7:
            raise ValueError(f"{side}池承托面必须与公共适配座顶面齐平")
        supports[side] = {"xy": (x, y), "axis_x": axis_x,
                          "plate_bottom": plate_bottom, "cap_bottom": cap_bottom}
    if abs(supports["left"]["cap_bottom"] - supports["right"]["cap_bottom"]) > 1e-7:
        raise ValueError("双池电极夹具要求两池共用同一承托高度")
    cap_bottom = supports["left"]["cap_bottom"]

    rack_x, rack_y = COPPER_RACK_XY
    rack_base = root_path + "/CopperElectrodeRack"
    tool_bank = create_tool_bank(stage, root_path)
    top = COPPER_RACK_CAP_CENTER_Z_M - ELECTRODE_CAP_M[2] / 2
    post_height = max(.004, top - .008)
    _cube(stage, rack_base + '/SharedPost', (rack_x, rack_y + .012, .008 + post_height / 2),
          (.004, .004, post_height), FIXTURE_COLOR)
    _cube(stage, rack_base + '/SharedCrossbar', (rack_x, rack_y + .012, top - .002),
          (.034, .004, .004), FIXTURE_COLOR)
    # Two visible support stations use the same CHEM-016 geometry, one per pool.
    for side, sign in (("left", -1), ("right", 1)):
        slot_x = rack_x + sign * COPPER_RACK_SLOT_X_OFFSET_M
        for finger, dx in (("L", -.0032), ("R", .0032)):
            _cube(stage, rack_base + f"/{side.title()}Fork{finger}",
                  (slot_x + dx, rack_y - .002, top - .001),
                  (.0014, .028, .002), FORK_COLOR)

    stand_top = pipette_stand_contact_z(PIPETTE_DOCK_EE_Z_M)
    stands = {}
    for pipette_id, (stand_x, stand_y) in PIPETTE_STAND_XY_BY_ID.items():
        short_id = 'P200' if pipette_id.endswith('_p200') else 'P1000'
        stand_base = root_path + f"/PipetteStand_{short_id}"
        post_h = stand_top - .008
        _cube(stage, stand_base + "/Post", (stand_x, stand_y + .010, .008 + post_h / 2),
              (.004, .004, post_h), FIXTURE_COLOR)
        for side, dx in (("L", -.012), ("R", .012)):
            _cube(stage, stand_base + f"/Fork{side}", (stand_x + dx, stand_y, stand_top - .001),
                  (.003, .020, .002), FORK_COLOR)
        stands[pipette_id] = {"xy_m": [stand_x, stand_y], "desktop_contact_z_m": 0.0,
                              "collar_contact_z_m": stand_top,
                              "dock_ee_z_m": PIPETTE_DOCK_EE_Z_M,
                              "asset": "project-designed pipette stand proxy"}

    cradle = root_path + "/DualCellElectrodeSupport"
    support_y = min(cell.xy[1] for cell in cells.values()) - .016
    outer_left = min(cell.xy[0] for cell in cells.values()) - .028
    outer_right = max(cell.xy[0] for cell in cells.values()) + .028
    post_top = cap_bottom - .003
    post_height = post_top - holder_top_z_m
    if post_height <= 0:
        raise ValueError("电极承托柱高度无效")
    for side, x in (("L", outer_left), ("R", outer_right)):
        _cube(stage, cradle + f"/{side}Foot", (x, support_y, holder_top_z_m + .001),
              (.010, .008, .002), (.38, .48, .54))
        _cube(stage, cradle + f"/{side}Post", (x, support_y, holder_top_z_m + post_height / 2),
              (.004, .004, post_height), (.46, .56, .62))
    _cube(stage, cradle + "/Crossbar",
          ((outer_left + outer_right) / 2, support_y, cap_bottom - .0015),
          (outer_right - outer_left, .004, .003), (.46, .56, .62))
    for side, record in supports.items():
        axis_x, y = record["axis_x"], record["xy"][1]
        # Two forward-projecting fingers reach under each CHEM-016 cap.
        for finger, dx in (("L", -.0032), ("R", .0032)):
            _cube(stage, cradle + f"/{side.title()}Fork{finger}",
                  (axis_x + dx, y - .0065, cap_bottom - .00075),
                  (.0018, .015, .0015), (.72, .82, .86))
    rack = {"xy_m": [rack_x, rack_y], "desktop_contact_z_m": 0.0,
            "cap_center_z_m": COPPER_RACK_CAP_CENTER_Z_M,
            "cap_bottom_supported_z_m": top, "mesh_asset_id": "CHEM-016",
            "slots": {side: [rack_x + sign * COPPER_RACK_SLOT_X_OFFSET_M,
                              rack_y, COPPER_RACK_CAP_CENTER_Z_M]
                      for side, sign in (("left", -1), ("right", 1))}}
    support = {"prim_path": cradle, "support_surface_z_m": cap_bottom,
               "adapter_top_z_m": holder_top_z_m,
               "electrode_supports": {
                   side: {"electrode_axis_xy_m": [record["axis_x"], record["xy"][1]],
                          "adapter_contact_z_m": holder_top_z_m,
                          "cap_bottom_contact_z_m": cap_bottom,
                          "cell_floor_z_m": cells[side].support_z + cells[side].floor_thickness,
                          "plate_bottom_z_m": record["plate_bottom"],
                          "plate_tip_clearance_m": TIP_CLEARANCE_M,
                          "salt_bridge_axis_xy_m": [cells[side].xy[0] + .0025, cells[side].xy[1]],
                          "salt_bridge_reserved": True,
                          "support_contact_verified_by_geometry": True}
                   for side, record in supports.items()}}
    return {"tool_bank": tool_bank, "electrode_rack": rack,
            "pipette_stand": stands['gilson_pipetman_p200'], "pipette_stands": stands,
            "left_electrode_cradle": support}


def create_electrode(stage, root_path, asset_usd: Path, cell, side="left"):
    from pxr import Gf, Usd, UsdGeom

    if side not in {"left", "right"}:
        raise ValueError("电极槽位必须为 left 或 right")
    source_stage = Usd.Stage.Open(str(Path(asset_usd).resolve()))
    if not source_stage:
        raise ValueError(f"无法打开 CHEM-016 转换文件：{asset_usd}")
    root = UsdGeom.Xform.Define(stage, root_path + f"/CopperElectrode_{side.title()}")
    center_z = COPPER_RACK_CAP_CENTER_Z_M
    slot_x = COPPER_RACK_XY[0] + (-1 if side == "left" else 1) * COPPER_RACK_SLOT_X_OFFSET_M
    root.AddTranslateOp().Set(Gf.Vec3d(slot_x, COPPER_RACK_XY[1], center_z))
    plate = _copy_fitted_component(stage, source_stage, str(root.GetPath()),
                                   "CopperPlateAdapted", "working_electrode",
                                   ELECTRODE_PLATE_M, (.72, .34, .13),
                                   local_translation=(0.0, 0.0,
                                                      ELECTRODE_PLATE_CENTER_OFFSET_Z_M))
    cap = _copy_fitted_component(stage, source_stage, str(root.GetPath()),
                                 "InsulatingCapAdapted", "electrode_cap",
                                 ELECTRODE_CAP_M, (.86, .88, .89))
    grip = _copy_fitted_component(stage, source_stage, str(root.GetPath()),
                                  "CapGripAdapted", "cap_grip", (.004, .004, .003), (.10, .12, .14))
    UsdGeom.Xformable(stage.GetPrimAtPath(grip["prim_path"])).AddTranslateOp().Set(Gf.Vec3d(0, 0, .0075))
    x, y = cell.xy
    final_center = (x + CELL_AXIS_X_OFFSET_M, y,
                    cell.support_z + cell.floor_thickness + TIP_CLEARANCE_M + ELECTRODE_PLATE_M[2] + ELECTRODE_CAP_M[2] / 2)
    stack = electrode_stack_geometry(final_center[2])
    return {"root_path": str(root.GetPath()), "root_translate_op": UsdGeom.Xformable(root).GetOrderedXformOps()[0],
            "source_usd": str(Path(asset_usd).resolve()),
            "asset_id": "CHEM-016", "components": {"plate": plate, "cap": cap, "grip": grip},
            "rack_center_m": [slot_x, COPPER_RACK_XY[1], center_z],
            f"{side}_cell_center_m": list(final_center), "target_side": f"{side}_half_cell",
            "electrode_stack_z_m": stack,
            "geometry_status": "adapted_teaching_proxy_from_graphite_source_mesh",
            "copper_appearance_only": True, "grasp_model": "kinematic_attachment_after_robot_pose_check"}


def set_electrode_center(electrode, position):
    from pxr import Gf

    electrode["root_translate_op"].Set(Gf.Vec3d(*position))


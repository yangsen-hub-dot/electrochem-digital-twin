"""Visual-only adapter for the selected ChemLab-Assets IKA mixer.

The imported IKA geometry is kept at its authored scale and rests on the
Matterix bench. A small support adapter places the existing 12 mm cell on the
plate. The custom 5 x 2 mm bar and surface motion are visual proxies only;
they do not model magnetic coupling, RPM, CFD, or mixing efficiency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def stirring_angle_degrees(elapsed_s):
    """Readable 10 s authored motion with acceleration/braking, NOT device RPM."""
    t = max(0., min(10., elapsed_s))
    ramp, brake = .8, 1.5
    if t < ramp:
        integrated = t*t/(2*ramp)
    elif t < 10.-brake:
        integrated = t-ramp/2
    else:
        u = t-(10.-brake)
        integrated = (10.-brake-ramp/2)+u-u*u/(2*brake)
    return 360.*.65*integrated


def mixing_camera_spec(geometries):
    """Elevated rear-oblique view through the open gaps, not through forks.

    Used with a longer lens and world +Y up to preserve left/right ordering.
    """
    values = list(geometries.values())
    focus = (sum(g['xy'][0] for g in values)/len(values),
             sum(g['xy'][1] for g in values)/len(values),
             min(g['floor_z'] for g in values)+.008)
    return focus, -.05, .20, 0.


def convert_glb_to_usd(app, source: Path, destination: Path) -> Path:
    """Convert GLB through Isaac Sim's official Kit Asset Converter API."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"缺少待转换的 ChemLab-Assets GLB：{source}")
    if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    import omni.kit.app
    extension_manager = omni.kit.app.get_app().get_extension_manager()
    extension_manager.set_extension_enabled_immediate("omni.kit.asset_converter", True)
    import omni.kit.asset_converter
    converter = omni.kit.asset_converter.get_instance()
    context = omni.kit.asset_converter.AssetConverterContext()
    context.ignore_materials = False
    context.ignore_animations = False
    context.ignore_camera = True
    context.ignore_light = True
    context.single_mesh = False

    async def convert():
        task = converter.create_converter_task(str(source), str(destination), None, context)
        success = await task.wait_until_finished()
        if not success:
            raise RuntimeError("ChemLab-Assets GLB 转 USD 失败：" + str(task.get_error_message()))
        return destination

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(convert(), loop=loop)
    while not future.done():
        app.update()
    return Path(future.result())


def add_ika_asset(stage, parent_path: str, usd_path: Path, *, plate_center):
    """Align the actual imported plate centre with the holder, feet on z=0."""
    from pxr import Gf, Usd, UsdGeom

    prim = stage.DefinePrim(parent_path + "/IKA_CHEM_008", "Xform")
    geometry = stage.DefinePrim(str(prim.GetPath()) + '/Geometry', 'Xform')
    if not geometry.GetReferences().AddReference(str(Path(usd_path).resolve())):
        raise RuntimeError('IKA USD 资产引用失败')
    raw = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render']).ComputeWorldBound(geometry).ComputeAlignedRange()
    size = raw.GetSize()
    if min(size) <= 0:
        raise RuntimeError('IKA 资产没有有效几何尺寸')
    # The source GLB explicitly uses Z-up; a converter may interpret the glTF
    # convention as Y-up. Correct that import convention, not the asset scale.
    orient = UsdGeom.Xformable(geometry)
    if size[1] < size[2]:
        orient.AddRotateXOp(opSuffix='sourceAxis').Set(90.0)
    scale = .16 / size[0]
    orient.AddScaleOp(opSuffix='importUnits').Set(Gf.Vec3f(scale, scale, scale))
    bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render']).ComputeWorldBound(geometry).ComputeAlignedRange()
    actual_size = bounds.GetSize()
    if any(abs(actual_size[i] - expected) > .001 for i, expected in enumerate((.16, .27, .0849))):
        raise RuntimeError(f'IKA 导入后的尺寸与资产清单不符: {list(actual_size)}')
    # The whole instrument footprint centre differs from the actual circular
    # plate centre. Locate that part rather than guessing a root offset.
    plate_parts = [p for p in Usd.PrimRange(geometry)
                   if p.IsA(UsdGeom.Mesh) and 'heating_plate' in str(p.GetPath()).lower()]
    if not plate_parts:
        raise RuntimeError('未找到 IKA heating_plate 圆盘网格')
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render'])
    plate_ranges = [cache.ComputeWorldBound(p).ComputeAlignedRange() for p in plate_parts]
    plate_lower = Gf.Vec3d(*(min(b.GetMin()[i] for b in plate_ranges) for i in range(3)))
    plate_upper = Gf.Vec3d(*(max(b.GetMax()[i] for b in plate_ranges) for i in range(3)))
    if any(abs((plate_upper-plate_lower)[i]-.135) > .001 for i in (0,1)):
        raise RuntimeError('识别的圆盘尺寸与 135 mm 盘面不符')
    plate_middle = (plate_lower + plate_upper) * .5
    x, y = plate_center
    translation = Gf.Vec3d(x-plate_middle[0], y-plate_middle[1], -bounds.GetMin()[2])
    xform = UsdGeom.Xformable(prim)
    xform.AddTranslateOp().Set(translation)
    return {
        "prim_path": str(prim.GetPath()),
        "source_asset": "ChemLab-Assets CHEM-008 IKA Plate RCT digital",
        "usd_path": str(Path(usd_path).resolve()),
        "root_pose_m": list(translation),
        "plate_center_xy_m": [x, y],
        "plate_mesh_paths": [str(p.GetPath()) for p in plate_parts],
        "alignment": "measured_plate_mesh_center_to_common_holder_center",
        "bench_contact_plane_z_m": 0.0,
        "hotplate_top_z_m": 0.0849,
        "geometry_scale": [1.0, 1.0, 1.0],
        "import_unit_correction": scale,
        "imported_dimensions_m": list(actual_size),
        "visual_asset_only": True,
        "heater_enabled": False,
        "built_in_mixer_behavior_invoked": False,
    }


def create_stirring_visuals(stage, parent_path: str, cell_id: str, center, floor_z: float):
    """Pre-place a visible 5 x 2 mm bar at a microcell floor, in metres."""
    from pxr import Gf, Sdf, UsdGeom, UsdShade

    x, y = center
    side = "left" if cell_id == "left_half_cell" else "right"
    bar_name = "MicroStirBar_5x2mm_" + side
    bar = UsdGeom.Capsule.Define(stage, parent_path + "/" + bar_name)
    bar.CreateAxisAttr("X")
    bar.CreateRadiusAttr(0.001)
    bar.CreateHeightAttr(0.003)
    bar.CreateDisplayColorAttr([Gf.Vec3f(0.97, 0.98, 0.94)])
    material_path = parent_path + "/Materials/MicroStirBarPTFE"
    material = UsdShade.Material.Define(stage, material_path)
    shader = UsdShade.Shader.Define(stage, material_path + "/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(.97, .98, .94))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(.34)
    shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(1.0)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(bar.GetPrim()).Bind(material)
    bar_xform = UsdGeom.Xformable(bar)
    bar_xform.AddTranslateOp().Set(Gf.Vec3d(x, y, floor_z + 0.00105))
    bar_rotate = bar_xform.AddRotateZOp()
    bar_rotate.Set(0.0)

    return {"bar": bar, "bar_rotate": bar_rotate,
            "center_m": [x, y], "floor_z_m": floor_z,
            "bar_dimensions_m": [0.005, 0.002],
            "initial_state": "preplaced_at_cell_floor_before_liquid_addition",
            "motion_type": "illustrative_visual_rotation_during_its_mixing_step",
            "rpm_calibrated": False, "magnetic_coupling_validated": False,
            "liquid_solver": None}


def update_stirring_visuals(visuals, elapsed_s: float, active: bool):
    """Advance animation only; the chemistry ledger is never changed here."""
    if active:
        angle = stirring_angle_degrees(elapsed_s) % 360.0
        visuals["bar_rotate"].Set(angle)

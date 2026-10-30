"""Commercial-style pipette tip rack adapter, in metres.

Reference product: Gilson PIPETMAN DIAMOND D1200ST TIPACK, a 96-tip rack
compatible with transfers in the 50-1200 uL range. The source listing does not
publish dimensions for one rack; this project uses an explicitly estimated
128 x 86 x 90 mm visual envelope and standard 9 mm 8x12 pitch. This is a
traceable visual/kinematic proxy, not an exact CAD replica or validated gripper.
"""

from pipette_visuals import (
    PIPETTE_NOZZLE_BOTTOM_OFFSET_M, PIPETTE_TIP_LENGTH_M, PIPETTE_TIP_RADIUS_M,
    pipette_visual_profile,
)

# Two separately identified 96-position supply modules on a shared bench base.
# Their envelope and placement are project estimates; the D200 module is not
# presented as a factory-exact TIPACK CAD asset.
RACK_XY_BY_PIPETTE = {
    "gilson_pipetman_p200": (0.35, 0.28),
    "gilson_pipetman_p1000": (0.50, 0.28),
}
RACK_XY_M = RACK_XY_BY_PIPETTE["gilson_pipetman_p1000"]
RACK_WIDTH_M = 0.128
RACK_DEPTH_M = 0.086
RACK_HEIGHT_M = 0.090
RACK_TIP_LENGTH_M = PIPETTE_TIP_LENGTH_M
RACK_ROWS = 8
RACK_COLUMNS = 12
RACK_PITCH_M = 0.009
RACK_TOP_Z_M = RACK_HEIGHT_M
# The pipette nozzle underside must meet the top of the tips. Since the
# nozzle-bottom offset is negative relative to the end-effector origin, solve
# EE_z + offset = rack_top_z (subtract the offset, do not add it).
RACK_DOCK_EE_Z_M = RACK_TOP_Z_M - PIPETTE_NOZZLE_BOTTOM_OFFSET_M


def tip_position(index, center=RACK_XY_M):
    """Return a deterministic row-major tip center; reject exhausted racks."""
    index = int(index)
    if not 0 <= index < RACK_ROWS * RACK_COLUMNS:
        raise IndexError("96-position tip rack is exhausted")
    row, column = divmod(index, RACK_COLUMNS)
    x0 = center[0] - (RACK_COLUMNS - 1) * RACK_PITCH_M / 2
    y0 = center[1] - (RACK_ROWS - 1) * RACK_PITCH_M / 2
    return (x0 + column * RACK_PITCH_M, y0 + row * RACK_PITCH_M)


def create_tip_rack(stage, root_path, desktop_z, Gf, UsdGeom,
                    pipette_id="gilson_pipetman_p1000"):
    """Build the named tip supply for one pipette model."""
    if pipette_id not in RACK_XY_BY_PIPETTE:
        raise ValueError(f"Unsupported pipette model for tip rack: {pipette_id!r}")
    x, y = RACK_XY_BY_PIPETTE[pipette_id]
    profile = pipette_visual_profile(pipette_id)
    tip_length = float(profile["tip_length_m"])
    is_p200 = pipette_id == "gilson_pipetman_p200"
    tip_id = profile["tip_id"]
    rack_reference = ("Gilson PIPETMAN DIAMOND D200-compatible 96-position rack proxy"
                      if is_p200 else "Gilson PIPETMAN DIAMOND D1200ST TIPACK")
    base = str(root_path) + f"/tip_rack_{'p200_d200' if is_p200 else 'p1000_d1200'}"
    bottom_z = float(desktop_z)
    rack_height = 0.060 if is_p200 else RACK_HEIGHT_M
    rack_top_z = rack_height
    tray_floor_z = bottom_z + 0.004
    tip_top_z = bottom_z + rack_top_z
    tip_bottom_z = tip_top_z - tip_length

    def cube(name, center, size, color):
        obj = UsdGeom.Cube.Define(stage, base + "/" + name)
        obj.CreateSizeAttr(1.0)
        obj.CreateDisplayColorAttr([Gf.Vec3f(*color)])
        obj.AddTranslateOp().Set(Gf.Vec3d(*center))
        obj.AddScaleOp().Set(Gf.Vec3f(*size))
        return obj

    color = (0.61, 0.69, 0.77) if is_p200 else (0.48, 0.68, 0.78)
    wall = 0.003
    wall_center_z = bottom_z + rack_height / 2
    cube("base", (x, y, bottom_z + 0.002),
         (RACK_WIDTH_M, RACK_DEPTH_M, 0.004), (0.73, 0.81, 0.84))
    cube("left_wall", (x - RACK_WIDTH_M/2 + wall/2, y, wall_center_z),
         (wall, RACK_DEPTH_M, rack_height - 0.004), color)
    cube("right_wall", (x + RACK_WIDTH_M/2 - wall/2, y, wall_center_z),
         (wall, RACK_DEPTH_M, rack_height - 0.004), color)
    cube("front_wall", (x, y - RACK_DEPTH_M/2 + wall/2, wall_center_z),
         (RACK_WIDTH_M, wall, rack_height - 0.004), color)
    cube("back_wall", (x, y + RACK_DEPTH_M/2 - wall/2, wall_center_z),
         (RACK_WIDTH_M, wall, rack_height - 0.004), color)
    cube("grid_support", (x, y, tray_floor_z + 0.001),
         (RACK_WIDTH_M - 2*wall, RACK_DEPTH_M - 2*wall, 0.002),
         (0.79, 0.86, 0.88))

    tips = []
    for index in range(RACK_ROWS * RACK_COLUMNS):
        tx, ty = tip_position(index, center=(x, y))
        tip = UsdGeom.Cone.Define(stage, base + f"/tips/tip_{index:02d}")
        tip.CreateRadiusAttr(PIPETTE_TIP_RADIUS_M)
        tip.CreateHeightAttr(tip_length)
        tip.CreateAxisAttr("Z")
        tip.CreateDisplayColorAttr([Gf.Vec3f(0.90, 0.94, 0.96)])
        tip.AddTranslateOp().Set(Gf.Vec3d(tx, ty,
            (tip_top_z + tip_bottom_z) / 2))
        tip.AddRotateXOp().Set(180.0)
        tips.append(tip)

    return {
        "asset_reference": rack_reference,
        "asset_source_url": ("https://www.gilson.com/default/shop-products/pipetman-diamond-tips-d200-tipack.html"
                             if is_p200 else "https://www.gilson.com/default/pipetman-diamond-tips-d1200st-tipack.html"),
        "model": "commercial_96_tip_rack_visual_adapter",
        "pipette_id": pipette_id, "tip_id": tip_id,
        "dimension_status": "project envelope estimate; not manufacturer CAD",
        "xy": (x, y), "desktop_z_m": bottom_z,
        "housing_bottom_z_m": bottom_z,
        "housing_top_z_m": bottom_z + rack_height,
        "rack_bottom_z_m": bottom_z,
        "rack_top_z_m": tip_top_z,
        "tip_bottom_z_m": tip_bottom_z,
        "tip_count": len(tips), "rows": RACK_ROWS,
        "columns": RACK_COLUMNS, "pitch_m": RACK_PITCH_M,
        "tip_length_m": tip_length,
        "size_estimate_m": [RACK_WIDTH_M, RACK_DEPTH_M, rack_height],
        "dock_ee_z_m": bottom_z + rack_top_z - PIPETTE_NOZZLE_BOTTOM_OFFSET_M,
        "tip_positions_m": [tip_position(i, center=(x, y)) for i in range(len(tips))],
        "tip_prims": tips,
    }

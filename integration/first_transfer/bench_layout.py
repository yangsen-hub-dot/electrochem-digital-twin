"""Versioned bench layout, metres; scene and robot targets share these values.

The reaction cells/stirrer, source beakers, tip supply and edge waste station
keep their accepted poses. Only idle tool storage is grouped on one left rail.
This is a design revision applied when building a scene, never an in-run move.
"""

VERSION = 'grouped_dual_pipette_tool_bank_v3'
TOOL_BANK_CENTER_M = (.30, .00, .004)
TOOL_BANK_SIZE_M = (.078, .250, .008)
TOOL_BANK_TOP_M = .008
PIPETTE_STAND_XY_BY_ID = {
    'gilson_pipetman_p200': (.275, .080),
    'gilson_pipetman_p1000': (.325, .080),
}
PIPETTE_STAND_XY = PIPETTE_STAND_XY_BY_ID['gilson_pipetman_p200']
PIPETTE_DOCK_EE_Z_M = .225
COPPER_RACK_XY = (.30, .000)
COPPER_RACK_CAP_CENTER_Z_M = .145
BRIDGE_RACK_CENTER_M = (.30, -.080, .150)
FIXTURE_COLOR = (.43, .51, .56)
FORK_COLOR = (.67, .75, .79)


def create_tool_bank(stage, root_path):
    from pxr import Gf, UsdGeom
    base = UsdGeom.Cube.Define(stage, str(root_path) + '/SharedToolBank/Base')
    base.CreateSizeAttr(1.)
    base.AddTranslateOp().Set(Gf.Vec3d(*TOOL_BANK_CENTER_M))
    base.AddScaleOp().Set(Gf.Vec3f(*TOOL_BANK_SIZE_M))
    base.CreateDisplayColorAttr([Gf.Vec3f(.36, .44, .49)])
    return {'layout_version': VERSION, 'center_m': list(TOOL_BANK_CENTER_M),
            'size_m': list(TOOL_BANK_SIZE_M), 'desktop_contact_z_m': 0.,
            'rack_support_plane_z_m': TOOL_BANK_TOP_M,
            'stations': {'pipette_p200': [*PIPETTE_STAND_XY_BY_ID['gilson_pipetman_p200'], PIPETTE_DOCK_EE_Z_M],
                         'pipette_p1000': [*PIPETTE_STAND_XY_BY_ID['gilson_pipetman_p1000'], PIPETTE_DOCK_EE_Z_M],
                         'electrodes': [*COPPER_RACK_XY, COPPER_RACK_CAP_CENTER_Z_M],
                         'salt_bridge': list(BRIDGE_RACK_CENTER_M)},
            'applied_at_scene_build_only': True,
            'reactor_and_liquid_source_poses_unchanged': True}

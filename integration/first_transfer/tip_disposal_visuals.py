"""Waste station and an 84 mm commercial-format ejected tip proxy, in metres.

No suitable standalone ChemLab waste station was confirmed in the catalogue.
This small custom open box only fills that gap; existing labware stays unchanged.
Residual amount belongs to TransferSession's waste ledger, not this geometry.
"""

WASTE_XY = (.40, -.32)
WASTE_FLOOR_Z = 0.0  # Matterix bench datum; asset-derived vessel floor is z≈0.001 m.
WASTE_RIM_Z = .096
WASTE_INNER_HALF = .042
DISCARDED_TIP_LENGTH_M = .084
DISCARDED_TIP_RADIUS_M = .0042


def falling_center(start, progress, length_m=DISCARDED_TIP_LENGTH_M):
    """Same tip falls into the box; final centre is above the box floor."""
    p = min(1.0, max(0.0, progress))
    end_z = WASTE_FLOOR_Z + float(length_m) / 2
    return (start[0], start[1], start[2] + (end_z - start[2]) * p * p)


class DisposalVisuals:
    def __init__(self, stage, root_path):
        from pxr import Gf, UsdGeom
        self.Gf = Gf
        x, y = WASTE_XY
        base = str(root_path) + '/tip_waste_station'
        wall_height = WASTE_RIM_Z - .006
        wall_center_z = .003 + wall_height / 2
        # Five separate solids leave the mouth and interior visibly open.
        for name, pos, size in [
            ('floor', (x, y, .003), (.096, .096, .006)),
            ('back', (x, y + .046, wall_center_z), (.096, .004, wall_height)),
            ('front', (x, y - .046, wall_center_z), (.096, .004, wall_height)),
            ('left', (x - .046, y, wall_center_z), (.004, .088, wall_height)),
            ('right', (x + .046, y, wall_center_z), (.004, .088, wall_height)),
        ]:
            cube = UsdGeom.Cube.Define(stage, base + '/' + name)
            cube.CreateSizeAttr(1)
            cube.CreateDisplayColorAttr([Gf.Vec3f(.32, .48, .57)])
            cube.AddTranslateOp().Set(Gf.Vec3d(*pos))
            cube.AddScaleOp().Set(Gf.Vec3f(*size))
        self.tip = UsdGeom.Cone.Define(stage, base + '/discarded_tip')
        self.tip.CreateRadiusAttr(DISCARDED_TIP_RADIUS_M)
        self.tip.CreateHeightAttr(DISCARDED_TIP_LENGTH_M)
        self.tip.CreateAxisAttr('Z')
        self.tip.CreateDisplayColorAttr([Gf.Vec3f(.85, .9, .95)])
        self.translate = self.tip.AddTranslateOp()
        self.tip.AddRotateXOp().Set(180.0)
        self.tip.CreateVisibilityAttr('invisible')

    def set_tip_model(self, length_m, radius_m=DISCARDED_TIP_RADIUS_M):
        """Match the discarded visual cone to the currently attached tip proxy."""
        self.tip.GetHeightAttr().Set(float(length_m))
        self.tip.GetRadiusAttr().Set(float(radius_m))

    def set_position(self, position):
        self.translate.Set(self.Gf.Vec3d(*position))
        self.tip.GetVisibilityAttr().Set('inherited')

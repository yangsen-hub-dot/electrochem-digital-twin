"""Render-only cosmetics; never modifies joints, collisions or chemical state."""


def soften_dark_robot_materials(stage, material_root):
    """Lighten explicitly dark solid colors only on non-instanced robot meshes.

    Missing/texture-only colors are left intact rather than guessing their color.
    Return evidence for the manifest; no on-disk asset is rewritten.
    """
    from pxr import Gf, Sdf, UsdGeom, UsdShade
    replacement = UsdShade.Material.Define(stage, material_root + '/RobotSoftGraphite')
    shader = UsdShade.Shader.Define(stage, material_root + '/RobotSoftGraphite/Surface')
    shader.CreateIdAttr('UsdPreviewSurface')
    shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(.28, .31, .35))
    shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(.6)
    replacement.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
    changed, skipped = [], []
    for prim in list(stage.Traverse()):
        path = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Mesh) or not any(
                part.startswith(('robot', 'franka', 'panda')) for part in path.lower().split('/')):
            continue
        if prim.IsInstanceProxy():
            skipped.append(path)
            continue
        material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if not material:
            continue
        dark = False
        # Traverse the bound material, whose shaders may not sit under /Robot.
        from pxr import Usd
        for child in Usd.PrimRange(material.GetPrim()):
            for name in ('inputs:diffuseColor', 'inputs:diffuse_color_constant', 'inputs:base_color'):
                attr = child.GetAttribute(name)
                color = attr.Get() if attr else None
                if color is not None and hasattr(color, '__len__') and len(color) >= 3:
                    if max(float(color[i]) for i in range(3)) <= .08:
                        dark = True
        if dark:
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(replacement,
                                                       bindingStrength=UsdShade.Tokens.strongerThanDescendants)
            changed.append(path)
    return {'strategy': 'render_only_dark_solid_color_to_graphite',
            'changed_meshes': changed, 'skipped_instance_proxies': skipped,
            'geometry_removed': False}

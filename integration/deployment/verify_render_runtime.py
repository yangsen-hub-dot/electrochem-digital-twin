"""Render a non-experimental headless test frame; never submits an experiment."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
sys.argv = [sys.argv[0]]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "first_transfer"))
from simulator_runtime import launch_headless_simulator

app = launch_headless_simulator()
print('SMOKE_APP_READY', flush=True)
product = rgb = None
try:
    import numpy as np
    import omni.replicator.core as rep
    import omni.usd
    from PIL import Image
    from pxr import Gf, UsdGeom, UsdLux

    stage = omni.usd.get_context().get_stage()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    cube = UsdGeom.Cube.Define(stage, "/World/TestCube")
    cube.CreateSizeAttr(1.0)
    cube.CreateDisplayColorAttr([(0.08, 0.5, 0.8)])
    light = UsdLux.DomeLight.Define(stage, "/World/Light")
    light.CreateIntensityAttr(1500.)
    camera = UsdGeom.Camera.Define(stage, "/World/TestCamera")
    camera.CreateFocalLengthAttr(30.)
    transform = Gf.Matrix4d().SetLookAt(Gf.Vec3d(2.5, 2.5, 2), Gf.Vec3d(0, 0, 0), Gf.Vec3d(0, 0, 1)).GetInverse()
    UsdGeom.Xformable(camera).AddTransformOp().Set(transform)
    product = rep.create.render_product(str(camera.GetPath()), (640, 480))
    rgb = rep.AnnotatorRegistry.get_annotator("rgb")
    rgb.attach(product)
    print('SMOKE_RGB_ATTACHED', flush=True)
    for _ in range(180):
        app.update()
        frame = rgb.get_data()
        frame = frame.get("data") if isinstance(frame, dict) else frame
        if frame is not None and np.asarray(frame).ndim == 3 and np.asarray(frame).std() > 2:
            break
    frame = np.asarray(frame)
    if frame.ndim != 3 or frame.size == 0 or float(frame.std()) <= 2:
        raise RuntimeError("Renderer did not produce a non-uniform RGB frame")
    args.output.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frame[:, :, :3].astype(np.uint8)).save(args.output / "runtime_render_smoke.png")
    result = {"status": "passed", "shape": list(frame.shape), "pixel_std": float(frame.std()),
              "actions_executed": 0, "experiment_submitted": False}
    (args.output / "runtime_render_smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("RUNTIME_RENDER_OK", json.dumps(result), flush=True)
finally:
    if rgb is not None: rgb.detach()
    if product is not None: product.destroy()
    app.close()

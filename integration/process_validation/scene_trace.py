"""Renderer-side telemetry only; never reads or writes error RNG/controller state.

Keeps actual rigid hand/finger visual meshes once, exact fixture cubes once and
one transform/state sample per encoded video frame. Does not claim continuous
collision coverage or fabricate data for old recordings.
"""
import json
from pathlib import Path
import numpy as np

from .report import atomic_json
from .geometry import derive_gripper_approach_axis_world


def body_ancestor_path(mesh_path, body_names):
    # USD visual Mesh and rigid link can share the same name (panda_hand).
    # The mesh itself must never be mistaken for the articulated body.
    parts=str(mesh_path).split('/')[:-1]
    for index in range(len(parts)-1,0,-1):
        if parts[index] in body_names:
            return '/'.join(parts[:index+1]), body_names.index(parts[index])
    return None


class SceneTrace:
    def __init__(self, stage, run_dir, run_id, policy, articulation=None):
        from pxr import Gf, Usd, UsdGeom
        self.stage, self.run_id, self.policy = stage, run_id, policy
        self.articulation = articulation
        self.body_names = list(articulation.body_names) if articulation is not None else []
        self.bindings = {}
        self.preflight_prefix = None
        self.preflight_scene = None
        self.last_sample = None
        self.last_preflight_intersections = None
        self.root = Path(run_dir) / "process_validation"
        self.root.mkdir(exist_ok=True)
        self.meshes, obstacles, unsupported = {}, [], []
        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        # Include render geometry inside USD instance proxies.
        for prim in Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies()):
            path = str(prim.GetPath())
            if any(token in path for token in policy["obstacle_path_tokens"]) and prim.IsA(UsdGeom.Cube):
                cube = UsdGeom.Cube(prim)
                matrix = cache.GetLocalToWorldTransform(prim)
                inverse = matrix.GetInverse()
                size = float(cube.GetSizeAttr().Get())
                corners = [[float(v) for v in matrix.Transform(Gf.Vec3d(x*size/2,y*size/2,z*size/2))]
                           for x in (-1,1) for y in (-1,1) for z in (-1,1)]
                obstacles.append({"path": path, "shape": "exact_usd_cube", "size": size,
                                  "matrix": [list(row) for row in matrix],
                                  "inverse_matrix": [list(row) for row in inverse], "corners": corners})
            if not any(token in path.lower() for token in policy["robot_path_tokens"]):
                continue
            if not prim.IsA(UsdGeom.Mesh) or any(token in path.lower() for token in ("/collisions/", "/collision/")):
                continue
            if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get()
            counts, indices = list(mesh.GetFaceVertexCountsAttr().Get() or []), list(mesh.GetFaceVertexIndicesAttr().Get() or [])
            if not points or not counts or any(n not in (3,4) for n in counts):
                unsupported.append(path)
                continue
            faces, offset = [], 0
            for count in counts:
                face = indices[offset:offset+count]
                faces.extend([[face[0], face[i], face[i+1]] for i in range(1,count-1)])
                offset += count
            self.meshes[path] = {"points": [list(point) for point in points], "triangles": faces}
            # Read actual PhysX articulation poses instead of stale USD link
            # transforms when Isaac Fabric bypasses USD transform updates.
            binding=body_ancestor_path(path,self.body_names)
            if binding is not None:
                body_path,index=binding
                body_world=cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body_path))
                mesh_world=cache.GetLocalToWorldTransform(prim)
                # Physics poses carry translation/rotation, not USD scale.
                body_scale=np.linalg.norm(np.asarray(body_world)[:3,:3],axis=1)
                scale=np.eye(4);scale[:3,:3]=np.diag(body_scale)
                local=np.asarray(mesh_world*body_world.GetInverse())@scale
                self.bindings[path]=(index,local)
                self.meshes[path]['body_path']=body_path
                self.meshes[path]['mesh_to_physics_link_matrix']=local.tolist()
        geometry = {"schema_version": "1.0", "run_id": run_id,
                    "obstacles": obstacles, "bodies": self.meshes,
                    "unsupported_meshes": unsupported, "coverage": "rigid_hand_fingers_vs_named_fixture_cubes",
                    "pose_source": "physx_articulation_body_poses" if self.bindings else "usd_only_unverified",
                    "bound_body_count": len(self.bindings)}
        atomic_json(self.root / "geometry.json", geometry)
        self.geometry = geometry
        self.stream = (self.root / "trace.jsonl").open("x", encoding="utf-8")
        self.failed = False

    def enable_preflight_collision_gate(self, phase_prefix):
        """Check every recorded frame in a bounded pose-preflight segment."""
        from .preflight_collision import prepare_scene
        self.preflight_scene = prepare_scene(self.geometry, self.run_id)
        self.preflight_prefix = str(phase_prefix)
        if not self.preflight_prefix:
            raise ValueError("preflight phase prefix is required")

    def sample(self, frame_state):
        from pxr import Usd, UsdGeom
        try:
            cache = UsdGeom.XformCache(Usd.TimeCode.Default())
            transforms = {path: [list(row) for row in cache.GetLocalToWorldTransform(self.stage.GetPrimAtPath(path))]
                          for path in self.meshes}
            closing_axis = None
            approach_axis = None
            if self.articulation is not None:
                positions = self.articulation.data.body_pos_w[0].detach().cpu().numpy()
                quaternions = self.articulation.data.body_quat_w[0].detach().cpu().numpy()
                for path, (index, local) in self.bindings.items():
                    w,x,y,z = map(float, quaternions[index])
                    rotation = np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                                         [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                                         [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
                    matrix = np.eye(4); matrix[:3,:3] = rotation.T; matrix[3,:3] = positions[index]
                    transforms[path] = (local@matrix).tolist()
                if all(name in self.body_names for name in ('panda_leftfinger','panda_rightfinger')):
                    delta = positions[self.body_names.index('panda_leftfinger')]-positions[self.body_names.index('panda_rightfinger')]
                    length = np.linalg.norm(delta)
                    if length > 1e-8: closing_axis = (delta/length).tolist()
                approach_axis = derive_gripper_approach_axis_world(
                    self.geometry, {"body_transforms": transforms})
            moved = [row["path"] for row in self.geometry["obstacles"]
                     if [list(v) for v in cache.GetLocalToWorldTransform(self.stage.GetPrimAtPath(row["path"]))] != row["matrix"]]
            sample = {**frame_state, "run_id": self.run_id,
                      "body_transforms": transforms, "closing_axis_world": closing_axis,
                      "gripper_approach_axis_world": approach_axis,
                      "moved_obstacles": moved}
            self.last_sample = sample
            self.last_preflight_intersections = None
            if (self.preflight_prefix is not None
                    and str(frame_state.get("phase", "")).startswith(self.preflight_prefix)):
                from .preflight_collision import sample_intersections
                self.last_preflight_intersections = sample_intersections(self.preflight_scene, sample)
                if moved:
                    self.last_preflight_intersections.append({
                        "preflight_error": "static obstacle moved during preflight",
                        "obstacles": moved,
                    })
            self.stream.write(json.dumps(sample, allow_nan=False)+"\n")
            self.stream.flush()
        except Exception as exc:
            self.failed = True
            if (self.preflight_prefix is not None
                    and str(frame_state.get("phase", "")).startswith(self.preflight_prefix)):
                self.last_preflight_intersections = [{"preflight_error": str(exc)}]
            atomic_json(self.root / "trace_error.json", {"message": str(exc), "frame_index": frame_state.get("frame_index")})
            raise

    def close(self):
        self.stream.close()

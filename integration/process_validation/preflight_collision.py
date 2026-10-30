"""Per-sample collision gate used before electrode descent or gripper closure."""
from .geometry import transform, triangles_in_box
from .geometry_evidence import affine_matrix, validated_scene


def prepare_scene(geometry, run_id):
    return validated_scene(geometry, run_id)


def sample_intersections(prepared_scene, sample):
    """Return exact mesh/cube intersections for one recorded robot pose."""
    bodies, obstacles = prepared_scene
    transforms = sample.get("body_transforms", {}) if isinstance(sample, dict) else {}
    collisions = []
    for body_path, body in bodies.items():
        matrix = transforms.get(body_path)
        if matrix is None:
            raise ValueError(f"missing body transform: {body_path}")
        world_points = transform(body["points"], affine_matrix(matrix))
        for obstacle in obstacles:
            if triangles_in_box(world_points, body["triangles"], obstacle["inverse_matrix"],
                                obstacle["size"], tolerance=0.0):
                collisions.append({"body": body_path, "obstacle": obstacle["path"]})
    return collisions

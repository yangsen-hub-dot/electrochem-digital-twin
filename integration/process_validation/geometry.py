"""Numpy geometry routines. Units metres, USD row-vector matrices.

Mesh triangles versus exact fixture cubes use the triangle/box separating-axis
test. Bounding-box overlap alone is never reported as a confirmed collision.
"""
import numpy as np


def transform(points, matrix):
    p = np.asarray(points, dtype=float)
    m = np.asarray(matrix, dtype=float)
    return p @ m[:3, :3] + m[3, :3]


def derive_gripper_approach_axis_world(geometry, sample):
    """Derive the finger-tip direction from recorded hand/finger meshes.

    The principal long axis of each finger mesh is oriented from the palm
    toward the farther end, then the two finger axes are averaged. This is
    distinct from the jaw closing axis, which comes from left/right finger
    separation.
    """
    bodies = geometry.get("bodies", {})
    transforms = sample.get("body_transforms", {})
    grouped = {"hand": [], "left": [], "right": []}
    for path, body in bodies.items():
        body_path = body.get("body_path", "")
        key = ("hand" if body_path.endswith("/panda_hand") else
               "left" if body_path.endswith("/panda_leftfinger") else
               "right" if body_path.endswith("/panda_rightfinger") else None)
        if key is None or path not in transforms:
            continue
        points = np.asarray(body.get("points", []), dtype=float)
        if points.ndim != 2 or points.shape[1] != 3 or len(points) < 3:
            continue
        grouped[key].append(transform(points, transforms[path]))
    if not all(grouped.values()):
        return None
    hand = np.vstack(grouped["hand"]).mean(axis=0)
    axes = []
    for key in ("left", "right"):
        points = np.vstack(grouped[key])
        center = points.mean(axis=0)
        values, vectors = np.linalg.eigh(np.cov((points - center).T))
        axis = vectors[:, int(np.argmax(values))]
        projections = (points - center) @ axis
        endpoints = (center + float(projections.min()) * axis,
                     center + float(projections.max()) * axis)
        distal = max(endpoints, key=lambda point: float(np.linalg.norm(point - hand)))
        if float(np.dot(distal - center, axis)) < 0:
            axis = -axis
        axes.append(axis)
    axis = axes[0] + axes[1]
    length = float(np.linalg.norm(axis))
    if not np.isfinite(length) or length <= 1e-8:
        return None
    return (axis / length).tolist()


def triangles_in_box(vertices, faces, inverse_box_matrix, size, tolerance):
    p = transform(vertices, inverse_box_matrix)
    half = np.full(3, float(size) / 2)
    # Transform tolerance from world metres to each local box axis.
    inv = np.asarray(inverse_box_matrix)
    shrink = tolerance * np.linalg.norm(inv[:3, :3], axis=0)
    half -= shrink
    if (half <= 0).any():
        return False
    tri = p[np.asarray(faces, dtype=int)]
    candidates = np.all((tri.min(axis=1) <= half) & (tri.max(axis=1) >= -half), axis=1)
    tri = tri[candidates]
    if not len(tri):
        return False
    edges = np.roll(tri, -1, axis=1) - tri
    normals = np.cross(edges[:, 0], edges[:, 1])
    axes = [np.broadcast_to(a, (len(tri), 3)) for a in np.eye(3)] + [normals]
    axes.extend(np.cross(edges[:, i], a) for i in range(3) for a in np.eye(3))
    intersects = np.ones(len(tri), dtype=bool)
    for axis in axes:
        projections = np.einsum("nkj,nj->nk", tri, axis)
        radius = np.abs(axis) @ half
        intersects &= (projections.min(axis=1) <= radius) & (projections.max(axis=1) >= -radius)
    return bool(intersects.any())


def aabb_gap(points, lower, upper):
    p = np.asarray(points)
    return float(np.linalg.norm(np.maximum(0, np.maximum(np.asarray(lower)-p.max(axis=0), p.min(axis=0)-np.asarray(upper)))))


def swept_aabb_possible(previous, current, lower, upper):
    all_points = np.vstack((previous, current))
    return bool(np.all(all_points.max(axis=0) >= lower) and np.all(all_points.min(axis=0) <= upper))

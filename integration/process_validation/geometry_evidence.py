"""Validate captured numeric geometry before collision/pose arithmetic.

Matrices use USD row vectors and may include asset-unit scaling. The 1e-4
axis-norm and 1e-8 affine tolerances handle serialization/roundoff only; they
are not physical accuracy or clearance specifications.
"""
import numpy as np

from .geometry import transform


class GeometryEvidenceError(ValueError):
    def __init__(self, message, code='invalid_geometry_evidence', status='fail'):
        super().__init__(message)
        self.code, self.status = code, status


def numeric_array(value, shape=None):
    try:
        raw = np.asarray(value)
        if raw.dtype.kind not in 'iuf' or not raw.size:
            raise ValueError
        result = raw.astype(float)
        if not np.isfinite(result).all() or (shape is not None and result.shape != shape):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise GeometryEvidenceError('几何输入必须为形状正确的有限数值，不能为布尔值、字符串或空数组')
    # Mixed numeric/bool lists otherwise lose their boolean type in numpy.
    def has_bool(item):
        return isinstance(item, (bool, np.bool_)) or (
            isinstance(item, (list, tuple)) and any(has_bool(v) for v in item))
    if has_bool(value):
        raise GeometryEvidenceError('几何输入不能包含布尔值')
    return result


def unit_axis(value):
    axis = numeric_array(value, (3,))
    length = float(np.linalg.norm(axis))
    if not np.isfinite(length) or abs(length - 1.) > 1e-4:
        raise GeometryEvidenceError('方向向量不是有效单位向量')
    return axis / length


def affine_matrix(value):
    matrix = numeric_array(value, (4, 4))
    if (not np.allclose(matrix[:, 3], [0., 0., 0., 1.], rtol=0., atol=1e-8)
            or np.linalg.matrix_rank(matrix[:3, :3]) != 3):
        raise GeometryEvidenceError('缺少有效可逆的 USD 行向量仿射变换')
    return matrix


def mesh_arrays(body):
    if not isinstance(body, dict):
        raise GeometryEvidenceError('手部网格结构无效')
    points = numeric_array(body.get('points'))
    faces = numeric_array(body.get('triangles'))
    if (points.ndim != 2 or points.shape[1] != 3 or len(points) < 3
            or faces.ndim != 2 or faces.shape[1] != 3
            or not np.equal(faces, np.floor(faces)).all()
            or (faces < 0).any() or (faces >= len(points)).any()):
        raise GeometryEvidenceError('手部网格顶点或三角面索引无效')
    faces = faces.astype(int)
    tri = points[faces]
    area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    if not np.isfinite(area).all() or (area <= 0).any():
        raise GeometryEvidenceError('手部网格含退化三角面，不能据此确认无相交')
    return {'points': points, 'triangles': faces}


def validated_scene(geometry, run_id):
    if not isinstance(geometry, dict) or geometry.get('run_id') != run_id:
        raise GeometryEvidenceError('几何来源运行编号不一致', 'identity')
    bodies, obstacles = geometry.get('bodies'), geometry.get('obstacles')
    if not isinstance(bodies, dict) or not bodies or not isinstance(obstacles, list) or not obstacles:
        raise GeometryEvidenceError('缺少非空手部网格或支架集合，不能预检通过', 'coverage', 'uncovered')
    checked_bodies = {path: mesh_arrays(body) for path, body in bodies.items()}
    checked_obstacles, paths = [], set()
    for row in obstacles:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not row['path'] or row['path'] in paths:
            raise GeometryEvidenceError('支架编号缺失或重复')
        paths.add(row['path'])
        size = numeric_array([row.get('size')], (1,))[0]
        if size <= 0:
            raise GeometryEvidenceError('支架立方体尺寸必须大于零')
        matrix, inverse = affine_matrix(row.get('matrix')), affine_matrix(row.get('inverse_matrix'))
        corners = numeric_array(row.get('corners'), (8, 3))
        expected = transform([[x, y, z] for x in (-size/2, size/2)
                              for y in (-size/2, size/2) for z in (-size/2, size/2)], matrix)
        distances = np.linalg.norm(corners[:, None] - expected[None, :], axis=2)
        if (len(np.unique(corners, axis=0)) != 8
                or not np.allclose(matrix @ inverse, np.eye(4), atol=1e-8, rtol=1e-8)
                or (distances.min(axis=0) > 1e-8).any() or (distances.min(axis=1) > 1e-8).any()):
            raise GeometryEvidenceError('支架角点、变换和逆变换不一致，不能使用包围盒初筛')
        checked_obstacles.append({**row, 'lower': corners.min(axis=0), 'upper': corners.max(axis=0)})
    return checked_bodies, checked_obstacles

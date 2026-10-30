"""Small, auditable pose helpers for high-clearance electrode preflight."""
import math


def _unit_quaternion(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) for item in value)):
        raise ValueError("quaternion must contain four finite numbers")
    norm = math.sqrt(sum(float(item) ** 2 for item in value))
    if norm < 1e-9:
        raise ValueError("quaternion norm is zero")
    return [float(item) / norm for item in value]


def pregrasp_pose(target_xyz, transit_height_m, xy_offset_m=(0.0, 0.0)):
    """Return a rack-aligned pregrasp waypoint while preserving the final target."""
    if (not isinstance(target_xyz, (list, tuple)) or len(target_xyz) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(float(v)) for v in target_xyz)):
        raise ValueError("target_xyz must contain three finite numbers")
    if (not isinstance(xy_offset_m, (list, tuple)) or len(xy_offset_m) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(float(v)) for v in xy_offset_m)
            or math.hypot(*map(float, xy_offset_m)) > 0.04):
        raise ValueError("pregrasp offset must be finite and within 40 mm")
    height = float(transit_height_m)
    if not math.isfinite(height) or not 0.42 <= height <= 0.55:
        raise ValueError("pregrasp height is outside the verified high-clearance envelope")
    return (float(target_xyz[0]) + float(xy_offset_m[0]),
            float(target_xyz[1]) + float(xy_offset_m[1]),
            max(height, float(target_xyz[2])))


def orientation_waypoints(start_wxyz, target_wxyz, max_step_deg=30.0):
    """Shortest-path quaternion SLERP; each returned pose is a bounded rotation step."""
    start, target = _unit_quaternion(start_wxyz), _unit_quaternion(target_wxyz)
    if (isinstance(max_step_deg, bool) or not isinstance(max_step_deg, (int, float))
            or not math.isfinite(float(max_step_deg)) or not 1.0 <= float(max_step_deg) <= 45.0):
        raise ValueError("orientation step must be between 1 and 45 degrees")
    dot = sum(a * b for a, b in zip(start, target))
    if dot < 0.0:
        target = [-value for value in target]
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    angle = 2.0 * math.acos(dot)
    segments = max(1, math.ceil(math.degrees(angle) / float(max_step_deg)))
    if angle < 1e-8:
        return [target]
    sin_angle = math.sin(angle / 2.0)
    result = []
    for index in range(1, segments + 1):
        t = index / segments
        left = math.sin((1.0 - t) * angle / 2.0) / sin_angle
        right = math.sin(t * angle / 2.0) / sin_angle
        result.append(_unit_quaternion([left * a + right * b for a, b in zip(start, target)]))
    result[-1] = target
    return result


def orientation_error_deg(actual_wxyz, target_wxyz):
    """Shortest rotation angle; q and -q describe the same measured pose."""
    actual, target = _unit_quaternion(actual_wxyz), _unit_quaternion(target_wxyz)
    dot = min(1.0, abs(sum(a*b for a, b in zip(actual, target))))
    return math.degrees(2.0 * math.acos(dot))

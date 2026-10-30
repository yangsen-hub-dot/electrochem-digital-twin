"""Task-space acceptance for a stalled high, side-grasp preflight only.

This does not approve a grasp, a low waypoint, or an electrode installation.
"""
from __future__ import annotations

import math


SUPPORTED_PHASES = {
    "pick_electrode/align/orientation_probe",
    "pick_electrode/align/high_transit_probe",
}


def high_probe_physically_reached(*, phase, target_m, actual_m,
                                  closing_axis_world, tolerance_m):
    if phase not in SUPPORTED_PHASES or not .008 < tolerance_m <= .015:
        return False
    vectors = (target_m, actual_m, closing_axis_world)
    if any(not isinstance(vector, (tuple, list)) or len(vector) != 3
           or not all(isinstance(value, (int, float)) and math.isfinite(value)
                      for value in vector) for vector in vectors):
        return False
    if float(target_m[2]) < .42 or float(actual_m[2]) < .42:
        return False
    if abs(float(closing_axis_world[2])) > .2:
        return False
    return max(abs(float(actual_m[i]) - float(target_m[i])) for i in range(3)) <= tolerance_m

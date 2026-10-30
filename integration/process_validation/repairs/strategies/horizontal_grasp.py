"""Registered pose reused from the independently checked electrode grasp."""
from math import sqrt, cos, sin, radians


TOP_DOWN_HORIZONTAL_WXYZ = [0.0, sqrt(.5), sqrt(.5), 0.0]
SIDE_FACING_HORIZONTAL_WXYZ = [.5, .5, -.5, .5]
OPPOSED_HORIZONTAL_WXYZ = [-.5, .5, .5, .5]
_c, _s = cos(radians(67.5)), sin(radians(67.5))
DIAGONAL_HORIZONTAL_WXYZ = [(_c-_s)/2, (_c+_s)/2, (_s-_c)/2, (_c+_s)/2]
DIAGONAL_CLOSING_AXIS = [sqrt(.5), -sqrt(.5), 0.]
DIAGONAL_APPROACH_AXIS = [sqrt(.5), sqrt(.5), 0.]


def horizontal_grasp_route(x, y, attempt_index, *, preferred_side, target_frame,
                           side_facing=False, opposed_facing=False, diagonal_facing=False):
    side = preferred_side if attempt_index == 2 else (
        "right" if preferred_side == "left" else "left")
    side_x = x + (-.06 if side == "left" else .06)
    return {
        "frame": "ee_world", "target_frame": target_frame,
        "waypoints_m": ([[.40, .00, .48], [x, y, .48]] if diagonal_facing
                        else [[.40, .00, .48], [side_x, y, .48], [x, y, .48]]),
        "orientation_wxyz": list(DIAGONAL_HORIZONTAL_WXYZ if diagonal_facing else
                                  OPPOSED_HORIZONTAL_WXYZ if opposed_facing else
                                  SIDE_FACING_HORIZONTAL_WXYZ if side_facing
                                  else TOP_DOWN_HORIZONTAL_WXYZ),
        "expected_closing_axis_world": list(DIAGONAL_CLOSING_AXIS) if diagonal_facing else [1., 0., 0.],
        "expected_approach_axis_world": (list(DIAGONAL_APPROACH_AXIS) if diagonal_facing else
                                         [0., 1., 0.] if opposed_facing else
                                         [0., -1., 0.] if side_facing else [0., 0., -1.]),
        "gripper_open_joint_target_m": .012,
        "waypoint_step_m": .035, "approach_side": side,
        "pose_basis": ("measured_mesh_clearance_135deg_horizontal_rotation" if diagonal_facing else
                       "measured_palm_behind_grip_reflected_away_from_cell_support" if opposed_facing else
                       "measured_default_pose_rolled_about_approach_axis_90deg"
                       if side_facing else "checked_electrode_top_down_horizontal_closure"),
    }

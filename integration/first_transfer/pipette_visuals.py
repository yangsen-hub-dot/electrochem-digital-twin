"""Pipette proxy dimensions shared by its grip and parking geometry.

All values are metres. The 10 mm grip waist is matched to the configured
Franka close opening for visual alignment only; it does not model contact
forces or validate hardware grasping.
"""

PIPETTE_BODY_RADIUS_M = 0.009
PIPETTE_BODY_HEIGHT_M = 0.075
PIPETTE_BODY_CENTER_Z_M = -0.030

PIPETTE_GRIP_DIAMETER_M = 0.010
PIPETTE_GRIP_RADIUS_M = PIPETTE_GRIP_DIAMETER_M / 2
PIPETTE_GRIP_HEIGHT_M = 0.012
PIPETTE_GRIP_CENTER_Z_M = -0.004

PIPETTE_LOWER_COLLAR_RADIUS_M = 0.010
PIPETTE_LOWER_COLLAR_HEIGHT_M = 0.012
PIPETTE_LOWER_COLLAR_CENTER_Z_M = -0.070
PIPETTE_NOZZLE_RADIUS_M = 0.004
PIPETTE_NOZZLE_HEIGHT_M = 0.012
PIPETTE_NOZZLE_CENTER_Z_M = -0.082
PIPETTE_TIP_LENGTH_M = 0.084
PIPETTE_TIP_RADIUS_M = 0.0042
PIPETTE_NOZZLE_BOTTOM_OFFSET_M = (PIPETTE_NOZZLE_CENTER_Z_M
                                  - PIPETTE_NOZZLE_HEIGHT_M / 2)
PIPETTE_TIP_CENTER_Z_M = (PIPETTE_NOZZLE_BOTTOM_OFFSET_M
                          - PIPETTE_TIP_LENGTH_M / 2)
PIPETTE_TIP_APEX_OFFSET_M = (PIPETTE_TIP_LENGTH_M / 2
                             - PIPETTE_TIP_CENTER_Z_M)

# These presentation profiles bind the two modeled commercial references to
# visibly distinct tool shapes. Dimensions are project visual proxies, not
# manufacturer CAD or validated robot grasp geometry. The waist/nozzle/tip
# interface remains shared so the existing kinematic action envelope is
# unchanged while model identity is made explicit in the scene and run record.
PIPETTE_VISUAL_PROFILES = {
    "gilson_pipetman_p200": {
        "model_name": "Gilson PIPETMAN P200",
        "tip_id": "PIPETMAN_DIAMOND_D200",
        "volume_range_ul": [20.0, 200.0],
        "body_radius_m": 0.0085,
        "body_height_m": 0.075,
        "body_center_z_m": -0.030,
        "body_color_rgb": (0.20, 0.43, 0.72),
        "tip_length_m": 0.051,
        "geometry_status": "project_visual_proxy_not_manufacturer_CAD",
    },
    "gilson_pipetman_p1000": {
        "model_name": "Gilson PIPETMAN P1000",
        "tip_id": "PIPETMAN_DIAMOND_D1200",
        "volume_range_ul": [100.0, 1000.0],
        "body_radius_m": 0.011,
        "body_height_m": 0.105,
        "body_center_z_m": -0.045,
        "body_color_rgb": (0.12, 0.34, 0.58),
        "tip_length_m": 0.084,
        "geometry_status": "project_visual_proxy_not_manufacturer_CAD",
    },
}


def pipette_visual_profile(pipette_id):
    """Return a copy of the visual/reference identity for a selected model."""
    try:
        profile = PIPETTE_VISUAL_PROFILES[str(pipette_id)]
    except KeyError as exc:
        raise ValueError(f"Unsupported pipette model: {pipette_id!r}") from exc
    return {**profile, "pipette_id": str(pipette_id)}


def pipette_grip_aligned(gripper_gap_m, tolerance_m=0.003):
    """Return whether the measured close gap matches the visual grip waist."""
    return abs(float(gripper_gap_m) - PIPETTE_GRIP_DIAMETER_M) <= tolerance_m


def pipette_stand_contact_z(dock_grasp_z_m):
    """Height of the parking-fork surface at the lower collar's bottom face."""
    return (float(dock_grasp_z_m) + PIPETTE_LOWER_COLLAR_CENTER_Z_M
            - PIPETTE_LOWER_COLLAR_HEIGHT_M / 2)

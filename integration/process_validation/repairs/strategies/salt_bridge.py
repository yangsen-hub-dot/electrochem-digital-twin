"""Constrained clearance routes for salt-bridge pickup and installation."""
from .horizontal_grasp import horizontal_grasp_route


def route_for_salt_bridge(step_id, issues, manifest, attempt_index):
    presentation = manifest.get("presentation", {}).get("salt_bridge", {})
    target = (presentation.get("rack_center_m") if step_id == "ui_pick_salt_bridge"
              else presentation.get("target_center_m"))
    if not isinstance(target, list) or len(target) != 3:
        return None
    # Route above the fixture field, then descend at the unchanged grasp or
    # holder target. The candidate changes travel only, never bridge geometry.
    corridor_y, transit_z = {
        1: (-.30, .48), 2: (-.36, .48), 3: (.08, .50),
    }.get(attempt_index, (None, None))
    if corridor_y is None:
        return None
    x, y, _ = map(float, target)
    route = {
        "candidate_id": f"salt_bridge_clearance_route_v{attempt_index}",
        "frame": "ee_world",
        "waypoints_m": [[.42, corridor_y, transit_z], [x, corridor_y, transit_z],
                        [x, y, transit_z]],
        "approach_z_m": .34 if step_id == "ui_pick_salt_bridge" else .30,
        "preserves_target": True,
        "evidence_obstacles": sorted({row.get("obstacle", "") for row in issues if row.get("obstacle")}),
    }
    if attempt_index >= 2:
        route.update(horizontal_grasp_route(x, y, attempt_index,
                                            preferred_side="left", target_frame="ee_world"))
        route["grasp_reference_offset_z_m"] = .008
    return route

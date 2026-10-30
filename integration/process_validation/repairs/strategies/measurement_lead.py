"""Constrained clearance routes for connecting cell measurement leads."""
from .horizontal_grasp import horizontal_grasp_route


def route_for_lead(step_id, issues, manifest, attempt_index):
    side = "left" if "left" in step_id else "right"
    targets = manifest.get("presentation", {}).get("measurement", {}).get("targets_m", {})
    if step_id.startswith("ui_pick_"):
        targets = manifest.get("presentation", {}).get("measurement", {}).get(
            "rack_grip_centers_m", {})
    target = targets.get(side)
    if not isinstance(target, list) or len(target) != 3:
        return None
    x, y, _ = map(float, target)
    # Preserve the measured electrical contact target; travel reaches it from
    # a high front corridor and descends only after clearing the cell support.
    corridor_y, transit_z = {
        1: (-.30, .48), 2: (-.36, .48), 3: (.08, .50), 4: (-.34, .48), 5: (-.30, .48),
    }.get(attempt_index, (None, None))
    if corridor_y is None:
        return None
    route = {
        "candidate_id": f"{side}_lead_clearance_route_v{attempt_index}",
        "frame": "grasp_world",
        "waypoints_m": [[.42, corridor_y, transit_z], [x, corridor_y, transit_z],
                        [x, y, transit_z]],
        "approach_z_m": .30,
        "preserves_target": True,
        "evidence_obstacles": sorted({row.get("obstacle", "") for row in issues if row.get("obstacle")}),
    }
    if attempt_index >= 2:
        route.update(horizontal_grasp_route(x, y, attempt_index,
                                            preferred_side=side, target_frame="grasp_world",
                                            side_facing=attempt_index >= 3,
                                            opposed_facing=attempt_index == 4,
                                            diagonal_facing=attempt_index == 5))
    if attempt_index == 2:
        # This segment resumes with the checked downward pose. Lift locally,
        # cross directly over the actual tab/terminal, then descend in place.
        # The generic central pose-probe point pulls a held tether far away.
        route.update(direct_overhead=True,
                     waypoints_m=[[x, y, .44], [x, y, .42]])
    return route

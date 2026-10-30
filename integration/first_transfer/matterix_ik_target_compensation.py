"""Matterix/IsaacLab 2.2 end-effector target residual correction.

The value is a simulator-specific observation from run
29c594422aff4248a07a96f35dd7e91c (requested y=-0.1600 m, settled y=-0.1564 m).
It compensates only the commanded target; the runner still checks the observed
end-effector pose against vessel geometry. This is not robot calibration and
must be remeasured if the Matterix/Isaac Lab runtime changes.
"""

MATTERIX_2_2_EE_TARGET_RESIDUAL_M = (0.0, 0.0036, 0.0)


def residual_for_runtime(isaaclab_version):
    """Never reuse the 2.2 observation on the original Isaac Lab 3 runtime."""
    if str(isaaclab_version).startswith("2.2."):
        return MATTERIX_2_2_EE_TARGET_RESIDUAL_M
    if str(isaaclab_version).startswith("3.0."):
        return (0.0, 0.0, 0.0)
    raise ValueError(f"Unverified Isaac Lab runtime: {isaaclab_version}")


def compensated_target(pose_m, *, isaaclab_version="2.2.0"):
    """Offset a requested xyz pose by the observed simulator steady-state residual."""
    if len(pose_m) != 3:
        raise ValueError("Matterix target pose must contain exactly xyz")
    return tuple(float(value) - residual
                 for value, residual in zip(pose_m, residual_for_runtime(isaaclab_version)))

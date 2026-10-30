"""Action-family strategy registry."""
from .strategies.measurement_lead import route_for_lead
from .strategies.salt_bridge import route_for_salt_bridge


ROUTE_STRATEGIES = {
    "salt_bridge": route_for_salt_bridge,
    "measurement_lead": route_for_lead,
}


def route_for(target, manifest, attempt_index):
    strategy = ROUTE_STRATEGIES.get(target.family)
    return strategy(target.step_id, target.issues, manifest, attempt_index) if strategy else None

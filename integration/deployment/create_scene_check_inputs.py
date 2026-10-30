"""Generate an initial ledger for a frozen cloud scene check, without executing actions."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--engine-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "first_transfer"))
from transfer_session import TransferSession

conditions = {
    "left_concentration_m": 0.00734, "right_concentration_m": 0.1,
    "left_volume_ml": 1.0, "right_volume_ml": 1.0, "temperature_c": 25.0,
    "salt_bridge_type": "KNO3", "salt_bridge_concentration_m": 0.1,
    "activity_correction_enabled": True, "liquid_junction_enabled": False,
    "errors_enabled": True,
    "operation_error_levels": {"pipetting": "low", "tip_carryover": "low", "mixing": "low"},
}
session = TransferSession(args.engine_root, conditions, executor="matterix", seed=17,
                          include_discard_tip=True, include_left_water=True,
                          include_paired_mix=True, include_left_electrode=True,
                          include_right_electrode=True, include_salt_bridge=True,
                          include_measurement=True)
args.output.mkdir(parents=True, exist_ok=True)
(args.output / "scene_check_conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
archive = session.archive()
archive["validation_scope"] = "initial_ledger_for_frozen_scene_no_actions_executed"
(args.output / "scene_check_initial_snapshot.json").write_text(json.dumps(archive, indent=2), encoding="utf-8")
print("SCENE_INPUTS_CREATED", str(args.output), "actions_executed=0")

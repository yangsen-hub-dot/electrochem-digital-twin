"""Run a numerical workflow example without connecting any device."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .simulator import load_config
from .workflow_runner import run_workflow


def main():
    parser = argparse.ArgumentParser(description="Local numerical workflow; no device control")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    result = run_workflow(load_config(args.config), seed=args.seed)
    event = next((step["event"] for step in reversed(result["steps"])
                  if step["event"].get("action") == "electrolyze" and step["status"] == "completed"), {})
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                      "completed_steps": sum(step["status"] == "completed" for step in result["steps"]),
                      "failure": result["failure"], "prediction": event.get("prediction"),
                      "conservation_scope": result["conservation_scope"]}, ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

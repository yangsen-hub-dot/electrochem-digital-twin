"""Persistent local records; no cloud access and no automatic overwrites."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

MODEL_ID = "hmf_tempo_fdca_endpoint_dargo2025_v1"
_ID = re.compile(r"^[0-9a-f]{32}$")


def save_run(directory, result, conditions, validation):
    run_id = result.get("run_id", "")
    if not _ID.fullmatch(run_id) or result.get("model_id") != MODEL_ID:
        raise ValueError("invalid HMF run")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{run_id}.json"
    if target.exists():
        raise FileExistsError("run already exists")
    payload = {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
               "model_id": MODEL_ID, "conditions": conditions, "validation": validation, "result": result}
    temporary = root / f".{run_id}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        # Atomic create without replacing an existing record, including races.
        os.link(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def load_run(directory, run_id):
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    with (Path(directory) / f"{run_id}.json").open(encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("model_id") != MODEL_ID or record.get("result", {}).get("run_id") != run_id:
        raise ValueError("record model or identity mismatch")
    return record


def list_runs(directory):
    root = Path(directory)
    if not root.exists():
        return []
    records = []
    for path in root.glob("*.json"):
        try:
            record = load_run(root, path.stem)
            result = record["result"]
            prediction = next((row["event"].get("prediction") for row in reversed(result.get("steps", []))
                               if row.get("event", {}).get("action") == "electrolyze" and row.get("status") == "completed"), None)
            records.append({"run_id": path.stem, "created_at_utc": record["created_at_utc"],
                            "status": result["status"], "conditions": record["conditions"], "prediction": prediction,
                            "source_run_id": (result.get("repair") or {}).get("source_run_id")})
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return sorted(records, key=lambda r: r["created_at_utc"], reverse=True)

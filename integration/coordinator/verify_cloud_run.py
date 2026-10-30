"""Explicit end-to-end API check: starts one real cloud process and waits."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

p = argparse.ArgumentParser()
p.add_argument("--node", default="http://127.0.0.1:50007")
p.add_argument("--media", default="http://127.0.0.1:50006")
p.add_argument("--access-code-file", type=Path, required=True)
p.add_argument("--conditions", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--timeout-s", type=float, default=240)
p.add_argument("--run-id", help="Inspect an existing run without starting another experiment")
a = p.parse_args()
token = a.access_code_file.read_text(encoding="utf-8").strip()
conditions = json.loads(a.conditions.read_text(encoding="utf-8"))
opener = build_opener(ProxyHandler({}))
request_id = uuid4().hex


def api(base, path, data=None):
    headers = {"Authorization": "Bearer " + token, "Origin": "null"}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Idempotency-Key": request_id})
    request = Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with opener.open(request, timeout=15) as response:
        return json.load(response)


if a.run_id:
    run_id = a.run_id
else:
    accepted = api(a.node, "/simulate", conditions)
    run_id = accepted["run_id"]
    repeated = api(a.node, "/simulate", conditions)
    assert repeated["run_id"] == run_id, "重复请求生成了第二次运行"
print("RUN_ACCEPTED=" + run_id, flush=True)
last = None
report = {"run_id": run_id, "request_id": request_id, "conditions": conditions,
          "idempotency_passed": None if a.run_id else True,
          "inspection_only": bool(a.run_id), "progress": []}
deadline = time.monotonic() + a.timeout_s
while time.monotonic() < deadline:
    try:
        state = api(a.node, "/state?run_id=" + run_id)
    except (OSError, TimeoutError) as exc:
        print('STATE_READ_RETRY=' + type(exc).__name__, flush=True)
        time.sleep(2)
        continue
    assert state["run_id"] == run_id
    voltage = state.get("voltage", {})
    if voltage.get("valid"):
        assert state["voltage_mv"] == voltage["virtual_meter_mv"]
        assert voltage["data_origin"] == "simulated" and voltage["measured_mv"] is None
    else:
        assert state["voltage_mv"] is None, "未完成有效测量时不应提前显示电压"
    key = (state["status"], state["completed_steps"])
    if key != last:
        last = key
        report["progress"].append({"status": key[0], "completed_steps": key[1], "timestamp": state["timestamp"]})
        print(f"PROGRESS={key[0]} steps={key[1]}/{state['total_steps']}", flush=True)
    if state["status"] in {"completed", "failed", "paused"}:
        break
    time.sleep(2)
else:
    raise RuntimeError("云端运行未在验收时间内完成；不会自动重新启动")
report["state"] = state
a.output.parent.mkdir(parents=True, exist_ok=True)
if state["status"] != "completed":
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise RuntimeError(state.get("failure") or state["status"])
assert state["conditions"]["left_concentration_m"] == conditions["left_concentration_m"]
assert state["conditions"]["left_volume_ml"] == conditions["left_volume_ml"]
assert state["conservation"]["passed"]
assert all(event["run_id"] == run_id for event in state["events"])
playback = api(a.media, f"/media/runs/{run_id}/playback")
assert playback["run_id"] == run_id
assert playback["conditions"]["left_volume_ml"] == conditions["left_volume_ml"]
with opener.open(a.media + playback["video_url"], timeout=20) as response:
    video = response.read()
report["video"] = {"run_id": playback["run_id"], "size_bytes": len(video),
                   "duration_s": playback["duration_s"], "sha256": hashlib.sha256(video).hexdigest(),
                   "same_run_passed": True}
a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"VERIFIED same_run=True conservation=True video_bytes={len(video)} duration_s={playback['duration_s']}", flush=True)

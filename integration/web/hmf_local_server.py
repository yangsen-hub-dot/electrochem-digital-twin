"""HMF numerical workflow service. Loopback by default; explicit authenticated hosting is optional."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "electrochemistry_simulation"))
sys.path.insert(0, str(ROOT / "integration"))
from electrochem_sim.simulator import load_config
from electrochem_sim.workflow_runner import run_workflow
from hmf_conditions import build_conditions
from hmf_history import save_run, load_run, list_runs
from hmf_validation import assess_run
from process_validation.repairs.hmf.planner import build_plan
from process_validation.repairs.hmf.execute import execute_repair

STATIC = {
    "/": (ROOT / "电化学浓差电池_轻量可视化原型.html", "text/html; charset=utf-8"),
    "/integration/web/lab_workspace.js": (ROOT / "integration/web/lab_workspace.js", "text/javascript; charset=utf-8"),
    "/integration/web/lab_workspace.css": (ROOT / "integration/web/lab_workspace.css", "text/css; charset=utf-8"),
    "/integration/web/hmf_live.js": (ROOT / "integration/web/hmf_live.js", "text/javascript; charset=utf-8"),
}
CONFIG = ROOT / "electrochemistry_simulation/config/hmf_fdca_single_cell_workflow.json"
_legacy_history = Path(r"D:\codex\HMF本地实验历史")
HISTORY = Path(os.environ.get("ELECTROCHEM_HMF_HISTORY", str(
    _legacy_history if os.name == "nt" and _legacy_history.exists() else ROOT / "var/hmf-history")))
REPAIR_LOCK = threading.Lock()


def compact_event(message):
    result = {k: v for k, v in message.items() if k not in ("state", "record")}
    state = message.get("state")
    record = message.get("record")
    if record is not None:
        result["step_status"] = record["status"]
        result["event"] = record["event"]
        state = record["after"]
    if state is not None:
        reactor = state.get("containers", {}).get("reactor", {})
        result["reactor"] = {"volume_l": reactor.get("volume_l"),
                             "concentrations_m": reactor.get("concentrations_m", {})}
        result["revision"] = state.get("state_ledger", {}).get("revision")
        result["certainty"] = state.get("state_ledger", {}).get("certainty", {}).get("chemical", {})
    return result


class Handler(BaseHTTPRequestHandler):
    def origin_allowed(self):
        origin = self.headers.get("Origin")
        own = f"http://127.0.0.1:{self.server.server_address[1]}"
        return origin is None or origin == own or origin in getattr(self.server, "allowed_origins", ())

    def end_headers(self):
        origin = self.headers.get("Origin")
        if origin is not None and self.origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def api_authorized(self):
        if not self.origin_allowed():
            self.send_error(403)
            return False
        token = getattr(self.server, "access_token", "")
        if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                             ("Bearer " + token).encode()):
            self.send_error(401)
            return False
        return True

    def do_OPTIONS(self):
        if not self.origin_allowed():
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            data = json.dumps({"status":"ok", "mode":"hmf_numeric", "models":["oxidation"],
                               "auth_required":bool(getattr(self.server, "access_token", ""))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/api/hmf/") and not self.api_authorized():
            return
        if path == "/api/hmf/history" or path.startswith("/api/hmf/history/"):
            try:
                directory = getattr(self.server, "history_dir", HISTORY)
                data = list_runs(directory) if path == "/api/hmf/history" else load_run(directory, path.rsplit("/", 1)[-1])
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (OSError, ValueError, TypeError, KeyError):
                self.send_error(404)
            return
        item = STATIC.get(path)
        site_root = getattr(self.server, "site_root", None)
        if site_root:
            root = Path(site_root).resolve()
            candidate = (root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            item = (candidate, mimetypes.guess_type(str(candidate))[0] or "application/octet-stream") \
                if candidate.is_relative_to(root) and candidate.is_file() else None
        if item is None:
            self.send_error(404)
            return
        path, content_type = item
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in {"/api/hmf/run", "/api/hmf/repair"}:
            self.send_error(404)
            return
        if not self.api_authorized():
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.send_error(415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 256:
                raise ValueError("invalid body size")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("invalid body")
            if self.path == "/api/hmf/repair":
                if set(data) != {"run_id"}:
                    raise ValueError("run_id required")
                self.repair_run(data['run_id'])
                return
            if set(data) - {"seed", "conditions"}:
                raise ValueError("unsupported fields")
            seed = data.get("seed", 0)
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
                raise ValueError("invalid seed")
            config, conditions = build_conditions(load_config(CONFIG), data.get("conditions", {}))
        except (ValueError, TypeError, OSError, json.JSONDecodeError):
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        connected = True
        def send(message):
            nonlocal connected
            if connected:
                try:
                    if message.get("type") == "started":
                        message["conditions"] = conditions
                    line = json.dumps(compact_event(message), ensure_ascii=False, allow_nan=False) + "\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    connected = False
        pending_finished = []
        def on_event(message):
            if message.get("type") == "finished":
                pending_finished.append(message)
            else:
                send(message)
        try:
            result = run_workflow(config, seed=seed, on_event=on_event)
            report = assess_run(result)
            try:
                save_run(getattr(self.server, "history_dir", HISTORY), result, conditions, report)
                saved, error = True, None
            except (OSError, ValueError, TypeError) as exc:
                saved, error = False, str(exc)
            finished = pending_finished[-1] if pending_finished else {"type": "finished", "run_id": result["run_id"], "status": result["status"]}
            send({**finished, "history_saved": saved, "history_error": error, "validation": report})
        except (ValueError, KeyError, TypeError) as exc:
            send({"type": "error", "reason": str(exc)})

    def repair_run(self, run_id):
        directory = getattr(self.server, "history_dir", HISTORY)
        try:
            with REPAIR_LOCK:
                source_record = load_run(directory, run_id)
                source = source_record['result']
                plan = build_plan(source)
                if not plan.get('eligible'):
                    raise ValueError(plan.get('reason', '不能局部修复'))
                for summary in list_runs(directory):
                    prior = load_run(directory, summary['run_id'])['result'].get('repair', {})
                    if prior.get('source_run_id') == run_id and prior.get('candidate_fingerprint') == plan['candidate_fingerprint']:
                        raise ValueError('该候选已执行，保留已有结果，不重复执行。')
                branch = execute_repair(source, plan)
                report = assess_run(branch)
                saved = save_run(directory, branch, source_record['conditions'], report)
            encoded = json.dumps(saved, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            encoded = json.dumps({'error':str(exc)}, ensure_ascii=False).encode('utf-8')
            self.send_response(409)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)


def main():
    parser = argparse.ArgumentParser(description="HMF numeric workflow viewer and optional authenticated API")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--history-dir", type=Path, default=HISTORY)
    parser.add_argument("--site-root", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--allowed-origin", action="append", default=[])
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else ""
    if args.token_file and len(token) < 24:
        parser.error("访问码至少需要 24 个字符")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and (not token or not args.allowed_origin):
        parser.error("对外监听时必须配置 token-file 和 allowed-origin")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.history_dir = args.history_dir
    server.site_root = args.site_root
    server.access_token = token
    server.allowed_origins = tuple(args.allowed_origin)
    print(f"http://{args.host}:{server.server_port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

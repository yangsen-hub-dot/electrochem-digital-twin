"""Read-only cloud recordings API with signed MP4 URLs and byte-range playback."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

RUN_ID = re.compile(r"^[a-f0-9]{32}$")


def recording_title(snapshot, manifest_status=None):
    """Name completed workflow scope, not the old first-transfer milestone."""
    if manifest_status == 'segment_diagnostic_ready':
        return '移液器量程切换局部视频 · P200 / P1000 放回与取用'
    plan=snapshot.get('plan',[])
    actions={step.get('action') for step in plan}
    if 'measure_voltage' in actions:
        return ('浓差电池完整实验 · 配液、组装与虚拟开路测量' if snapshot.get('full_experiment_completed')
                else '浓差电池虚拟流程 · 尚未确认全部完成')
    if 'connect_salt_bridge' in actions:return '浓差电池组装 · 双侧配液、电极与盐桥'
    if 'place_electrode' in actions:return '双侧配液、同步混匀与铜电极安装'
    if 'mix_pair' in actions:return '双侧溶液配制与同步磁力混匀'
    if any(s.get('source')=='water' or '_water_' in s.get('step_id','') for s in plan):
        return '左侧溶液配制 · 母液加入、换枪头与定量加水'
    return '母液转移 · 装枪头、吸液、分液'+('、弃头' if 'discard_tip' in actions else '')


class MediaStore:
    def __init__(self, root, token, ttl=3600):
        self.root = Path(root).resolve()
        if len(token) < 24:
            raise ValueError("视频访问码至少需要 24 个字符")
        self.token = token
        self.ttl = ttl

    def recording(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise FileNotFoundError("无效运行编号")
        folder = (self.root / run_id).resolve()
        if folder.parent != self.root:
            raise FileNotFoundError("运行路径超出录像目录")
        try:
            manifest = json.loads((folder / "video_manifest.json").read_text(encoding="utf-8"))
            archive = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            snapshot = archive["snapshot"]
            if manifest.get("run_id") != run_id or snapshot.get("run_id") != run_id:
                raise ValueError("录像与运行编号不一致")
            manifest_status = manifest.get("status")
            failed_recording=(manifest_status=='failed' and manifest.get('recording_finalized') is True
                and manifest.get('partial_recording') is True and manifest.get('frame_count',0)>0)
            if manifest_status not in {"ready", "segment_diagnostic_ready"} and not failed_recording:
                raise ValueError("录像尚未完成")
            filename = Path(manifest.get("video_path", "first_transfer.mp4")).name
            video = (folder / filename).resolve()
            if video.parent != folder or video.suffix.lower() != ".mp4" or not video.is_file():
                raise ValueError("录像文件不可用")
            entry = {
                "run_id": run_id, "title": recording_title(snapshot, manifest_status),
                "status": snapshot.get("status"), "scope": snapshot.get("scope"),
                "diagnostic_segment": manifest_status == "segment_diagnostic_ready",
                "created_at_utc": (archive.get("events") or [{}])[0].get("occurred_at_utc"),
                "duration_s": manifest.get("duration_s"), "frame_count": manifest.get("frame_count"),
                "model": manifest.get("model"), "conditions": snapshot.get("conditions", {}),
                'recording_scope': manifest.get('recording_scope'),
                'continuation_segment': bool(manifest.get('continuation')),
                'source_run_id': (manifest.get('continuation') or manifest.get('correction')
                    or manifest.get('diagnostic') or {}).get('source_run_id'),
                'full_workflow_video': manifest.get('full_workflow_video', manifest_status == 'ready'),
                "size_bytes": video.stat().st_size, "recorded_replay": True,
                'failed_recording':failed_recording,
                'initial_frame_only':failed_recording and manifest.get('frame_count',0)==1,
                'failure_reason':manifest.get('reason') if failed_recording else None,
            }
            if manifest.get('continuation'):
                entry['title'] = '修正后继续实验 · 检查点之后的动作录像'
            elif manifest.get('correction'):
                entry['title'] = '铜电极动作局部修正与复验'
            if (manifest.get('diagnostic') or {}).get('stop_after_step')=='ui_connect_right_lead':
                entry['title']='右引线抓取与连接 · 局部仿真'
            if failed_recording:
                entry.update(title='动作暂停 · 本次失败前的部分录像',full_workflow_video=False)
            return video, entry
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FileNotFoundError("找不到可播放的本次录像") from exc

    def recordings(self):
        entries = []
        for folder in self.root.iterdir():
            if not folder.is_dir() or not RUN_ID.fullmatch(folder.name):
                continue
            try:
                entries.append(self.recording(folder.name)[1])
            except FileNotFoundError:
                continue
        return sorted(entries, key=lambda e: e.get("created_at_utc") or "", reverse=True)

    def signature(self, path, expires):
        return hmac.new(self.token.encode(), f"{path}\n{expires}".encode(), hashlib.sha256).hexdigest()

    def playback(self, run_id):
        _, entry = self.recording(run_id)
        path = f"/media/runs/{run_id}/video.mp4"
        expires = int(time.time()) + self.ttl
        entry.update({"video_url": path + "?" + urlencode({"expires": expires, "sig": self.signature(path, expires)}),
                      "expires_at": expires})
        return entry

    def authorized_video(self, path, query):
        try:
            expires = int(query["expires"][0])
            signature = query["sig"][0]
            if expires <= time.time() or expires > time.time() + self.ttl + 5:
                return False
            return hmac.compare_digest(signature, self.signature(path, expires))
        except (KeyError, TypeError, ValueError, IndexError):
            return False


def byte_range(value, size):
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("不支持的 Range")
    left, right = match.groups()
    if not left:
        length = int(right)
        if length <= 0:
            raise ValueError("无效尾部范围")
        return max(0, size - length), size - 1, True
    start = int(left)
    end = min(int(right), size - 1) if right else size - 1
    if start >= size or end < start:
        raise ValueError("范围超出文件")
    return start, end, True


def create_server(host, port, store, allowed_origins=("null",)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log bearer codes or signed URL query parameters.
            print(f"{self.command} {urlsplit(self.path).path}", flush=True)

        def common_headers(self):
            origin = self.headers.get("Origin")
            if origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def json_response(self, code, value):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.common_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def do_OPTIONS(self):
            self.send_response(204)
            self.common_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Range")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if path == "/media/health":
                self.json_response(200, {"status": "ok", "service": "cloud-recordings", "read_only": True})
                return
            video_match = re.fullmatch(r"/media/runs/([a-f0-9]{32})/video\.mp4", path)
            if video_match:
                if not store.authorized_video(path, parse_qs(url.query)):
                    self.json_response(403, {"message": "播放地址无效或已过期，请重新选择录像"})
                    return
                try:
                    video, _ = store.recording(video_match[1])
                except FileNotFoundError as exc:
                    self.json_response(404, {"message": str(exc)})
                    return
                size = video.stat().st_size
                try:
                    start, end, partial = byte_range(self.headers.get("Range"), size)
                except ValueError:
                    self.send_response(416)
                    self.common_headers()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if partial else 200)
                self.common_headers()
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    with video.open("rb") as stream:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # seeking / closing a browser video cancels old requests
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + store.token):
                self.json_response(401, {"message": "请输入正确的视频访问码"})
                return
            if path == "/media/runs":
                self.json_response(200, {"recordings": store.recordings()})
                return
            match = re.fullmatch(r"/media/runs/([a-f0-9]{32})/playback", path)
            if match:
                try:
                    self.json_response(200, store.playback(match[1]))
                except FileNotFoundError as exc:
                    self.json_response(404, {"message": str(exc)})
                return
            self.json_response(404, {"message": "接口不存在"})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="读取云盘录像；不启动实验、不控制设备")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50006)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--allowed-origin", action="append", default=[])
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    args.token_file.parent.mkdir(parents=True, exist_ok=True)
    if not args.token_file.exists():
        fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
    token = args.token_file.read_text(encoding="utf-8").strip()
    store = MediaStore(args.root, token)
    server = create_server(args.host, args.port, store, tuple(args.allowed_origin or ["null"]))
    print(f"MEDIA_SERVER_READY host={args.host} port={server.server_port} root={store.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

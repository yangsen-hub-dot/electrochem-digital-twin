import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloud_video_server import MediaStore, byte_range, create_server, recording_title


class CloudVideoTests(unittest.TestCase):
    def test_recording_title_follows_complete_workflow_scope(self):
        s={'plan':[{'action':'dispense','step_id':'ui_left_water_dispense_01'},
                   {'action':'measure_voltage','step_id':'ui_measure_voltage'}],
           'full_experiment_completed':True}
        self.assertIn('完整实验',recording_title(s))
        self.assertIn('虚拟开路测量',recording_title(s))
        s['full_experiment_completed']=False
        self.assertIn('尚未确认',recording_title(s))
        self.assertNotIn('完整实验',recording_title(s))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_id = "a" * 32
        folder = self.root / self.run_id
        folder.mkdir()
        self.payload = b"0123456789-video-fixture"
        (folder / "first_transfer.mp4").write_bytes(self.payload)
        (folder / "video_manifest.json").write_text(json.dumps({
            "run_id": self.run_id, "status": "ready", "duration_s": 9.4,
            "video_path": "/cloud/" + self.run_id + "/first_transfer.mp4",
            "frame_count": 113, "model": "semantic_pipetting_proxy_v1",
        }), encoding="utf-8")
        (folder / "run.json").write_text(json.dumps({
            "snapshot": {"run_id": self.run_id, "status": "completed", "scope": "left_stock_transfer", "conditions": {}},
            "events": [{"occurred_at_utc": "2026-10-14T11:00:00+00:00"}],
        }), encoding="utf-8")
        self.token = "test-only-code-01234567890123456789"
        self.store = MediaStore(self.root, self.token)
        self.server = create_server("127.0.0.1", 0, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path, headers=None, method="GET"):
        return urlopen(Request(self.base + path, headers=headers or {}, method=method), timeout=5)

    def auth(self):
        return {"Authorization": "Bearer " + self.token, "Origin": "null"}

    def playback(self):
        with self.get(f"/media/runs/{self.run_id}/playback", self.auth()) as response:
            return json.load(response)["video_url"]

    def test_listing_requires_code_and_marks_recorded_replay(self):
        with self.assertRaises(HTTPError) as denied:
            self.get("/media/runs")
        self.assertEqual(denied.exception.code, 401)
        with self.get("/media/runs", self.auth()) as response:
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "null")
            recordings = json.load(response)["recordings"]
        self.assertEqual(len(recordings), 1)
        self.assertTrue(recordings[0]["recorded_replay"])
        self.assertEqual(recordings[0]["run_id"], self.run_id)

    def test_signed_video_full_head_and_range_playback(self):
        path = self.playback()
        with self.get(path) as response:
            self.assertEqual(response.read(), self.payload)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
        with self.get(path, method="HEAD") as response:
            self.assertEqual(int(response.headers["Content-Length"]), len(self.payload))
            self.assertEqual(response.read(), b"")
        with self.get(path, {"Range": "bytes=3-8"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.payload[3:9])
            self.assertEqual(response.headers["Content-Range"], f"bytes 3-8/{len(self.payload)}")

    def test_suffix_and_open_range(self):
        path = self.playback()
        for value, expected in (("bytes=-5", self.payload[-5:]), ("bytes=10-", self.payload[10:])):
            with self.subTest(value=value), self.get(path, {"Range": value}) as response:
                self.assertEqual(response.read(), expected)

    def test_invalid_range_and_unsigned_or_expired_link_rejected(self):
        path = self.playback()
        with self.assertRaises(HTTPError) as error:
            self.get(path, {"Range": "bytes=999-1000"})
        self.assertEqual(error.exception.code, 416)
        for bad in (f"/media/runs/{self.run_id}/video.mp4", path.replace("sig=", "sig=wrong")):
            with self.subTest(path=bad), self.assertRaises(HTTPError) as error:
                self.get(bad)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.store.authorized_video("/x", {"expires": [str(int(time.time()) - 1)], "sig": ["wrong"]}))

    def test_incomplete_or_mismatched_recording_is_not_listed(self):
        folder = self.root / self.run_id
        manifest_path = folder / "video_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for update in ({"status": "recording"}, {"run_id": "b" * 32}):
            manifest_path.write_text(json.dumps({**manifest, **update}), encoding="utf-8")
            self.assertEqual(self.store.recordings(), [])
        with self.assertRaises(FileNotFoundError):
            self.store.recording("../secrets")

    def test_preflight_allows_authorization_for_local_page(self):
        with self.get("/media/runs", {"Origin": "null", "Access-Control-Request-Headers": "authorization"}, "OPTIONS") as response:
            self.assertEqual(response.status, 204)
            self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "null")

    def test_multiple_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            byte_range("bytes=0-2,5-7", 10)

    def test_failed_partial_requires_closed_recording_and_is_labeled(self):
        folder=self.root/self.run_id
        path=folder/'video_manifest.json';manifest=json.loads(path.read_text())
        (folder/'first_transfer.partial.mp4').write_bytes(self.payload)
        manifest.update(status='failed',video_path='first_transfer.partial.mp4',
                        partial_recording=True,reason='position timeout')
        path.write_text(json.dumps(manifest))
        with self.assertRaises(FileNotFoundError): self.store.recording(self.run_id)
        manifest['recording_finalized']=True;path.write_text(json.dumps(manifest))
        entry=self.store.playback(self.run_id)
        self.assertTrue(entry['failed_recording'])
        self.assertFalse(entry['full_workflow_video'])
        with self.get(entry['video_url']) as response:
            self.assertEqual(response.read(),self.payload)


if __name__ == "__main__":
    unittest.main()

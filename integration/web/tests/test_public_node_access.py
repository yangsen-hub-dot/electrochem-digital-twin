"""Public frontend origin and bearer checks; no external servers or GPU jobs."""
import http.client
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hmf_local_server import Handler
from http.server import ThreadingHTTPServer


class PublicNodeAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.history_dir = self.temp.name
        self.server.access_token = "test-only-public-token-123456789"
        self.server.allowed_origins = ("https://yangsen-hub-dot.github.io",)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def request(self, method, path, token=None, origin="https://yangsen-hub-dot.github.io"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Origin": origin}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_preflight_allows_only_configured_origin(self):
        status, headers, _ = self.request("OPTIONS", "/api/hmf/run")
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.server.allowed_origins[0])
        self.assertIn("Authorization", headers["Access-Control-Allow-Headers"])
        status, headers, _ = self.request("OPTIONS", "/api/hmf/run", origin="https://unrelated.example")
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_history_requires_token_and_correct_origin(self):
        self.assertEqual(self.request("GET", "/api/hmf/history")[0], 401)
        status, _, body = self.request("GET", "/api/hmf/history", self.server.access_token)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"[]")
        self.assertEqual(self.request("GET", "/api/hmf/history", self.server.access_token, "https://unrelated.example")[0], 403)


if __name__ == "__main__":
    unittest.main()

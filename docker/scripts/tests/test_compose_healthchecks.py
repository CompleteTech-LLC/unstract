"""Exercise actual Compose probe commands against isolated HTTP responses.

Run with: uv run --no-project --with pyyaml python -m unittest discover \
    -s docker/scripts/tests -p test_compose_healthchecks.py
"""

import http.server
import pathlib
import subprocess
import sys
import threading
import unittest

import yaml

DOCKER = pathlib.Path(__file__).resolve().parents[2]


class Response(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


class ComposeHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = yaml.safe_load((DOCKER / "docker-compose.yaml").read_text())
        cls.essentials = yaml.safe_load(
            (DOCKER / "docker-compose-dev-essentials.yaml").read_text()
        )
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Response)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_probe(self, probe, old_port, status):
        Response.status = status
        args = [
            part.replace(str(old_port), str(self.server.server_port))
            for part in probe[1:]
        ]
        args = [part.replace("$$", "$") for part in args]
        if args[0] == "python":
            args[0] = sys.executable
        return subprocess.run(args, capture_output=True, timeout=10).returncode

    def test_pg_worker_rejects_http_failure(self):
        probe = self.main["services"]["worker-pg-executor"]["healthcheck"]["test"]
        self.assertEqual(self.run_probe(probe, 8090, 200), 0)
        self.assertNotEqual(self.run_probe(probe, 8090, 503), 0)

    def test_backend_allows_only_documented_statuses(self):
        probe = self.main["services"]["backend"]["healthcheck"]["test"]
        for status in (200, 401):
            self.assertEqual(self.run_probe(probe, 8000, status), 0)
        for status in (403, 404, 500):
            self.assertNotEqual(self.run_probe(probe, 8000, status), 0)

    def test_qdrant_requires_ready_status(self):
        probe = self.essentials["services"]["qdrant"]["healthcheck"]["test"]
        self.assertEqual(self.run_probe(probe, 6333, 200), 0)
        self.assertNotEqual(self.run_probe(probe, 6333, 503), 0)

    def test_pg_worker_rejects_closed_listener(self):
        probe = self.main["services"]["worker-pg-executor"]["healthcheck"]["test"]
        listener = http.server.HTTPServer(("127.0.0.1", 0), Response)
        port = listener.server_port
        listener.server_close()
        command = probe[-1].replace(":8090/", f":{port}/")
        result = subprocess.run(
            [sys.executable, "-c", command], capture_output=True, timeout=10
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

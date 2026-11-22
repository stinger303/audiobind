import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from audiobind import catalog
from audiobind.qbit import QbitClient


class TestOriginRedirects(unittest.TestCase):
    def setUp(self):
        self.hits = []
        self.target = self.serve()
        self.source = self.serve()
        catalog.cache_clear()
        self.addCleanup(catalog.cache_clear)
        sleeper = patch.object(catalog.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def serve(self):
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.hits.append((self.server.server_port, self.path))
                if self.path.startswith("/cross"):
                    location = case.target + "/ok"
                elif self.path.startswith("/same"):
                    location = "/ok"
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                    return
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_catalog_follows_only_same_origin(self):
        self.assertEqual(catalog._get_json(self.source + "/same"), {"ok": True})
        self.assertIsNone(catalog._get_json(self.source + "/cross"))
        self.assertEqual(len(self.hits), 3)

    def test_qbit_follows_only_same_origin(self):
        client = QbitClient(self.source, "user", "password")
        self.assertEqual(client._get("/same", {})[0], 200)
        self.assertEqual(client._get("/cross", {})[0], 302)
        self.assertEqual(len(self.hits), 3)

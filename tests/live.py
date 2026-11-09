"""A real web server on an ephemeral port, against throwaway library/output/state dirs."""

import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from audiobind import web
from audiobind.config import CONFIG
from audiobind.state import State


class LiveServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.lib = self.root / "library"
        self.out = self.root / "output"
        self.lib.mkdir()
        self.out.mkdir()
        self.state = State(self.root / "state")
        self.saved = (CONFIG.library_root, CONFIG.output_root, CONFIG.web_port)
        CONFIG.library_root, CONFIG.output_root = self.lib, self.out
        CONFIG.web_port = 0
        self.server = web.serve(self.state)
        self.base = "http://127.0.0.1:%d" % CONFIG.web_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        web.Handler.state = None
        CONFIG.library_root, CONFIG.output_root, CONFIG.web_port = self.saved
        self.state.close()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None, raw=False):
        """(status, body). JSON bodies are decoded unless ``raw``."""
        body = data if isinstance(data, bytes) or data is None else json.dumps(data).encode()
        hdrs = {"Content-Type": "application/json"} if headers is None else headers
        req = urllib.request.Request(self.base + path, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
            exc.close()
        if raw:
            return status, payload
        return status, json.loads(payload or b"{}")

    def post(self, payload, path="/api/unit"):
        return self.request(path, payload)

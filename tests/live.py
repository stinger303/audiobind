"""A real web server on an ephemeral port, against throwaway library/output/state dirs."""

import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from audiobind import auth as auth_mod
from audiobind import web
from audiobind.config import CONFIG
from audiobind.state import State

#: the account seed_account() creates (cheap PBKDF2 rounds: these tests are not about hashing)
TEST_USER = "tester"
TEST_PASSWORD = "test-password-123"


class _SessionCookie(urllib.request.BaseHandler):
    """Adds the signed-in session cookie to every urllib request that brings no credentials
    of its own (a test that sets Cookie or Authorization keeps exactly what it set)."""

    handler_order = 100

    def __init__(self, cookie: str):
        self.cookie = cookie

    def http_request(self, req):
        if not req.has_header("Cookie") and not req.has_header("Authorization"):
            req.add_unredirected_header("Cookie", self.cookie)
        return req


def seed_account(state, username: str = TEST_USER, password: str = TEST_PASSWORD) -> str:
    """Create the account and a signed-in session; returns the Cookie header value.

    Since 0.16.0-dev2 a fresh database sends every page to /setup, so a test that just wants
    to talk to the app signs in first, exactly like a browser that has finished setup."""
    if not state.has_credentials():
        state.create_credentials(username, auth_mod.hash_password(password, iterations=1000))
    sid = auth_mod.SESSIONS.create(state.credentials_version())
    return f"{auth_mod.COOKIE_NAME}={sid}"


def sign_in_urllib(state) -> str:
    """seed_account() + make plain urllib.request.urlopen() carry the cookie. Undo with
    sign_out_urllib() in tearDown."""
    cookie = seed_account(state)
    urllib.request.install_opener(urllib.request.build_opener(_SessionCookie(cookie)))
    return cookie


def sign_out_urllib() -> None:
    urllib.request.install_opener(None)


class LiveServerCase(unittest.TestCase):
    #: sign in with a seeded account (False: start from a fresh DB with nobody signed in)
    signed_in = True

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
        self.cookie = sign_in_urllib(self.state) if self.signed_in else ""

    def tearDown(self):
        sign_out_urllib()
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

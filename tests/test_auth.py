"""v0.15.1: optional AUTH_TOKEN login (cookie session + Bearer for the API)."""

import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import config as config_mod
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

TOKEN = "correct horse battery staple"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class UnitTests(unittest.TestCase):
    def test_token_matches_is_exact(self):
        self.assertTrue(auth_mod.token_matches("abc", "abc"))
        self.assertFalse(auth_mod.token_matches("abd", "abc"))
        self.assertFalse(auth_mod.token_matches("", ""))  # no token configured never matches

    def test_session_expires_after_ttl(self):
        now = [1000.0]
        sessions = auth_mod.Sessions(ttl=60, clock=lambda: now[0])
        sid = sessions.create()
        self.assertTrue(sessions.valid(sid))
        now[0] += 61
        self.assertFalse(sessions.valid(sid))
        self.assertFalse(sessions.valid("made-up"))

    def test_default_ttl_is_seven_days(self):
        self.assertEqual(auth_mod.SESSION_TTL, 7 * 24 * 3600)
        self.assertIn("Max-Age=604800", auth_mod.session_cookie("x"))

    def test_limiter_window_slides(self):
        now = [0.0]
        lim = auth_mod.FailLimiter(limit=5, window=600, clock=lambda: now[0])
        for _ in range(5):
            self.assertFalse(lim.blocked("1.2.3.4"))
            lim.record("1.2.3.4")
        self.assertTrue(lim.blocked("1.2.3.4"))
        self.assertFalse(lim.blocked("5.6.7.8"))
        self.assertGreater(lim.retry_after("1.2.3.4"), 0)
        now[0] = 601
        self.assertFalse(lim.blocked("1.2.3.4"))

    def test_cookie_value_parsing(self):
        self.assertEqual(auth_mod.cookie_value("a=1; audiobind_session=xyz; b=2"), "xyz")
        self.assertEqual(auth_mod.cookie_value("junk;;=;"), "")
        self.assertEqual(auth_mod.cookie_value(None), "")

    def test_env_token(self):
        with mock.patch.dict(os.environ, {"AUTH_TOKEN": " s3cret "}):
            self.assertEqual(config_mod.Config.from_env().auth_token, "s3cret")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTH_TOKEN", None)
            self.assertEqual(config_mod.Config.from_env().auth_token, "")


class AuthLiveTests(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.saved_token = CONFIG.auth_token
        auth_mod.SESSIONS.clear()
        auth_mod.LIMITER.clear()
        src = self.lib / "Author" / "Book"
        os.makedirs(src)
        (src / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="Author/Book", status="review"))

    def tearDown(self):
        CONFIG.auth_token = self.saved_token
        auth_mod.SESSIONS.clear()
        auth_mod.LIMITER.clear()
        CONTROL.resume()
        CONTROL.clear_scan()
        super().tearDown()

    def raw(self, path, data=None, headers=None):
        """(status, headers, body) without following redirects."""
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with OPENER.open(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            out = exc.code, exc.headers, exc.read()
            exc.close()
            return out

    def login(self, token=TOKEN):
        body = urllib.parse.urlencode({"token": token}).encode()
        return self.raw("/login", body, {"Content-Type": "application/x-www-form-urlencoded"})

    # -- auth off: unchanged ---------------------------------------------------------------

    def test_auth_off_everything_open(self):
        CONFIG.auth_token = ""
        self.assertEqual(self.raw("/")[0], 200)
        self.assertEqual(self.raw("/api/units")[0], 200)
        status, _ = self.post({"action": "pause"}, path="/api/control")
        self.assertEqual(status, 200)

    def test_auth_off_login_page_redirects_home(self):
        CONFIG.auth_token = ""
        status, headers, _ = self.raw("/login")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")

    def test_auth_off_no_signout_link(self):
        CONFIG.auth_token = ""
        self.assertNotIn(b"/logout", self.raw("/")[2])

    # -- auth on: refusals -----------------------------------------------------------------

    def test_page_without_cookie_redirects_to_login(self):
        CONFIG.auth_token = TOKEN
        status, headers, _ = self.raw("/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertEqual(self.raw("/settings")[0], 303)

    def test_api_without_credentials_is_401(self):
        CONFIG.auth_token = TOKEN
        status, headers, body = self.raw("/api/units")
        self.assertEqual(status, 401)
        self.assertNotIn(b"Author/Book", body)
        self.assertIn("Bearer", headers["WWW-Authenticate"])

    def test_post_without_credentials_is_refused(self):
        CONFIG.auth_token = TOKEN
        status, _ = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 401)
        self.assertTrue((self.lib / "Author" / "Book").exists())

    def test_bogus_cookie_is_refused(self):
        CONFIG.auth_token = TOKEN
        status, _, _ = self.raw("/api/state", headers={"Cookie": "audiobind_session=forged"})
        self.assertEqual(status, 401)

    def test_login_page_is_public_and_themed(self):
        CONFIG.auth_token = TOKEN
        status, _, body = self.raw("/login")
        self.assertEqual(status, 200)
        self.assertIn(b'type="password"', body)
        self.assertIn(b"--ab-bg", body)  # the app's dark theme
        self.assertEqual(self.raw("/favicon.svg")[0] in (200, 404), True)

    # -- login flow ------------------------------------------------------------------------

    def test_login_sets_strict_httponly_cookie_and_grants_access(self):
        CONFIG.auth_token = TOKEN
        status, headers, _ = self.login()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=604800", cookie)
        sid = auth_mod.cookie_value(cookie.split(";", 1)[0])
        self.assertTrue(sid)
        jar = {"Cookie": f"audiobind_session={sid}"}
        self.assertEqual(self.raw("/", headers=jar)[0], 200)
        self.assertIn(b"/logout", self.raw("/", headers=jar)[2])
        status, _, body = self.raw("/api/units", headers=jar)
        self.assertEqual(status, 200)
        self.assertIn(b"Author/Book", body)
        status, _, _ = self.raw(
            "/api/control", json.dumps({"action": "pause"}).encode(), {**jar, "Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(CONTROL.paused)

    def test_json_login(self):
        CONFIG.auth_token = TOKEN
        status, headers, body = self.raw(
            "/login", json.dumps({"token": TOKEN}).encode(), {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertIn("audiobind_session=", headers["Set-Cookie"])

    def test_wrong_token_is_401_and_sets_no_cookie(self):
        CONFIG.auth_token = TOKEN
        status, headers, body = self.login("nope")
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIn(b"not right", body)

    def test_logout_clears_cookie_and_session(self):
        CONFIG.auth_token = TOKEN
        _, headers, _ = self.login()
        sid = auth_mod.cookie_value(headers["Set-Cookie"].split(";", 1)[0])
        jar = {"Cookie": f"audiobind_session={sid}"}
        status, headers, _ = self.raw("/logout", headers=jar)
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.raw("/api/state", headers=jar)[0], 401)

    def test_bearer_token_on_api(self):
        CONFIG.auth_token = TOKEN
        status, _, body = self.raw("/api/units", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        status, _, _ = self.raw("/api/units", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_bearer_not_accepted_for_pages(self):
        CONFIG.auth_token = TOKEN
        self.assertEqual(self.raw("/", headers={"Authorization": f"Bearer {TOKEN}"})[0], 303)

    # -- rate limit ------------------------------------------------------------------------

    def test_rate_limit_trips_after_five_failures(self):
        CONFIG.auth_token = TOKEN
        for _ in range(5):
            self.assertEqual(self.login("wrong")[0], 401)
        status, headers, body = self.login()  # even the right token waits out the lock
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_wrong_bearer_tokens_count_towards_the_limit(self):
        CONFIG.auth_token = TOKEN
        for _ in range(5):
            self.raw("/api/units", headers={"Authorization": "Bearer guess"})
        status, _, _ = self.raw("/api/units", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 429)

    def test_success_resets_failure_count(self):
        CONFIG.auth_token = TOKEN
        for _ in range(4):
            self.login("wrong")
        self.assertEqual(self.login()[0], 303)
        for _ in range(4):
            self.assertEqual(self.login("wrong")[0], 401)

    def test_login_still_needs_allowed_host(self):
        CONFIG.auth_token = TOKEN
        body = urllib.parse.urlencode({"token": TOKEN}).encode()
        status, _, _ = self.raw(
            "/login", body, {"Content-Type": "application/x-www-form-urlencoded", "Host": "evil.example"}
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()

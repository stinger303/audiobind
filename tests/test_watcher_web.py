"""0.18.0-dev1: the watcher's UI - wishlist states, Retry, Settings > Watcher, /api/watcher."""

import http.client
import json
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import mam, series, watcher, web
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import State

try:
    from live import LiveServerCase, seed_account
except ImportError:
    from tests.live import LiveServerCase, seed_account


def add(state, asin, status="wanted", note="", title="A Book", checked=False):
    state.wishlist_add({"asin": asin, "title": title, "authors": ["A. Writer"], "origin": "search"})
    if status != "wanted":
        state.wishlist_set_status(asin, status, note=note, checked=checked)
    elif note or checked:
        state.wishlist_record_check(asin, note, miss=False)


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()
        watcher.WATCHER._paused = None

    def tearDown(self):
        self.schedule.stop()
        self.state.close()
        self.dir.cleanup()


class TestWishlistTab(StateCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(CONFIG, "mam_id", "x")  # these tests are about the MAM surfaces
        p.start()
        self.addCleanup(p.stop)

    def test_every_state_has_its_chip_and_retry_only_where_it_applies(self):
        add(self.state, "B000000001")
        add(self.state, "B000000002", "downloading", note="sent to qBittorrent")
        add(self.state, "B000000003", "in_library")
        add(self.state, "B000000004", "not_found", note="not found on MAM in 3 searches")
        add(self.state, "B000000005", "error", note="download of MAM #9 failed <boom>")
        html = web.render_discover(self.state, "wishlist")
        for label in ("WANTED", "DOWNLOADING", "IN LIBRARY", "NOT FOUND", "ERROR"):
            self.assertIn(f">{label}</span>", html)
        for status in ("wanted", "downloading", "in_library", "not_found", "error"):
            self.assertIn(f'class="ab-disc-pill ab-wl-{status}"', html)
        self.assertEqual(html.count('onclick="abRetry(this)">Retry</button>'), 2)
        self.assertEqual(html.count('onclick="abUnwant(this)">Remove</button>'), 5)
        self.assertIn("download of MAM #9 failed &lt;boom&gt;", html, "notes are escaped")
        self.assertIn("5 books wanted", html)
        self.assertIn("1 downloading &#183; 1 in library &#183; 1 not found &#183; 1 error", html)

    def test_last_checked(self):
        add(self.state, "B000000001")
        add(self.state, "B000000002", note="dry run", checked=True)
        html = web.render_discover(self.state, "wishlist")
        self.assertIn("never checked", html)
        self.assertIn("checked just now", html)

    def test_without_mam_the_wishlist_tab_is_gone(self):
        add(self.state, "B000000001")
        with mock.patch.object(CONFIG, "mam_id", ""):
            html = web.render_discover(self.state, "wishlist")
        self.assertNotIn("/discover/wishlist", html)
        self.assertNotIn("books wanted", html)

    def test_the_bar_warns_when_qbittorrent_is_not_configured(self):
        with mock.patch.object(CONFIG, "mam_id", "x"):
            html = web.render_discover(self.state, "wishlist")
        self.assertIn("The MAM watcher is disabled: QBIT_USER / QBIT_PASS are not set", html)

    def test_the_bar_when_on_paused_and_dry_run(self):
        with mock.patch.object(CONFIG, "mam_id", "x"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "p"), mock.patch.object(CONTROL, "_dry_run", True):
            html = web.render_discover(self.state, "wishlist")
            self.assertIn("checks up to <b>5</b> wanted books every <b>60 min</b>", html)
            self.assertIn("dry run: decisions are noted, nothing is downloaded", html)
            watcher.WATCHER.set_paused(self.state, True)
            self.assertIn("The MAM watcher is <b>paused</b>", web.render_discover(self.state, "wishlist"))

    def test_rail_keeps_the_wishlist_count(self):
        add(self.state, "B000000001")
        add(self.state, "B000000002", "downloading")
        self.assertIn('id="ab-rail-wanted" title="books on your wishlist">2</span>', web.render(self.state))


class TestSettingsSection(StateCase):
    def test_off(self):
        html = web.render_settings(self.state)
        self.assertIn('id="watcher"', html)
        self.assertIn("MAM acquisition (optional)", html)
        self.assertIn("<code>MAM_ID</code>", html)
        self.assertNotIn('id="ab-watch-run"', html, "no dead controls for a watcher that is off")
        self.assertNotIn('id="ab-watch-toggle"', html, "no pause switch for a watcher that is off")

    def test_missing_qbit_credentials_warn(self):
        with mock.patch.object(CONFIG, "mam_id", "x"):
            html = web.render_settings(self.state)
        self.assertIn('class="ab-hintbox ab-watch-warn" role="alert"><b>The watcher is disabled.</b>', html)
        self.assertIn("QBIT_USER / QBIT_PASS are not set", html)
        self.assertIn("no login set", html)

    def test_enabled_shows_status_last_cycle_and_controls(self):
        self.state.set_meta(watcher.META_LAST_CYCLE, {"at": time.time() - 7200, "trigger": "timer", "searched": 3,
                                                      "sent": 1, "not_found": 2, "errors": 0})
        self.state.set_meta(watcher.META_LAST_ERROR, {"at": time.time() - 60, "message": "MAM answered HTTP 503"})
        with mock.patch.object(CONFIG, "mam_id", "SECRET-MAM-COOKIE"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "SECRET-QBIT-PASS"):
            html = web.render_settings(self.state)
            self.assertIn('<span class="ok">Enabled</span>', html)
            self.assertIn("every 60 min, at most 5 books per cycle", html)
            self.assertIn("2h ago (timer) &#8212; 3 searched, 1 sent to qBittorrent, 2 not found", html)
            self.assertIn("MAM answered HTTP 503", html)
            self.assertIn('id="ab-watch-run" onclick="abWatcher(\'run\')" title=', html, "run is enabled")
            self.assertIn("Pause watcher", html)
            watcher.WATCHER.set_paused(self.state, True)
            paused = web.render_settings(self.state)
        for page in (html, paused):
            self.assertNotIn("SECRET-MAM-COOKIE", page)
            self.assertNotIn("SECRET-QBIT-PASS", page)
        self.assertIn("Resume watcher", paused)
        self.assertIn('<span class="warn">Paused</span>', paused)

    def test_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            for name, script in (("watcher.js", web.WATCHER_SCRIPT), ("discover.js", web.DISCOVER_SCRIPT)):
                js = pathlib.Path(tmp) / name
                js.write_text(script)
                result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


class TestWatcherApi(LiveServerCase):
    def setUp(self):
        super().setUp()
        watcher.WATCHER._paused = None

    def tearDown(self):
        worker = watcher.WATCHER._worker
        if worker is not None:
            worker.join(5)
        watcher.WATCHER._client = None
        watcher.WATCHER._paused = None
        super().tearDown()

    def test_status_never_carries_a_secret(self):
        with mock.patch.object(CONFIG, "mam_id", "SECRET-MAM-COOKIE"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "SECRET-QBIT-PASS"):
            status, raw = self.request("/api/watcher", headers={}, raw=True)
        self.assertEqual(status, 200)
        self.assertNotIn(b"SECRET-MAM-COOKIE", raw)
        self.assertNotIn(b"SECRET-QBIT-PASS", raw)
        body = json.loads(raw)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["max_per_cycle"], 5)

    def test_run_refused_while_off(self):
        status, body = self.request("/api/watcher", {"action": "run"})
        self.assertEqual(status, 409)
        self.assertIn("MAM_ID", body["error"])

    def test_pause_and_resume_persist(self):
        status, body = self.request("/api/watcher", {"action": "pause"})
        self.assertEqual((status, body["paused"]), (200, True))
        self.assertTrue(self.state.get_meta(watcher.META_PAUSED))
        status, body = self.request("/api/watcher", {"action": "resume"})
        self.assertEqual((status, body["paused"]), (200, False))
        self.assertFalse(self.state.get_meta(watcher.META_PAUSED))
        status, _ = self.request("/api/watcher", {"action": "explode"})
        self.assertEqual(status, 400)

    def test_run_a_cycle_now(self):
        add(self.state, "B000000001")
        transport_calls = []

        def transport(method, url, data, headers, timeout):
            transport_calls.append(url)
            return 200, {}, b'{"error": "Nothing returned, out of 0"}'

        client = mam.MamClient("cookie", transport=transport, limiter=mam.RateLimiter(spacing=0))
        with mock.patch.object(CONFIG, "mam_id", "cookie"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "p"), mock.patch.object(watcher, "make_mam", return_value=client):
            status, body = self.request("/api/watcher", {"action": "run"})
            self.assertEqual(status, 200, body)
            watcher.WATCHER._worker.join(5)
        self.assertEqual(transport_calls, [mam.SEARCH_URL])
        self.assertIn("not found on MAM", self.state.wishlist_get("B000000001")["note"])
        self.assertEqual(self.state.get_meta(watcher.META_LAST_CYCLE)["trigger"], "manual")

    def test_retry(self):
        add(self.state, "B000000001", "not_found", note="x")
        add(self.state, "B000000002", "downloading")
        status, body = self.request("/api/wishlist/retry", {"asin": "B000000001"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["item"]["status"], "wanted")
        self.assertIn("retry requested", body["item"]["note"])
        status, body = self.request("/api/wishlist/retry", {"asin": "B000000002"})
        self.assertEqual(status, 409)
        self.assertIn("downloading", body["error"])
        self.assertEqual(self.request("/api/wishlist/retry", {"asin": "B0MISSING1"})[0], 404)
        self.assertEqual(self.request("/api/wishlist/retry", {"asin": "../x"})[0], 400)

    def test_wishlist_api_lists_the_watcher_fields(self):
        add(self.state, "B000000001", "error", note="boom")
        _, listing = self.request("/api/wishlist", headers={})
        item = listing["items"][0]
        self.assertEqual((item["status"], item["note"]), ("error", "boom"))
        for key in ("checked_at", "updated_at", "mam_tid", "torrent_hash"):
            self.assertIn(key, item)

    def test_posts_have_the_cross_site_guard(self):
        for path, body in (("/api/watcher", {"action": "pause"}), ("/api/wishlist/retry", {"asin": "B000000001"})):
            status, _ = self.request(path, json.dumps(body).encode(), headers={"Content-Type": "text/plain"})
            self.assertEqual(status, 415, path)
            status, _ = self.request(path, body, headers={"Content-Type": "application/json",
                                                          "Origin": "http://evil.example"})
            self.assertEqual(status, 403, path)
        self.assertIsNone(self.state.get_meta(watcher.META_PAUSED))


class TestWatcherRoutesNeedSignIn(LiveServerCase):
    signed_in = False

    def setUp(self):
        super().setUp()
        self.state.create_credentials("owner", auth_mod.hash_password("owner-pass-123", iterations=1000))

    def raw(self, method, path, body=None, cookie=""):
        conn = http.client.HTTPConnection("127.0.0.1", CONFIG.web_port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def test_signed_out_is_refused(self):
        for route in ("/api/watcher", "/api/wishlist/retry"):
            self.assertNotIn(route, web.Handler.AUTH_EXEMPT)
        self.assertEqual(self.raw("GET", "/api/watcher"), 401)
        self.assertEqual(self.raw("POST", "/api/watcher", {"action": "pause"}), 401)
        self.assertEqual(self.raw("POST", "/api/wishlist/retry", {"asin": "B000000001"}), 401)
        self.assertIsNone(self.state.get_meta(watcher.META_PAUSED))
        cookie = seed_account(self.state)
        self.assertEqual(self.raw("GET", "/api/watcher", cookie=cookie), 200)


if __name__ == "__main__":
    unittest.main()

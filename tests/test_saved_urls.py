"""SEC-05c: a client URL *saved* in the State DB (a legacy or malformed value the Settings validator
never saw) with a login in it is used without the login, logged once as such, and the login never
reaches a page, an API answer, the status view, a probe error or a log line."""

import http.server
import json
import logging
import threading
import unittest

from audiobind import config, watcher, web
from audiobind.util import redact_url

try:
    from test_watcher_gui_settings import WatcherSettingsCase
except ImportError:
    from tests.test_watcher_gui_settings import WatcherSettingsCase

USER = "legacyuser"
SECRET = "s3cret-legacy-pw"
URL_KEYS = ("qbit_url", "deluge_url", "transmission_url", "rtorrent_url", "tracker_url")


class _Recorder(http.server.BaseHTTPRequestHandler):
    """A stand-in qBittorrent: records every request and refuses the login."""

    def do_POST(self):  # noqa: N802
        self.server.seen.append((self.path, dict(self.headers)))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b"Fails."
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SavedUrlCase(WatcherSettingsCase):
    def setUp(self):
        super().setUp()
        logged = getattr(config, "_LOGIN_LOGGED", set())
        logged.clear()  # "once per value" is per process: each test starts from none logged
        self.addCleanup(logged.clear)

    def store(self, **values):
        """Write raw values straight into the DB, the way a legacy install would hold them."""
        self.state.set_meta_many({f"setting_{key}": value for key, value in values.items()})
        config.set_effective(self.state)

    def assertClean(self, text, where):  # noqa: N802
        self.assertNotIn(SECRET, text, where)
        self.assertNotIn(USER, text, where)


class StrippedOnReadTests(SavedUrlCase):
    def test_every_url_setting_is_read_without_its_login(self):
        raw = {key: f"http://{USER}:{SECRET}@host:8080" for key in URL_KEYS}
        with self.assertLogs("audiobind", logging.WARNING) as logs:
            self.store(**raw)
        for key in URL_KEYS:
            self.assertEqual(config.setting_effective(key), "http://host:8080", key)
            self.assertEqual(config.effective_val(config.SETTING_MAP[key]), "http://host:8080", key)
        out = "\n".join(logs.output)
        self.assertClean(out, "log")
        for key in URL_KEYS:
            self.assertEqual(sum(key in line for line in logs.output), 1, key)  # one line per value

    def test_logged_once_per_value(self):
        with self.assertLogs("audiobind", logging.WARNING):
            self.store(qbit_url=f"http://{USER}:{SECRET}@host:8080")
        with self.assertNoLogs("audiobind", logging.WARNING):
            config.set_effective(self.state)
            config.set_effective(self.state)
        with self.assertLogs("audiobind", logging.WARNING) as logs:  # a different value logs again
            self.store(qbit_url=f"http://{USER}:{SECRET}@other:8080")
        self.assertEqual(len(logs.output), 1)

    def test_a_password_with_slash_or_at_is_removed_whole(self):
        for raw, want in ((f"http://{USER}:p/{SECRET}@host:8080", "http://host:8080"),
                          (f"http://{USER}:p@{SECRET}@host:8080/transmission/rpc",
                           "http://host:8080/transmission/rpc"),
                          (f"{USER}:{SECRET}@host:8080", "host:8080")):
            with self.subTest(raw=raw), self.assertLogs("audiobind", logging.WARNING):
                self.store(transmission_url=raw)
                got = config.setting_effective("transmission_url")
                self.assertEqual(got, want)

    def test_a_well_formed_url_is_unchanged(self):
        with self.assertNoLogs("audiobind", logging.WARNING):
            self.store(transmission_url="https://tm.lan/transmission/rpc", rtorrent_url="scgi://rt:5000")
        self.assertEqual(config.setting_effective("transmission_url"), "https://tm.lan/transmission/rpc")
        self.assertEqual(config.setting_effective("rtorrent_url"), "scgi://rt:5000")
        self.assertEqual(config.setting_effective("qbit_url"), "http://env-qbit:8080")  # the env default
        self.assertEqual(config.effective_val("qbit_url"), "http://env-qbit:8080")


class NeverShownTests(SavedUrlCase):
    def setUp(self):
        super().setUp()
        self.store(qbit_url=f"http://{USER}:{SECRET}@host:8080")  # the log line: StrippedOnReadTests

    def test_not_in_api_settings(self):
        # the settings JSON is the snapshot's "settings" (GET /api/state); /api/settings is POST-only
        status, _h, body = self.call("/api/state", method="GET")
        self.assertEqual(status, 200)
        self.assertClean(body, "/api/state")
        self.assertEqual(json.loads(body)["settings"]["qbit_url"], "http://host:8080")
        for payload in ({"settings": {"qbit_category": "books"}}, {"reset_keys": ["qbit_category"]},
                        {"reset": True}):
            status, _h, body = self.call("/api/settings", payload)
            self.assertEqual(status, 200, payload)
            self.assertClean(body, f"/api/settings {payload}")

    def test_not_in_the_watcher_settings_html(self):
        page = self.page()
        self.assertClean(page, "/settings")
        self.assertIn('value="http://host:8080"', self.field("qbit_url", page))
        status, _h, body = self.call("/settings", method="GET")
        self.assertClean(body, "/settings (served)")

    def test_not_in_the_status_view(self):
        self.assertClean(web.render_watcher_section(self.state), "status view")
        self.assertClean(json.dumps(watcher.WATCHER.status(self.state)), "/api/watcher")
        status, _h, body = self.call("/api/watcher", method="GET")
        self.assertClean(body, "/api/watcher (served)")


class PublicSettingsTests(unittest.TestCase):
    def test_a_url_with_a_login_is_masked(self):
        for raw in (f"http://{USER}:{SECRET}@host:8080", f"{USER}:{SECRET}@host", f"http://a@{USER}:{SECRET}@h",
                    f"http://{USER}:p/{SECRET}@host"):
            out = web.public_settings({"qbit_url": raw, "deluge_url": raw})
            self.assertClean(json.dumps(out), raw)

    def test_a_clean_url_is_unchanged(self):
        out = web.public_settings({"qbit_url": "http://qbit:8080", "tracker_url": "https://t.example"})
        self.assertEqual(out, {"qbit_url": "http://qbit:8080", "tracker_url": "https://t.example"})

    assertClean = SavedUrlCase.assertClean  # noqa: N815


class ClientTests(SavedUrlCase):
    def test_the_client_connects_to_the_bare_host_and_the_probe_never_echoes(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        logger = logging.getLogger("audiobind")
        with self.assertLogs(logger, logging.DEBUG) as logs:
            self.store(qbit_url=f"http://{USER}:{SECRET}@127.0.0.1:{port}")
            self.assertEqual(watcher.make_client().url, f"http://127.0.0.1:{port}")
            status, _h, body = self.call("/api/watcher/test-qbit", {})
            logger.debug("end")
        self.assertEqual(status, 502)
        self.assertClean(body, "probe answer")
        self.assertTrue(server.seen, "the client reached host:port")
        self.assertTrue(all(path.startswith("/api/v2/") for path, _hdr in server.seen))
        self.assertClean(json.dumps(server.seen), "what the client sent")
        self.assertClean("\n".join(logs.output), "log")

    def test_an_unreachable_saved_url_error_never_echoes(self):
        self.store(qbit_url=f"http://{USER}:{SECRET}@127.0.0.1:9")
        status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 502)
        self.assertClean(body, "probe answer")
        self.assertIn("127.0.0.1:9", body)
        with self.assertRaises(watcher._QbitDown) as caught:
            watcher.WATCHER._qbit({})
        self.assertClean(str(caught.exception), "watcher cycle error")

    def test_qbit_client_errors_never_echo_a_login(self):
        from audiobind.qbit import QbitClient, QbitError

        for url in (f"{USER}:{SECRET}://qbit", f"{USER}:{SECRET}@qbit:8080"):
            with self.assertRaises(QbitError, msg=url) as caught:
                QbitClient(url, "", "")
            self.assertClean(str(caught.exception), url)
        client = QbitClient(f"http://{USER}:{SECRET}@127.0.0.1:9", "", "")
        with self.assertRaises(QbitError) as caught:
            client.login()
        self.assertClean(str(caught.exception), "unreachable")


class RedactTests(unittest.TestCase):
    def test_malformed_input_gets_a_fixed_placeholder(self):
        for raw in (f"{USER}:{SECRET}@qbit", f"{USER}:{SECRET}", f"http://a@{USER}:{SECRET}@h",
                    f"{USER}:{SECRET}://qbit", f"http//{USER}:{SECRET}@x"):
            self.assertEqual(redact_url(raw), "<url hidden>", raw)

    def test_well_formed_input(self):
        self.assertEqual(redact_url(f"http://{USER}:{SECRET}@qbit:8080/x"), "http://***@qbit:8080/x")
        self.assertEqual(redact_url(f"http://{USER}:p/{SECRET}@qbit"), "http://***@qbit")
        self.assertEqual(redact_url("http://qbit:8080"), "http://qbit:8080")
        self.assertEqual(redact_url("scgi://rt:5000"), "scgi://rt:5000")
        self.assertEqual(redact_url(""), "")


if __name__ == "__main__":
    unittest.main()

"""SEC-05: a download client's URL from the environment goes through the Settings validator.

A URL with a login in it (user:pass@) is not used, and the login never reaches a page, an
API answer, an error or a log line - only *** in its place."""

import os
import unittest
from unittest import mock

from audiobind import config
from audiobind.clients import base, transport
from audiobind.config import CONFIG, Config

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

SECRET = "hunter2-secret"
ENV_URLS = {
    "QBIT_URL": ("qbit_url", f"http://admin:{SECRET}@qbit:8080"),
    "DELUGE_URL": ("deluge_url", f"http://admin:{SECRET}@deluge:8112"),
    "TRANSMISSION_URL": ("transmission_url", f"http://admin:{SECRET}@transmission:9091/transmission/rpc"),
    "RTORRENT_URL": ("rtorrent_url", f"http://admin:{SECRET}@rutorrent/RPC2"),
}


def from_env(env: dict) -> Config:
    clean = {k: v for k, v in os.environ.items() if k not in ENV_URLS}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
        return Config.from_env()


class EnvUrlTests(unittest.TestCase):
    def test_a_url_with_a_login_is_not_used(self):
        for name, (attr, url) in ENV_URLS.items():
            cfg = from_env({name: url})
            self.assertEqual(getattr(cfg, attr), "", name)

    def test_the_refusal_is_logged_redacted(self):
        for name, (_attr, url) in ENV_URLS.items():
            lines = config.rejected_env_lines(from_env({name: url}))
            self.assertEqual(len(lines), 1, name)
            self.assertIn(name, lines[0])
            self.assertIn("no login in it", lines[0])  # the validation error says why
            self.assertNotIn(SECRET, lines[0])
            self.assertNotIn("admin", lines[0])

    def test_a_malformed_url_with_a_login_never_reaches_the_log(self):
        # SEC-05b: the redactor guesses where the login is; for a malformed value it can guess
        # wrong, so the rejected value is left out of the line altogether
        for name in ENV_URLS:
            for url in (f"admin:{SECRET}://qbit", f"http://admin:{SECRET}", f"http//admin:{SECRET}@x"):
                lines = config.rejected_env_lines(from_env({name: url}))
                self.assertEqual(len(lines), 1, (name, url))
                self.assertIn(name, lines[0])
                self.assertNotIn(SECRET, lines[0], (name, url))

    def test_the_daemon_puts_the_lines_in_the_log(self):
        from audiobind import daemon

        self.assertIs(daemon.rejected_env_lines, config.rejected_env_lines)

    def test_valid_urls_are_unchanged(self):
        cfg = from_env({"QBIT_URL": "http://qbit:8080/", "DELUGE_URL": "http://deluge:8112",
                        "TRANSMISSION_URL": "https://tm.lan/transmission/rpc", "RTORRENT_URL": "scgi://rt:5000"})
        self.assertEqual((cfg.qbit_url, cfg.deluge_url, cfg.transmission_url, cfg.rtorrent_url),
                         ("http://qbit:8080", "http://deluge:8112", "https://tm.lan/transmission/rpc", "scgi://rt:5000"))
        self.assertEqual(config.rejected_env_lines(cfg), [])

    def test_settings_path_is_unchanged(self):
        check = config.WATCHER_TEXT_CHECKS["qbit_url"][1]
        self.assertEqual(check(" http://qbit:8080/ "), "http://qbit:8080")
        with self.assertRaises(ValueError) as caught:
            check(f"http://admin:{SECRET}@qbit:8080")
        self.assertNotIn(SECRET, str(caught.exception))
        for key in ("deluge_url", "transmission_url", "rtorrent_url"):
            with self.assertRaises(ValueError):
                config.WATCHER_TEXT_CHECKS[key][1](f"http://admin:{SECRET}@host:1")


class RedactTests(unittest.TestCase):
    def test_redact_url(self):
        from audiobind.util import redact_url

        cases = {
            f"http://admin:{SECRET}@qbit:8080/x": "http://***@qbit:8080/x",
            f"admin:{SECRET}@qbit": "***@qbit",
            f"http://admin:p/{SECRET}@qbit": "http://***@qbit",
            "http://qbit:8080": "http://qbit:8080",
            "": "",
        }
        for url, want in cases.items():
            self.assertEqual(redact_url(url), want, url)

    def test_transport_rejection_does_not_echo_the_login(self):
        with self.assertRaises(base.ClientError) as caught:
            transport.http_url(f"http://admin:{SECRET}@tm:9091", "TRANSMISSION_URL")
        message = str(caught.exception)
        self.assertIn("TRANSMISSION_URL", message)
        self.assertNotIn(SECRET, message)
        self.assertIn("***@tm:9091", message)

    def test_rtorrent_constructor_does_not_echo_the_login(self):
        from audiobind.clients.rtorrent import RTorrentClient

        for url in (f"http://admin:{SECRET}@rutorrent/RPC2", f"scgi://admin:{SECRET}@rt:5000",
                    f"scgi://admin:{SECRET}", f"admin:{SECRET}://rt"):
            with self.assertRaises(base.ClientError, msg=url) as caught:
                RTorrentClient(url)
            self.assertIn("RTORRENT_URL", str(caught.exception), url)
            self.assertNotIn(SECRET, str(caught.exception), url)


class EnvUrlPageTests(LiveServerCase):
    """What an env-supplied QBIT_URL with a login looks like from the browser's side."""

    def setUp(self):
        super().setUp()
        self.saved_url = CONFIG.qbit_url
        CONFIG.qbit_url = from_env({"QBIT_URL": ENV_URLS["QBIT_URL"][1]}).qbit_url

    def tearDown(self):
        CONFIG.qbit_url = self.saved_url
        super().tearDown()

    def test_never_in_a_page_or_an_api_answer(self):
        for path in ("/settings", "/api/watcher", "/api/settings"):
            status, body = self.request(path, raw=True)
            self.assertNotIn(SECRET.encode(), body, path)


if __name__ == "__main__":
    unittest.main()

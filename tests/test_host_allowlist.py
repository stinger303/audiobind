"""v0.15.1: the Host allowlist (DNS-rebinding defence) on every request."""

import json
import logging
import os
import socket
import unittest
from unittest import mock

from audiobind import config as config_mod
from audiobind import web
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class HostNameTests(unittest.TestCase):
    def test_port_and_case_are_ignored(self):
        self.assertEqual(web.host_name("Tower.Local:8686"), "tower.local")
        self.assertEqual(web.host_name("192.168.1.5:80"), "192.168.1.5")
        self.assertEqual(web.host_name("localhost"), "localhost")

    def test_ipv6_forms(self):
        self.assertEqual(web.host_name("[::1]:8080"), "::1")
        self.assertEqual(web.host_name("[fe80::1]"), "fe80::1")
        self.assertEqual(web.host_name("::1"), "::1")

    def test_trailing_dot_is_dropped(self):
        self.assertEqual(web.host_name("tower.local.:8080"), "tower.local")


class ConfigTests(unittest.TestCase):
    def test_default_is_localhost_only(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALLOWED_HOSTS", None)
            os.environ.pop("TRUSTED_PROXY", None)
            cfg = config_mod.Config.from_env()
        self.assertEqual(cfg.allowed_hosts, ["localhost", "127.0.0.1"])
        self.assertFalse(cfg.allowed_hosts_set)
        self.assertFalse(cfg.trusted_proxy)

    def test_env_list_is_split_trimmed_and_lowercased(self):
        env = {"ALLOWED_HOSTS": " Tower.local , 192.168.1.10,, ", "TRUSTED_PROXY": "true"}
        with mock.patch.dict(os.environ, env):
            cfg = config_mod.Config.from_env()
        self.assertEqual(cfg.allowed_hosts, ["tower.local", "192.168.1.10"])
        self.assertTrue(cfg.allowed_hosts_set)
        self.assertTrue(cfg.trusted_proxy)


class StartupWarningTests(unittest.TestCase):
    def setUp(self):
        self.saved = (CONFIG.allowed_hosts_set, web._HOSTS_WARNED)

    def tearDown(self):
        CONFIG.allowed_hosts_set, web._HOSTS_WARNED = self.saved

    def test_unset_allowed_hosts_warns_once(self):
        CONFIG.allowed_hosts_set, web._HOSTS_WARNED = False, False
        with self.assertLogs("audiobind", level="WARNING") as logs:
            web.warn_default_hosts()
            web.warn_default_hosts()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("ALLOWED_HOSTS", logs.output[0])
        self.assertIn("unreachable from other machines", logs.output[0])

    def test_set_allowed_hosts_is_silent(self):
        CONFIG.allowed_hosts_set, web._HOSTS_WARNED = True, False
        logger = logging.getLogger("audiobind")
        with mock.patch.object(logger, "warning") as warn:
            web.warn_default_hosts()
        warn.assert_not_called()


class HostGuardLiveTests(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.port = CONFIG.web_port
        self.saved_hosts = (CONFIG.allowed_hosts, CONFIG.trusted_proxy)
        src = self.lib / "Author" / "Book"
        os.makedirs(src)
        (src / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="Author/Book", status="review"))

    def tearDown(self):
        CONFIG.allowed_hosts, CONFIG.trusted_proxy = self.saved_hosts
        CONTROL.resume()
        CONTROL.clear_scan()
        super().tearDown()

    def get(self, path, host):
        return self.request(path, None, {"Host": host}, raw=True)

    def test_default_allows_localhost_and_loopback(self):
        self.assertEqual(self.get("/api/state", f"127.0.0.1:{self.port}")[0], 200)
        self.assertEqual(self.get("/api/state", f"localhost:{self.port}")[0], 200)

    def test_rebound_hostname_get_is_refused(self):
        status, body = self.get("/api/units", f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertIn(b"ALLOWED_HOSTS", body)
        self.assertNotIn(b"Author/Book", body)

    def test_rebound_hostname_page_is_refused(self):
        self.assertEqual(self.get("/", "evil.example")[0], 403)

    def test_rebound_post_with_matching_origin_is_refused(self):
        # the exact rebinding shape: Origin and Host agree, but the host is not ours
        host = f"evil.example:{self.port}"
        status, _ = self.request(
            "/api/unit",
            json.dumps({"action": "delete", "path": "Author/Book", "confirm": True}).encode(),
            {"Content-Type": "application/json", "Host": host, "Origin": "http://" + host},
            raw=True,
        )
        self.assertEqual(status, 403)
        self.assertTrue((self.lib / "Author" / "Book").exists())
        self.assertIsNotNone(self.state.get("Author/Book"))

    def test_configured_lan_name_is_allowed(self):
        CONFIG.allowed_hosts = ["tower.local", "192.168.1.10"]
        self.assertEqual(self.get("/api/state", f"Tower.Local:{self.port}")[0], 200)
        self.assertEqual(self.get("/api/state", "192.168.1.10:8686")[0], 200)
        self.assertEqual(self.get("/api/state", f"127.0.0.1:{self.port}")[0], 403)

    def test_forwarded_host_ignored_without_trusted_proxy(self):
        CONFIG.allowed_hosts = ["books.example.net"]
        status, _ = self.request(
            "/api/state", None, {"Host": f"127.0.0.1:{self.port}", "X-Forwarded-Host": "books.example.net"}, raw=True
        )
        self.assertEqual(status, 403)

    def test_forwarded_host_honoured_with_trusted_proxy(self):
        CONFIG.allowed_hosts, CONFIG.trusted_proxy = ["books.example.net"], True
        status, _ = self.request(
            "/api/state", None, {"Host": "backend:8080", "X-Forwarded-Host": "books.example.net"}, raw=True
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            "/api/state", None, {"Host": "backend:8080", "X-Forwarded-Host": "evil.example"}, raw=True
        )
        self.assertEqual(status, 403)

    def test_forwarded_origin_not_trusted_without_proxy_setting(self):
        # Origin naming the forwarded host must not pass the POST guard unless TRUSTED_PROXY
        CONFIG.allowed_hosts = ["127.0.0.1", "books.example.net"]
        status, _ = self.request(
            "/api/control",
            json.dumps({"action": "pause"}).encode(),
            {
                "Content-Type": "application/json",
                "Origin": "https://books.example.net",
                "X-Forwarded-Host": "books.example.net",
            },
            raw=True,
        )
        self.assertEqual(status, 403)
        self.assertFalse(CONTROL.paused)

    def _raw(self, request_bytes):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data

    def test_http10_without_host_is_refused(self):
        reply = self._raw(b"GET /api/units HTTP/1.0\r\n\r\n")
        self.assertTrue(reply.split(b"\r\n", 1)[0].endswith(b"403 Forbidden"), reply[:80])
        self.assertNotIn(b"Author/Book", reply)

    def test_post_without_host_is_refused(self):
        body = json.dumps({"action": "pause"}).encode()
        reply = self._raw(
            b"POST /api/control HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        self.assertIn(b" 403 ", reply.split(b"\r\n", 1)[0])
        self.assertFalse(CONTROL.paused)


if __name__ == "__main__":
    unittest.main()

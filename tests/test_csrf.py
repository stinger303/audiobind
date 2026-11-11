"""v0.14.4: POST endpoints refuse cross-site requests.

A page on another site could fire ``fetch(url, {method:'POST', mode:'no-cors', body:...})``
with ``text/plain`` - no preflight - and delete books or flip controls.
"""

import json
import os
import unittest

from audiobind.control import CONTROL
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class TestPostGuard(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.host = self.base.split("://", 1)[1]
        src = self.lib / "Author" / "Book"
        os.makedirs(src)
        (src / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="Author/Book", status="review"))
        self.delete = json.dumps({"action": "delete", "path": "Author/Book", "confirm": True}).encode()

    def tearDown(self):
        CONTROL.resume()
        CONTROL.clear_scan()
        super().tearDown()

    def test_text_plain_post_is_refused_with_415(self):
        status, body = self.request("/api/unit", self.delete, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertTrue((self.lib / "Author" / "Book").exists())
        self.assertIsNotNone(self.state.get("Author/Book"))

    def test_default_form_content_type_is_refused_with_415(self):
        # urllib fills in application/x-www-form-urlencoded when no type is given
        status, _ = self.request("/api/control", json.dumps({"action": "pause"}).encode(), {})
        self.assertEqual(status, 415)
        self.assertFalse(CONTROL.paused)

    def test_form_post_to_settings_is_refused(self):
        status, _ = self.request(
            "/api/settings", b"reset=1", {"Content-Type": "application/x-www-form-urlencoded"}
        )
        self.assertEqual(status, 415)

    def test_foreign_origin_is_refused_with_403(self):
        status, body = self.request(
            "/api/unit", self.delete, {"Content-Type": "application/json", "Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertTrue((self.lib / "Author" / "Book").exists())

    def test_foreign_referer_is_refused_with_403(self):
        status, _ = self.request(
            "/api/control",
            json.dumps({"action": "pause"}).encode(),
            {"Content-Type": "application/json", "Referer": "http://evil.example/page"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(CONTROL.paused)

    def test_null_origin_is_refused(self):
        status, _ = self.request(
            "/api/control",
            json.dumps({"action": "pause"}).encode(),
            {"Content-Type": "application/json", "Origin": "null"},
        )
        self.assertEqual(status, 403)

    def test_same_origin_post_is_allowed(self):
        status, body = self.request(
            "/api/control",
            json.dumps({"action": "pause"}).encode(),
            {"Content-Type": "application/json; charset=utf-8", "Origin": "http://" + self.host},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(CONTROL.paused)

    def test_same_origin_referer_is_allowed(self):
        status, _ = self.request(
            "/api/control",
            json.dumps({"action": "scan"}).encode(),
            {"Content-Type": "application/json", "Referer": f"http://{self.host}/?status=review"},
        )
        self.assertEqual(status, 200)

    def test_reverse_proxy_forwarded_host_is_allowed(self):
        # v0.15.1: X-Forwarded-Host is only believed with TRUSTED_PROXY on (and its host allowed)
        from audiobind.config import CONFIG

        saved = (CONFIG.trusted_proxy, CONFIG.allowed_hosts)
        CONFIG.trusted_proxy, CONFIG.allowed_hosts = True, ["127.0.0.1", "books.example.net"]
        self.addCleanup(setattr, CONFIG, "trusted_proxy", saved[0])
        self.addCleanup(setattr, CONFIG, "allowed_hosts", saved[1])
        status, _ = self.request(
            "/api/control",
            json.dumps({"action": "scan"}).encode(),
            {
                "Content-Type": "application/json",
                "Origin": "https://books.example.net",
                "X-Forwarded-Host": "books.example.net",
            },
        )
        self.assertEqual(status, 200)

    def test_curl_style_post_without_origin_is_allowed(self):
        status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertFalse((self.lib / "Author" / "Book").exists())


if __name__ == "__main__":
    unittest.main()

"""v0.15.1 low/info bundle: security headers, bounded JSON bodies, no outbound traffic
from GETs, byte-capped path components, escaped confidence, reserved "ingest:" keys."""

import json
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock

from audiobind import daemon, library, series, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState
from audiobind.units import discover_units, find_unit
from audiobind.util import safe_name, truncate_utf8

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class LiveLows(LiveServerCase):
    def raw(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", CONFIG.web_port), timeout=10) as sock:
            sock.sendall(request)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return data

    def head(self, request: bytes) -> tuple[str, dict]:
        reply = self.raw(request)
        lines = reply.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        return lines[0], dict(line.split(": ", 1) for line in lines[1:] if ": " in line)

    def host(self) -> bytes:
        # Host plus the signed-in session (0.16.0-dev2: a fresh DB would otherwise answer /setup)
        return f"Host: 127.0.0.1:{CONFIG.web_port}\r\nCookie: {self.cookie}\r\n".encode()

    # -- headers -----------------------------------------------------------------------

    def test_security_headers_on_every_kind_of_response(self):
        requests = [
            b"GET / HTTP/1.0\r\n" + self.host() + b"\r\n",
            b"GET /api/state HTTP/1.0\r\n" + self.host() + b"\r\n",
            b"GET /cover?asin=B000000000 HTTP/1.0\r\n" + self.host() + b"\r\n",  # 404
            b"GET / HTTP/1.0\r\nHost: evil.example\r\n\r\n",  # 403 host refusal
            b"PUT / HTTP/1.0\r\n" + self.host() + b"\r\n",  # BaseHTTPRequestHandler's own 501
        ]
        for request in requests:
            with self.subTest(request=request.split(b"\r\n", 1)[0]):
                _, headers = self.head(request)
                for name, value in HEADERS.items():
                    self.assertEqual(headers.get(name), value)

    def test_handler_timeout(self):
        self.assertEqual(web.Handler.timeout, 15)

    # -- bounded / typed JSON bodies -----------------------------------------------------

    def post_raw(self, path: str, body: bytes, length: str | None = None) -> str:
        length = str(len(body)) if length is None else length
        status, _ = self.head(
            f"POST {path} HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n".encode()
            + self.host()
            + b"\r\n"
            + body
        )
        return status.split(" ")[1]

    def test_negative_content_length_is_413(self):
        self.assertEqual(self.post_raw("/api/control", b"", "-1"), "413")

    def test_huge_content_length_is_413(self):
        self.assertEqual(self.post_raw("/api/unit", b"{}", str(70 * 1024)), "413")

    def test_garbage_content_length_is_400(self):
        self.assertEqual(self.post_raw("/api/settings", b"{}", "lots"), "400")

    def test_non_object_bodies_are_400_on_every_endpoint(self):
        for path in ("/api/unit", "/api/settings", "/api/series", "/api/control"):
            for body in (b"[]", b'"x"', b"3", b"null"):
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.post_raw(path, body), "400")

    def test_bad_json_is_400(self):
        self.assertEqual(self.post_raw("/api/control", b"{nope"), "400")

    def test_body_at_the_limit_is_accepted(self):
        body = json.dumps({"action": "scan", "pad": "x" * (web.MAX_BODY - 40)}).encode()
        self.assertLessEqual(len(body), web.MAX_BODY)
        self.assertEqual(self.post_raw("/api/control", body), "200")
        from audiobind.control import CONTROL

        CONTROL.clear_scan()

    # -- GETs never go outbound ------------------------------------------------------------

    def test_series_gets_do_not_schedule_refreshes(self):
        with mock.patch.object(series, "aggregate", wraps=series.aggregate) as agg:
            for path in ("/api/series", "/series", "/", "/settings"):
                self.assertEqual(self.request(path, None, {}, raw=True)[0], 200)
        self.assertTrue(agg.call_args_list)  # the pages do read the shelf...
        self.assertFalse(any(call.kwargs.get("refresh", True) for call in agg.call_args_list))  # ...cache only

    def test_post_refresh_schedules(self):
        with mock.patch.object(series, "aggregate", wraps=series.aggregate) as agg:
            status, body = self.post({"action": "refresh"}, path="/api/series")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(any(call.kwargs.get("refresh") for call in agg.call_args_list))

    # -- delete errors never echo raw OSError text ----------------------------------------

    def test_output_removal_error_is_generic(self):
        published = self.out / "Author" / "Book" / "Book.m4b"
        published.parent.mkdir(parents=True)
        published.write_bytes(b"m4b")
        self.state.save(UnitState(path="Gone/Book", status="converted", output=str(published)))
        boom = PermissionError(13, "Permission denied", str(published.parent))
        with mock.patch.object(web.shutil, "rmtree", side_effect=boom):
            status, body = self.post({"action": "delete", "path": "Gone/Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["output"].startswith("failed (EACCES)"))
        self.assertNotIn(str(self.out), json.dumps(body))

    # -- reserved "ingest:" keys -----------------------------------------------------------

    def test_library_folder_named_ingest_is_rekeyed_and_deletable(self):
        folder = self.lib / "ingest:Some Book"
        folder.mkdir()
        (folder / "01.mp3").write_bytes(b"audio")
        units = discover_units(self.lib, [])
        self.assertEqual([u.rel_path for u in units], ["./ingest:Some Book"])
        self.assertIsNotNone(find_unit(self.lib, [], "./ingest:Some Book"))
        self.state.save(UnitState(path="./ingest:Some Book", status="review"))
        status, body = self.post({"action": "delete", "path": "./ingest:Some Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertFalse(folder.exists())


class SafeNameBytesTests(unittest.TestCase):
    def test_ascii_capped_at_200_bytes(self):
        self.assertEqual(len(safe_name("a" * 500).encode()), 200)

    def test_multibyte_never_split(self):
        for ch in ("é", "日", "😀"):
            with self.subTest(ch=ch):
                out = safe_name(ch * 300)
                raw = out.encode("utf-8")
                self.assertLessEqual(len(raw), 200)
                self.assertEqual(out, ch * len(out))  # whole characters only
                self.assertGreater(len(raw), 200 - len(ch.encode()))

    def test_short_names_unchanged(self):
        self.assertEqual(safe_name("Dune: Part 1"), "Dune- Part 1")
        self.assertEqual(truncate_utf8("abc", 10), "abc")

    def test_truncation_does_not_leave_trailing_dot_or_space(self):
        out = safe_name("x" * 199 + " .tail")
        self.assertFalse(out.endswith((" ", ".")))

    def test_long_unicode_title_fits_name_max(self):
        saved = CONFIG.output_root
        with tempfile.TemporaryDirectory() as tmp:
            CONFIG.output_root = pathlib.Path(tmp)
            try:
                title = "長い題名" * 80  # 960 bytes of UTF-8
                path = library.m4b_path("Series " * 60, ["作者" * 100], title, "3")
                for part in path.relative_to(tmp).parts:
                    self.assertLessEqual(len(part.encode()), 255, part)
                path.parent.mkdir(parents=True)
                path.write_bytes(b"ok")  # the filesystem accepts every component
            finally:
                CONFIG.output_root = saved


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_confidence_is_escaped(self):
        self.state.save(UnitState(path="A/B", status="review", title="B", confidence="<i>X</i>"))
        with mock.patch.object(series.REFRESHER, "schedule", return_value=False):
            page = web.render(self.state)
        self.assertIn("Match confidence: <b>&lt;i&gt;X&lt;/i&gt;</b>", page)

    def test_daemon_refresh_survives_errors(self):
        with mock.patch.object(series, "aggregate", side_effect=RuntimeError("boom")):
            with self.assertLogs("audiobind", level="ERROR"):
                daemon.refresh_series(self.state)


if __name__ == "__main__":
    unittest.main()

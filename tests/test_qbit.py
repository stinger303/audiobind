"""0.18.0-dev1: the qBittorrent push client, against a local fake of the 5.x WebUI API."""

import email.parser
import email.policy
import hashlib
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from audiobind import qbit

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://t.myanonamouse.net/ann4:info" + INFO + b"e"


class FakeQbit(BaseHTTPRequestHandler):
    """qBittorrent 5.x as far as the client cares: 204 empty login + SID, Referer CSRF check."""

    server_version = "FakeQbit"
    USER, PASSWORD = "admin", "s3cret pass"

    def log_message(self, *args):
        return

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        record = {"path": self.path, "headers": dict(self.headers), "body": body}
        self.server.calls.append(record)
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Referer") != origin:
            self._reply(403, b"Forbidden")  # CSRF protection
            return
        if self.path == "/api/v2/auth/login":
            form = urllib.parse.parse_qs(body.decode())
            if form.get("username") == [self.USER] and form.get("password") == [self.PASSWORD]:
                self._reply(204, b"", {"Set-Cookie": "SID=abc123; HttpOnly; path=/"})
            else:
                self._reply(200, b"Fails.")
            return
        if "SID=abc123" not in (self.headers.get("Cookie") or ""):
            self._reply(403, b"Forbidden")
            return
        if self.path == "/api/v2/torrents/add":
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            parts = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                parts[name] = (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
            record["parts"] = parts
            self._reply(200, self.server.add_answer)
            return
        if self.path == "/api/v2/auth/logout":
            self._reply(200)
            return
        self._reply(404)


class QbitCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeQbit)
        self.server.calls = []
        self.server.add_answer = b"Ok."
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def client(self, password=FakeQbit.PASSWORD):
        return qbit.QbitClient(self.url + "/", FakeQbit.USER, password)


class TestLogin(QbitCase):
    def test_login_sends_referer_and_keeps_the_sid_cookie(self):
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)
        call = self.server.calls[0]
        self.assertEqual(call["path"], "/api/v2/auth/login")
        self.assertEqual(call["headers"]["Referer"], self.url)
        self.assertEqual(urllib.parse.parse_qs(call["body"].decode()),
                         {"username": ["admin"], "password": ["s3cret pass"]})
        self.assertTrue(any(ck.name == "SID" for ck in c.jar))

    def test_bad_password_is_an_error_that_never_shows_the_password(self):
        c = self.client(password="wrong-password-xyz")
        with self.assertRaises(qbit.QbitError) as ctx:
            c.login()
        self.assertNotIn("wrong-password-xyz", str(ctx.exception))
        self.assertNotIn("wrong-password-xyz", repr(c))
        self.assertFalse(c.logged_in)

    def test_missing_credentials_never_send_anything(self):
        with self.assertRaises(qbit.QbitError):
            qbit.QbitClient(self.url, "", "").login()
        self.assertEqual(self.server.calls, [])

    def test_unreachable(self):
        c = qbit.QbitClient("http://127.0.0.1:1", "a", "b")
        with self.assertRaises(qbit.QbitError) as ctx:
            c.login()
        self.assertIn("could not be reached", str(ctx.exception))

    def test_bad_url(self):
        with self.assertRaises(qbit.QbitError):
            qbit.QbitClient("ftp://x", "a", "b")


class TestAdd(QbitCase):
    def test_add_posts_the_torrent_with_the_right_form_fields(self):
        c = self.client()
        c.login()
        digest = c.add_torrent(TORRENT, "1001.torrent")
        call = self.server.calls[-1]
        self.assertEqual(call["path"], "/api/v2/torrents/add")
        self.assertEqual(call["headers"]["Referer"], self.url)
        self.assertIn("SID=abc123", call["headers"]["Cookie"])
        parts = call["parts"]
        self.assertEqual(parts["torrents"], ("1001.torrent", "application/x-bittorrent", TORRENT))
        self.assertEqual(parts["category"][2], b"mam-audiobooks")
        self.assertEqual(parts["paused"][2], b"false")
        self.assertEqual(parts["stopped"][2], b"false")
        self.assertNotIn("autoTMM", parts, "no automatic-torrent-management override")
        self.assertNotIn("savepath", parts, "the category decides where it lands")
        self.assertEqual(digest, hashlib.sha1(INFO).hexdigest())

    def test_add_logs_in_first_when_needed(self):
        self.client().add_torrent(TORRENT)
        self.assertEqual([c["path"] for c in self.server.calls], ["/api/v2/auth/login", "/api/v2/torrents/add"])

    def test_fails_is_an_error(self):
        self.server.add_answer = b"Fails."
        with self.assertRaises(qbit.QbitError) as ctx:
            self.client().add_torrent(TORRENT)
        self.assertIn("refused", str(ctx.exception))

    def test_a_hash_in_the_answer_wins(self):
        self.server.add_answer = b'{"added_torrent_ids": ["' + b"C" * 40 + b'"]}'
        self.assertEqual(self.client().add_torrent(TORRENT), "c" * 40)

    def test_logout_forgets_the_session(self):
        c = self.client()
        c.login()
        c.logout()
        self.assertFalse(c.logged_in)
        self.assertEqual(len(list(c.jar)), 0)
        self.assertEqual(self.server.calls[-1]["path"], "/api/v2/auth/logout")


class TestInfoHash(unittest.TestCase):
    def test_v1_info_hash(self):
        self.assertEqual(qbit.info_hash(TORRENT), hashlib.sha1(INFO).hexdigest())

    def test_garbage_is_empty(self):
        for junk in (b"", b"<html>", b"d4:info", b"d3:abci1e", b"d4:infod" * 5000):
            self.assertEqual(qbit.info_hash(junk), "")


if __name__ == "__main__":
    unittest.main()

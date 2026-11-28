"""0.18.0-dev1: the qBittorrent push client, against a local fake of the 5.x WebUI API."""

import email.parser
import email.policy
import hashlib
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from audiobind import qbit

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:info" + INFO + b"e"


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
                status, cookie = self.server.login_reply
                self._reply(status, b"", {"Set-Cookie": cookie} if cookie else {})
            else:
                self._reply(200, b"Fails.")
            return
        if "=abc123" not in (self.headers.get("Cookie") or ""):
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
        self.server.login_reply = (204, "SID=abc123; HttpOnly; path=/")
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

    def test_qbt_sid_port_cookie_is_a_success(self):
        self.server.login_reply = (204, "QBT_SID_8083=abc123; HttpOnly; SameSite=Lax; path=/")
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)
        self.assertTrue(any(ck.name == "QBT_SID_8083" for ck in c.jar))
        c.add_torrent(TORRENT)  # the session works for the next call
        self.assertIn("QBT_SID_8083=abc123", self.server.calls[-1]["headers"]["Cookie"])

    def test_204_without_a_cookie_is_a_failure(self):
        self.server.login_reply = (204, None)
        with self.assertRaises(qbit.QbitError):
            self.client().login()

    def test_200_with_sid_still_works(self):
        self.server.login_reply = (200, "SID=abc123; path=/")
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)

    def test_empty_cookie_value_is_a_failure(self):
        self.server.login_reply = (204, "QBT_SID_8083=; path=/")
        with self.assertRaises(qbit.QbitError):
            self.client().login()

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
        digest = c.add_torrent(TORRENT, "1001.torrent", "my-category")
        call = self.server.calls[-1]
        self.assertEqual(call["path"], "/api/v2/torrents/add")
        self.assertEqual(call["headers"]["Referer"], self.url)
        self.assertIn("SID=abc123", call["headers"]["Cookie"])
        parts = call["parts"]
        self.assertEqual(parts["torrents"], ("1001.torrent", "application/x-bittorrent", TORRENT))
        self.assertEqual(parts["category"][2], b"my-category")
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


class TestProgress(QbitCase):
    def test_progress_is_one_get_for_the_whole_batch(self):
        h1, h2, gone = "a" * 40, "b" * 40, "c" * 40
        listing = json.dumps([{"hash": h1, "progress": 1.0}, {"hash": h2, "progress": 0.4}]).encode()
        gets = []
        orig = FakeQbit.do_GET if hasattr(FakeQbit, "do_GET") else None

        def do_GET(handler):  # noqa: N802
            gets.append((handler.path, dict(handler.headers)))
            if "=abc123" not in (handler.headers.get("Cookie") or ""):
                handler._reply(403, b"Forbidden")
            else:
                handler._reply(200, listing)

        FakeQbit.do_GET = do_GET
        self.addCleanup(lambda: setattr(FakeQbit, "do_GET", orig) if orig else delattr(FakeQbit, "do_GET"))
        out = self.client().progress([h1, h2, gone])
        self.assertEqual(out, {h1: 1.0, h2: 0.4})
        self.assertEqual(len(gets), 1)
        path, headers = gets[0]
        self.assertTrue(path.startswith("/api/v2/torrents/info?hashes="))
        self.assertEqual(urllib.parse.unquote(path.split("hashes=")[1]), "|".join([h1, h2, gone]))
        self.assertEqual(headers["Referer"], self.url)

    def test_no_hashes_sends_nothing(self):
        self.assertEqual(self.client().progress(["", None]), {})
        self.assertEqual(self.server.calls, [])

    def test_unreachable_and_refused_are_qbit_errors(self):
        with self.assertRaises(qbit.QbitError):
            qbit.QbitClient("http://127.0.0.1:1", "a", "b").progress(["a" * 40])
        with self.assertRaises(qbit.QbitError):  # no do_GET on the fake: 501
            self.client().progress(["a" * 40])


class TestIncomplete(QbitCase):
    def test_incomplete_counts_the_category_below_the_threshold_in_one_get(self):
        listing = json.dumps([{"progress": 1.0}, {"progress": 0.4}, {"progress": 0.9995}, {"progress": 0.0},
                              {"progress": None}, "junk"]).encode()
        gets = []

        def do_GET(handler):  # noqa: N802
            gets.append(handler.path)
            handler._reply(200, listing)

        FakeQbit.do_GET = do_GET
        self.addCleanup(lambda: delattr(FakeQbit, "do_GET"))
        self.assertEqual(self.client().incomplete("my cat", 0.999), 2)
        self.assertEqual(gets, ["/api/v2/torrents/info?category=my+cat"])


def _torrent(n):
    """One torrent dict shaped like qBittorrent 5.x's /torrents/info entry (~2 KB of JSON)."""
    return {
        "hash": f"{n:040x}", "name": f"Author {n} - A Long Audiobook Title Volume {n} (Unabridged)",
        "progress": 1.0 if n % 3 else 0.5, "ratio": n / 10, "category": "mam-audiobooks",
        "save_path": "/downloads/mam-audiobooks", "content_path": f"/downloads/mam-audiobooks/Book {n}",
        "tracker": "https://tracker.example/announce.php?passkey=" + "x" * 32, "tags": "",
        "force_start": False, "has_metadata": True,
        "magnet_uri": "magnet:?xt=urn:btih:" + "f" * 40 + "&dn=" + "y" * 900,
        "comment": "z" * 400, "size": 123456789, "state": "stalledUP", "added_on": 1764000000 + n,
    }


class TestLargeAnswer(QbitCase):
    """QBT-TRUNC: a torrent list over 64 KiB was cut off and then reported as "unreadable"."""

    def serve(self, body):
        def do_GET(handler):  # noqa: N802
            try:
                handler._reply(200, body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client stopped reading at its ceiling

        FakeQbit.do_GET = do_GET
        self.addCleanup(lambda: delattr(FakeQbit, "do_GET"))

    def test_a_120_kb_category_listing_is_read_whole(self):
        items = [_torrent(n) for n in range(60)]
        body = json.dumps(items).encode()
        self.assertGreater(len(body), 110 * 1024)
        self.serve(body)
        state = self.client().snatch_state("mam-audiobooks")
        self.assertEqual(set(state), {item["hash"] for item in items})
        self.assertEqual(state[f"{7:040x}"], (1.0, 0.7))
        self.assertEqual(self.client().incomplete("mam-audiobooks", 0.999), 20)

    def test_an_answer_over_the_ceiling_says_it_was_cut_off(self):
        ceiling = 8 * 1024 * 1024
        self.serve(b"[" + b" " * ceiling + b"]")
        with self.assertRaises(qbit.QbitError) as ctx:
            self.client().snatch_state("mam-audiobooks")
        message = str(ctx.exception)
        self.assertNotIn("unreadable", message)
        self.assertIn("cut off", message)
        self.assertIn("8 MiB", message)
        self.assertEqual(ctx.exception.kind, "error")

    def test_an_answer_exactly_at_the_ceiling_is_complete(self):
        ceiling = 8 * 1024 * 1024
        body = b"[" + b" " * (ceiling - 2) + b"]"
        self.assertEqual(len(body), ceiling)
        self.serve(body)
        self.assertEqual(self.client().snatch_state("mam-audiobooks"), {})

    def test_a_body_that_is_not_json_is_still_unreadable(self):
        self.serve(b"<html>")
        with self.assertRaises(qbit.QbitError) as ctx:
            self.client().snatch_state("mam-audiobooks")
        self.assertIn("answered the torrent list with something unreadable", str(ctx.exception))

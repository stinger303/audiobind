"""The Deluge adapter against a fake Web UI JSON-RPC transport (no network, no real Deluge)."""

import base64
import hashlib
import json
import unittest

from audiobind.clients import base
from audiobind.clients.deluge import DelugeClient

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO).hexdigest()


class FakeDeluge:
    """Deluge 2.x Web UI JSON-RPC as far as the adapter cares. ``client._send`` is replaced by
    ``self.send``; every call is recorded as (method, params)."""

    PASSWORD = "deluge pw"

    def __init__(self):
        self.calls = []
        self.authed = False
        self.connected = True
        self.hosts = [["h1", "127.0.0.1", 58846, "Online"]]
        self.plugins = ["Label"]
        self.labels = []
        self.torrents = {}  # hash -> {"progress": %, "ratio": r, "label": l}
        self.status = 200
        self.add_error = None
        self.raw = None  # a raw answer body that replaces the next reply

    def send(self, body):
        request = json.loads(body)
        method, params = request["method"], request["params"]
        self.calls.append((method, params))
        if self.raw is not None:
            return self.status, self.raw
        if self.status != 200:
            return self.status, b""

        def ok(result):
            return 200, json.dumps({"result": result, "error": None, "id": request["id"]}).encode()

        def err(message, code=4):
            return 200, json.dumps({"result": None, "error": {"message": message, "code": code},
                                    "id": request["id"]}).encode()

        if method == "auth.login":
            self.authed = params == [self.PASSWORD]
            return ok(self.authed)
        if not self.authed:
            return err("Not authenticated", 1)
        if method == "auth.delete_session":
            self.authed = False
            return ok(True)
        if method == "web.connected":
            return ok(self.connected)
        if method == "web.get_hosts":
            return ok(self.hosts)
        if method == "web.connect":
            self.connected = bool(self.hosts)
            return ok(None)
        if method == "core.get_enabled_plugins":
            return ok(self.plugins)
        if method == "label.get_labels":
            return ok(self.labels) if "Label" in self.plugins else err("Unknown method")
        if method == "label.add":
            self.labels.append(params[0])
            return ok(None)
        if method == "label.set_torrent":
            self.torrents[params[0]]["label"] = params[1]
            return ok(None)
        if method == "core.add_torrent_file":
            if self.add_error:
                return err(self.add_error)
            data = base64.b64decode(params[1])
            digest = hashlib.sha1(INFO).hexdigest() if data == TORRENT else None
            if digest is None:
                return err("Unable to add torrent, decoding filedump failed")
            if digest in self.torrents:
                return err(f"Torrent already in session ({digest}).")
            self.torrents[digest] = {"progress": 0.0, "ratio": -1.0, "label": ""}
            return ok(digest)
        if method == "core.get_torrents_status":
            filt, keys = params
            out = {}
            for h, t in self.torrents.items():
                if "id" in filt and h not in filt["id"]:
                    continue
                if "label" in filt and t["label"] != filt["label"]:
                    continue
                out[h] = {k: ({"hash": h} | t).get(k) for k in keys}
            return ok(out)
        if method == "daemon.info":
            return ok("2.1.1")
        return err("Unknown method")


class DelugeCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDeluge()

    def client(self, password=FakeDeluge.PASSWORD, url="http://deluge:8112/"):
        c = DelugeClient(url, password)
        c._send = self.fake.send
        return c

    def methods(self):
        return [m for m, _ in self.fake.calls]


class TestLogin(DelugeCase):
    def test_login_checks_password_daemon_and_label_plugin(self):
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)
        self.assertEqual(self.methods(), ["auth.login", "web.connected", "core.get_enabled_plugins"])
        self.assertEqual(c.url, "http://deluge:8112")

    def test_a_disconnected_web_ui_connects_to_the_first_host(self):
        self.fake.connected = False
        self.client().login()
        self.assertIn(("web.connect", ["h1"]), self.fake.calls)

    def test_no_daemon_is_unavailable(self):
        self.fake.connected, self.fake.hosts = False, []
        with self.assertRaises(base.ClientError) as ctx:
            self.client().login()
        self.assertEqual(ctx.exception.kind, base.UNAVAILABLE)

    def test_bad_password_is_auth_and_never_shows_the_password(self):
        c = self.client(password="wrong-pw-xyz")
        with self.assertRaises(base.ClientError) as ctx:
            c.login()
        self.assertEqual(ctx.exception.kind, base.AUTH)
        self.assertNotIn("wrong-pw-xyz", str(ctx.exception))
        self.assertNotIn("wrong-pw-xyz", repr(c))
        self.assertFalse(c.logged_in)

    def test_no_password_sends_nothing(self):
        with self.assertRaises(base.ClientError) as ctx:
            self.client(password="").login()
        self.assertEqual(ctx.exception.kind, base.AUTH)
        self.assertEqual(self.fake.calls, [])

    def test_missing_label_plugin_is_reported_at_login(self):
        self.fake.plugins = []
        with self.assertRaisesRegex(base.ClientError, "Label plugin"):
            self.client().login()

    def test_http_errors_and_garbage(self):
        self.fake.status = 502
        with self.assertRaisesRegex(base.ClientError, "HTTP 502"):
            self.client().login()
        self.fake.status, self.fake.raw = 200, b"<html>"
        with self.assertRaisesRegex(base.ClientError, "unreadable"):
            self.client().login()

    def test_bad_url(self):
        for url in ("", "ftp://x", "http://user:pw@deluge:8112"):
            with self.assertRaises(base.ClientError):
                DelugeClient(url, "p")


class TestAdd(DelugeCase):
    def test_add_creates_the_label_adds_started_and_labels_it(self):
        digest = self.client().add_torrent(TORRENT, "tracker-1.torrent", "AudioBooks")
        self.assertEqual(digest, HASH)
        add = next(p for m, p in self.fake.calls if m == "core.add_torrent_file")
        self.assertEqual(add[0], "tracker-1.torrent")
        self.assertEqual(base64.b64decode(add[1]), TORRENT)
        self.assertEqual(add[2], {"add_paused": False})
        self.assertIn(("label.add", ["audiobooks"]), self.fake.calls, "Deluge labels are lower-case")
        self.assertEqual(self.fake.torrents[HASH]["label"], "audiobooks")

    def test_an_existing_label_is_not_added_again(self):
        self.fake.labels = ["audiobooks"]
        self.client().add_torrent(TORRENT)
        self.assertNotIn("label.add", self.methods())

    def test_duplicate_is_refused(self):
        c = self.client()
        c.add_torrent(TORRENT)
        with self.assertRaises(base.ClientError) as ctx:
            c.add_torrent(TORRENT)
        self.assertEqual(ctx.exception.kind, base.REFUSED)
        self.assertIn("already", str(ctx.exception))

    def test_invalid_torrent_is_refused(self):
        with self.assertRaises(base.ClientError) as ctx:
            self.client().add_torrent(b"<html>")
        self.assertEqual(ctx.exception.kind, base.REFUSED)

    def test_a_failed_label_after_the_add_still_returns_the_hash(self):
        c = self.client()
        real = self.fake.send

        def send(body):
            if json.loads(body)["method"] == "label.set_torrent":
                return 200, b'{"result": null, "error": {"message": "boom", "code": 4}, "id": 0}'
            return real(body)

        c._send = send
        with self.assertLogs("audiobind", "WARNING"):
            self.assertEqual(c.add_torrent(TORRENT), HASH)


class TestReads(DelugeCase):
    def setUp(self):
        super().setUp()
        a, b, other = "a" * 40, "b" * 40, "c" * 40
        self.fake.torrents = {a: {"progress": 100.0, "ratio": 1.5, "label": "audiobooks"},
                              b: {"progress": 40.0, "ratio": -1.0, "label": "audiobooks"},
                              other: {"progress": 10.0, "ratio": 0.0, "label": "tv"}}
        self.a, self.b, self.other = a, b, other

    def test_progress_is_a_fraction_in_one_request(self):
        out = self.client().progress([self.a.upper(), self.b, "f" * 40, "", None])
        self.assertEqual(out, {self.a: 1.0, self.b: 0.4})
        self.assertEqual(self.methods().count("core.get_torrents_status"), 1)

    def test_no_hashes_sends_nothing(self):
        self.assertEqual(self.client().progress(["", None]), {})
        self.assertEqual(self.fake.calls, [])

    def test_incomplete_and_snatch_state_follow_the_label(self):
        self.assertEqual(self.client().incomplete("AudioBooks", 0.999), 1)
        self.assertEqual(self.client().snatch_state("audiobooks"), {self.a: (1.0, 1.5), self.b: (0.4, 0.0)})

    def test_version_logs_out(self):
        c = self.client()
        self.assertEqual(c.version(), "2.1.1")
        self.assertEqual(self.methods()[-1], "auth.delete_session")
        self.assertFalse(c.logged_in)

    def test_logout_never_raises(self):
        c = self.client()
        c.login()

        def broken(body):
            raise OSError("gone")

        c._send = broken
        c.logout()
        self.assertFalse(c.logged_in)


if __name__ == "__main__":
    unittest.main()

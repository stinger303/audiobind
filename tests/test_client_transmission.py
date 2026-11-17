"""The Transmission adapter against a fake RPC transport (no network, no real Transmission)."""

import base64
import hashlib
import json
import unittest

from audiobind.clients import base
from audiobind.clients.transmission import TransmissionClient

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO).hexdigest()
AUTH = "Basic " + base64.b64encode(b"tuser:t pass").decode()


class FakeTransmission:
    """Transmission's RPC as far as the adapter cares: 409 + session id, optional basic auth."""

    def __init__(self, rpc_version=17):
        self.calls = []  # (method, arguments, headers)
        self.rpc_version = rpc_version
        self.session_id = "sess-1"
        self.need_auth = AUTH
        self.torrents = {}
        self.status = None  # force a status
        self.raw = None

    def send(self, body, headers):
        request = json.loads(body)
        self.calls.append((request["method"], request["arguments"], dict(headers)))
        if self.status:
            return self.status, {}, b""
        if self.need_auth and headers.get("Authorization") != self.need_auth:
            return 401, {}, b"Unauthorized"
        if headers.get("X-Transmission-Session-Id") != self.session_id:
            return 409, {"x-transmission-session-id": self.session_id}, b""
        if self.raw is not None:
            return 200, {}, self.raw
        method, args = request["method"], request["arguments"]

        def ok(arguments):
            return 200, {}, json.dumps({"result": "success", "arguments": arguments}).encode()

        if method == "session-get":
            return ok({"version": "4.0.5 (a6fe2a64aa)", "rpc-version": self.rpc_version})
        if method == "torrent-add":
            data = base64.b64decode(args["metainfo"])
            if data != TORRENT:
                return 200, {}, b'{"result": "invalid or corrupt torrent file", "arguments": {}}'
            if HASH in self.torrents:
                return ok({"torrent-duplicate": {"hashString": HASH, "id": 1}})
            self.torrents[HASH] = {"hashString": HASH, "percentDone": 0.0, "uploadRatio": -1,
                                   "labels": args.get("labels", [])}
            return ok({"torrent-added": {"hashString": HASH, "id": 1, "name": "book"}})
        if method == "torrent-set":
            for h in args["ids"]:
                self.torrents[h]["labels"] = args["labels"]
            return ok({})
        if method == "torrent-get":
            items = [t for h, t in self.torrents.items() if "ids" not in args or h in args["ids"]]
            return ok({"torrents": [{k: t.get(k) for k in args["fields"]} for t in items]})
        return 200, {}, b'{"result": "method name not recognized", "arguments": {}}'


class TransmissionCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTransmission()

    def client(self, user="tuser", password="t pass", url="http://transmission:9091"):
        c = TransmissionClient(url, user, password)
        c._send = self.fake.send
        return c


class TestLogin(TransmissionCase):
    def test_the_rpc_path_is_added_to_a_bare_address(self):
        self.assertEqual(self.client().url, "http://transmission:9091/transmission/rpc")
        self.assertEqual(TransmissionClient("http://t:9091/custom/rpc").url, "http://t:9091/custom/rpc")

    def test_login_follows_the_409_session_id_and_sends_basic_auth(self):
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)
        (_m1, _a1, h1), (_m2, _a2, h2) = self.fake.calls
        self.assertNotIn("X-Transmission-Session-Id", h1)
        self.assertEqual(h2["X-Transmission-Session-Id"], "sess-1")
        self.assertEqual(h2["Authorization"], AUTH)

    def test_no_auth_configured_sends_no_header(self):
        self.fake.need_auth = None
        c = self.client(user="", password="")
        c.login()
        self.assertNotIn("Authorization", self.fake.calls[-1][2])

    def test_bad_credentials_are_auth_and_never_show_the_password(self):
        c = self.client(password="wrong-pw-xyz")
        with self.assertRaises(base.ClientError) as ctx:
            c.login()
        self.assertEqual(ctx.exception.kind, base.AUTH)
        self.assertNotIn("wrong-pw-xyz", str(ctx.exception))
        self.assertNotIn("wrong-pw-xyz", repr(c))

    def test_whitelist_403_is_auth(self):
        self.fake.status = 403
        with self.assertRaisesRegex(base.ClientError, "whitelist"):
            self.client().login()

    def test_old_transmission_without_labels_is_refused(self):
        self.fake.rpc_version = 15
        with self.assertRaisesRegex(base.ClientError, "3.0 or newer"):
            self.client().login()

    def test_garbage_is_an_error(self):
        self.fake.raw = b"<html>"
        with self.assertRaisesRegex(base.ClientError, "unreadable"):
            self.client().login()


class TestAdd(TransmissionCase):
    def test_add_is_started_and_labelled_on_rpc_17(self):
        self.assertEqual(self.client().add_torrent(TORRENT, "t.torrent", "audiobooks"), HASH)
        method, args, _h = next(c for c in self.fake.calls if c[0] == "torrent-add")
        self.assertEqual(base64.b64decode(args["metainfo"]), TORRENT)
        self.assertIs(args["paused"], False)
        self.assertEqual(args["labels"], ["audiobooks"])
        self.assertNotIn("torrent-set", [c[0] for c in self.fake.calls])

    def test_rpc_16_labels_with_torrent_set(self):
        self.fake.rpc_version = 16
        self.client().add_torrent(TORRENT, category="audiobooks")
        add = next(c for c in self.fake.calls if c[0] == "torrent-add")
        self.assertNotIn("labels", add[1])
        self.assertEqual(self.fake.torrents[HASH]["labels"], ["audiobooks"])

    def test_duplicate_is_refused(self):
        c = self.client()
        c.add_torrent(TORRENT)
        with self.assertRaises(base.ClientError) as ctx:
            c.add_torrent(TORRENT)
        self.assertEqual(ctx.exception.kind, base.REFUSED)

    def test_invalid_torrent_is_refused(self):
        with self.assertRaises(base.ClientError) as ctx:
            self.client().add_torrent(b"<html>")
        self.assertEqual(ctx.exception.kind, base.REFUSED)
        self.assertIn("corrupt", str(ctx.exception))


class TestReads(TransmissionCase):
    def setUp(self):
        super().setUp()
        a, b, o = "a" * 40, "b" * 40, "c" * 40
        self.fake.torrents = {a: {"hashString": a, "percentDone": 1.0, "uploadRatio": 2.0, "labels": ["audiobooks"]},
                              b: {"hashString": b, "percentDone": 0.5, "uploadRatio": -1, "labels": ["audiobooks"]},
                              o: {"hashString": o, "percentDone": 0.1, "uploadRatio": 0, "labels": ["tv"]}}
        self.a, self.b = a, b

    def test_progress(self):
        self.assertEqual(self.client().progress([self.a.upper(), self.b, "f" * 40, None]), {self.a: 1.0, self.b: 0.5})
        self.assertEqual(self.client().progress([]), {})

    def test_category_reads(self):
        self.assertEqual(self.client().incomplete("audiobooks", 0.999), 1)
        self.assertEqual(self.client().snatch_state("audiobooks"), {self.a: (1.0, 2.0), self.b: (0.5, 0.0)})

    def test_version(self):
        self.assertEqual(self.client().version(), "4.0.5")


if __name__ == "__main__":
    unittest.main()

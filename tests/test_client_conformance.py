"""One scenario, four adapters: the download-client contract (clients/base.py) holds for each.

The transport is faked at the lowest urllib layer - an HTTPHandler whose http_open answers from a
fake client - so everything above the socket is the real code: each adapter's own request code,
the shared transport helper, urllib's HTTPError for 401/409, the cookie jar. No socket is ever
opened; "unreachable" is that handler raising ConnectionRefusedError, as a refused connect does.

The fake clients are simulations written from the protocols; this proves the adapters keep the
contract against them, not that a real Deluge / Transmission / rTorrent agrees.
"""

import base64
import email.message
import hashlib
import io
import json
import re
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
import xmlrpc.client
from http.cookiejar import CookieJar
from unittest import mock

from audiobind.clients import base, rtorrent
from audiobind.clients.base import ClientError, DownloadClient
from audiobind.clients.deluge import DelugeClient
from audiobind.clients.qbittorrent import QbitClient
from audiobind.clients.rtorrent import RTorrentClient
from audiobind.clients.transmission import TransmissionClient

try:
    from test_client_deluge import FakeDeluge
    from test_client_rtorrent import FakeRTorrent
    from test_client_transmission import FakeTransmission
except ImportError:
    from tests.test_client_deluge import FakeDeluge
    from tests.test_client_rtorrent import FakeRTorrent
    from tests.test_client_transmission import FakeTransmission

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO).hexdigest()
OTHER, ELSEWHERE, UNKNOWN = "a" * 40, "c" * 40, "f" * 40
GOOD, BAD = "right pw", "WRONG-PW-UNIQUE"
CATEGORY = "audiobooks"


def canonical(name: str) -> str:
    return "-".join(part.capitalize() for part in name.split("-"))


class FakeHandler(urllib.request.HTTPHandler):
    """The bottom of urllib: answers ``app(method, url, headers, body) -> (status, headers, body)``."""

    def __init__(self, app):
        super().__init__()
        self.app = app

    def http_open(self, req):
        headers = {canonical(k): v for k, v in req.header_items()}
        status, answer_headers, body = self.app(req.get_method(), req.full_url, headers, req.data or b"")
        msg = email.message.Message()
        for k, v in answer_headers.items():
            msg[k] = v
        response = urllib.response.addinfourl(io.BytesIO(body), msg, req.full_url, status)
        response.msg = "OK" if status < 400 else "Error"
        return response


def refused(*_a):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def garbage(*_a):
    return 200, {"Content-Type": "text/html"}, b"<html>a captive portal</html>"


# ---------------------------------------------------------------------------- harnesses
class QbitHarness:
    """qBittorrent 5.x WebUI, as the adapter has always talked to it."""

    def __init__(self):
        self.torrents = {}  # hash -> {"progress", "ratio", "category"}

    def app(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        if path == "/api/v2/auth/login":
            form = urllib.parse.parse_qs(body.decode())
            if form.get("password") == [GOOD]:
                return 204, {"Set-Cookie": "SID=abc123; path=/"}, b""
            return 200, {}, b"Fails."
        if "SID=abc123" not in headers.get("Cookie", ""):
            return 403, {}, b"Forbidden"
        if path == "/api/v2/torrents/add":
            if TORRENT not in body:
                return 415, {}, b""
            if HASH in self.torrents:
                return 200, {}, b"Fails."
            category = re.search(rb'name="category"\r\n\r\n(.*?)\r\n', body).group(1).decode()
            self.torrents[HASH] = {"progress": 0.0, "ratio": 0.0, "category": category}
            return 200, {}, b"Ok."
        if path == "/api/v2/torrents/info":
            items = [{"hash": h, **t} for h, t in self.torrents.items()
                     if ("category" not in query or t["category"] == query["category"][0])
                     and ("hashes" not in query or h in query["hashes"][0].split("|"))]
            return 200, {}, json.dumps(items).encode()
        if path == "/api/v2/app/version":
            return 200, {}, b"v5.0.4"
        if path == "/api/v2/auth/logout":
            return 200, {}, b""
        return 404, {}, b""

    def make(self, password=GOOD, app=None):
        client = QbitClient("http://qbit.example:8080", "admin", password)
        client._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(client.jar),
                                                     FakeHandler(app or self.app))
        return client

    def seed(self, digest, progress, ratio, category):
        self.torrents[digest] = {"progress": progress, "ratio": ratio, "category": category}


class DelugeHarness:
    def __init__(self):
        self.fake = FakeDeluge()
        self.fake.PASSWORD = GOOD

    def app(self, method, url, headers, body):
        assert urllib.parse.urlsplit(url).path == "/json"
        status, answer = self.fake.send(body)
        return status, {"Content-Type": "application/json"}, answer

    def make(self, password=GOOD, app=None):
        client = DelugeClient("http://deluge.example:8112", password)
        client._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(client.jar),
                                                     FakeHandler(app or self.app))
        return client

    def seed(self, digest, progress, ratio, category):
        self.fake.torrents[digest] = {"progress": progress * 100.0, "ratio": ratio, "label": category}


class TransmissionHarness:
    def __init__(self):
        self.fake = FakeTransmission()
        self.fake.need_auth = "Basic " + base64.b64encode(f"tuser:{GOOD}".encode()).decode()

    def app(self, method, url, headers, body):
        status, answer_headers, answer = self.fake.send(body, headers)
        return status, {canonical(k): v for k, v in answer_headers.items()}, answer

    def make(self, password=GOOD, app=None):
        client = TransmissionClient("http://transmission.example:9091", "tuser", password)
        client._opener = urllib.request.build_opener(FakeHandler(app or self.app))
        return client

    def seed(self, digest, progress, ratio, category):
        self.fake.torrents[digest] = {"hashString": digest, "percentDone": progress, "uploadRatio": ratio,
                                      "labels": [category]}


class RTorrentHarness:
    """rTorrent behind an XML-RPC /RPC2 mount with basic auth (SCGI, which has no login, is covered
    by the unreachable test below and by test_client_rtorrent's framing tests)."""

    def __init__(self):
        self.fake = FakeRTorrent()
        self.auth = "Basic " + base64.b64encode(f"ruser:{GOOD}".encode()).decode()

    def app(self, method, url, headers, body):
        if headers.get("Authorization") != self.auth:
            return 401, {"WWW-Authenticate": 'Basic realm="rtorrent"'}, b""
        return 200, {"Content-Type": "text/xml"}, self.fake.send(body)

    def make(self, password=GOOD, app=None):
        client = RTorrentClient("http://rutorrent.example/RPC2", "ruser", password)
        client._opener = urllib.request.build_opener(FakeHandler(app or self.app))
        return client

    def seed(self, digest, progress, ratio, category):
        self.fake.torrents[digest.upper()] = [digest.upper(), category, int(progress * 1000), 1000, int(ratio * 1000)]


# ---------------------------------------------------------------------------- the scenario
class Contract:
    """Mixed into one TestCase per adapter. ``harness`` builds the client over the fake transport."""

    harness_class = None
    name = ""

    def setUp(self):
        self.h = self.harness_class()

    def assert_kind(self, kinds, call):
        """``call`` raises ClientError of one of ``kinds`` - and nothing that is not a ClientError."""
        try:
            call()
        except ClientError as exc:
            self.assertIn(exc.kind, kinds, f"{self.name}: {exc!r}")
            self.assertNotIn(BAD, str(exc))
            return exc
        except Exception as exc:  # noqa: BLE001 - the point of the test
            self.fail(f"{self.name}: {type(exc).__name__} escaped instead of ClientError: {exc}")
        self.fail(f"{self.name}: no error raised")

    def test_shape(self):
        client = self.h.make()
        self.assertIsInstance(client, DownloadClient)
        self.assertEqual(client.name, self.name)
        self.assertFalse(client.logged_in)
        self.assertNotIn(GOOD, repr(client))

    def test_the_whole_scenario(self):
        client = self.h.make()
        client.login()
        self.assertTrue(client.logged_in)

        # add: the v1 hash, lower-case, started, in the category
        digest = client.add_torrent(TORRENT, "tracker-1.torrent", CATEGORY)
        self.assertEqual(digest, HASH)
        self.assertEqual(client.progress([HASH.upper(), UNKNOWN, "", None]), {HASH: 0.0},
                         "unknown hashes are absent, case does not matter")
        self.assertEqual(client.progress(["", None]), {})

        # the same torrent again, and a file that is not a torrent: refused
        self.assert_kind({base.REFUSED}, lambda: client.add_torrent(TORRENT, "tracker-1.torrent", CATEGORY))
        self.assert_kind({base.REFUSED}, lambda: client.add_torrent(b"<html>not a torrent</html>", "x.torrent", CATEGORY))

        # reads: progress 0..1, ratio, only the category
        self.h.seed(HASH, 0.5, 0.25, CATEGORY)
        self.h.seed(OTHER, 1.0, 2.0, CATEGORY)
        self.h.seed(ELSEWHERE, 0.1, 0.0, "tv")
        self.assertEqual(client.incomplete(CATEGORY, 0.999), 1)
        self.assertEqual(client.snatch_state(CATEGORY), {HASH: (0.5, 0.25), OTHER: (1.0, 2.0)})
        self.assertEqual(client.progress([HASH, OTHER, ELSEWHERE]), {HASH: 0.5, OTHER: 1.0, ELSEWHERE: 0.1})

        # the health probe, then logout - twice, and when the transport is gone
        version = client.version()
        self.assertTrue(version and not version.startswith(("v", "V")), version)
        self.assertFalse(client.logged_in)
        client.logout()
        broken = self.h.make(app=refused)
        broken.logged_in = True
        broken.logout()
        self.assertFalse(broken.logged_in)

    def test_unreachable_is_unavailable_on_every_call(self):
        client = self.h.make(app=refused)
        for call in self.calls(client):
            self.assert_kind({base.UNAVAILABLE}, call)
        client.logout()

    def test_bad_credentials_are_auth(self):
        client = self.h.make(password=BAD)
        exc = self.assert_kind({base.AUTH}, client.login)
        self.assertNotIn(BAD, repr(client))
        self.assertFalse(client.logged_in, exc)
        self.assert_kind({base.AUTH}, lambda: client.add_torrent(TORRENT, "t.torrent", CATEGORY))

    def test_garbage_answers_are_client_errors(self):
        client = self.h.make(app=garbage)
        for call in self.calls(client):
            self.assert_kind({base.AUTH, base.ERROR}, call)

    def calls(self, client):
        return (client.login, lambda: client.add_torrent(TORRENT, "t.torrent", CATEGORY),
                lambda: client.progress([HASH]), lambda: client.incomplete(CATEGORY, 0.5),
                lambda: client.snatch_state(CATEGORY), client.version)


class TestQbittorrentContract(Contract, unittest.TestCase):
    harness_class, name = QbitHarness, "qBittorrent"


class TestDelugeContract(Contract, unittest.TestCase):
    harness_class, name = DelugeHarness, "Deluge"


class TestTransmissionContract(Contract, unittest.TestCase):
    harness_class, name = TransmissionHarness, "Transmission"


class TestRTorrentContract(Contract, unittest.TestCase):
    harness_class, name = RTorrentHarness, "rTorrent"

    def test_scgi_unreachable_is_unavailable_on_every_call(self):
        with mock.patch.object(rtorrent.socket, "create_connection", side_effect=ConnectionRefusedError):
            client = RTorrentClient("scgi://rtorrent.example:5000")
            for call in self.calls(client):
                self.assert_kind({base.UNAVAILABLE}, call)


class TestEveryAdapterIsCovered(unittest.TestCase):
    def test_one_contract_case_per_download_client(self):
        from audiobind.config import DOWNLOAD_CLIENTS

        covered = {cls.name for cls in (TestQbittorrentContract, TestDelugeContract, TestTransmissionContract,
                                        TestRTorrentContract)}
        self.assertEqual(covered, set(DOWNLOAD_CLIENTS.values()))


if __name__ == "__main__":
    unittest.main()

"""The rTorrent adapter against a fake XML-RPC transport, and its SCGI framing against a fake
socket (no network, no real rTorrent)."""

import hashlib
import unittest
import xmlrpc.client
from unittest import mock

from audiobind.clients import base, rtorrent
from audiobind.clients.rtorrent import RTorrentClient

INFO = b"d6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaae"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO).hexdigest()


class FakeRTorrent:
    """rTorrent 0.9's XML-RPC as far as the adapter cares. Torrents: HASH(upper) -> row."""

    def __init__(self):
        self.calls = []
        self.torrents = {}
        self.take = True  # False: load.raw_start answers 0 but nothing appears
        self.raw = None

    def send(self, body):
        params, method = xmlrpc.client.loads(body)
        self.calls.append((method, params))
        if self.raw is not None:
            return self.raw

        def ok(value):
            return xmlrpc.client.dumps((value,), methodresponse=True).encode()

        def fault(text):
            return xmlrpc.client.dumps(xmlrpc.client.Fault(-501, text)).encode()

        if method == "system.client_version":
            return ok("0.9.8")
        if method == "d.hash":
            return ok(params[0]) if params[0] in self.torrents else fault("Could not find info-hash.")
        if method == "load.raw_start":
            data = params[1].data
            if self.take and data == TORRENT:
                label = ""
                for cmd in params[2:]:
                    if cmd.startswith("d.custom1.set="):
                        label = cmd.split("=", 1)[1].strip('"')
                self.torrents[HASH.upper()] = [HASH.upper(), label, 0, 1000, 0]
            return ok(0)
        if method == "d.multicall2":
            return ok([list(r) for r in self.torrents.values()])
        return fault(f"Method '{method}' not defined")


class RCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRTorrent()

    def client(self, url="scgi://rtorrent:5000"):
        c = RTorrentClient(url)
        c._send = self.fake.send
        return c

    def methods(self):
        return [m for m, _ in self.fake.calls]


class TestAddress(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(RTorrentClient("scgi://rt:5000")._scgi, ("rt", 5000))
        self.assertIsNone(RTorrentClient("https://rt.example/RPC2", "u", "p")._scgi)
        for bad in ("", "scgi://rt", "ftp://rt:1", "http://u:p@rt/RPC2"):
            with self.assertRaises(base.ClientError):
                RTorrentClient(bad)


class TestCalls(RCase):
    def test_login_asks_the_version(self):
        c = self.client()
        c.login()
        self.assertTrue(c.logged_in)
        self.assertEqual(self.methods(), ["system.client_version"])

    def test_add_checks_absent_loads_started_with_custom1_and_checks_present(self):
        self.assertEqual(self.client().add_torrent(TORRENT, "t.torrent", "audio books"), HASH)
        self.assertEqual(self.methods(), ["system.client_version", "d.hash", "load.raw_start", "d.hash"])
        _m, params = self.fake.calls[2]
        self.assertEqual(params[0], "")
        self.assertEqual(params[1].data, TORRENT)
        self.assertEqual(params[2], 'd.custom1.set="audio books"')

    def test_duplicate_is_refused_before_loading(self):
        c = self.client()
        c.add_torrent(TORRENT)
        with self.assertRaises(base.ClientError) as ctx:
            c.add_torrent(TORRENT)
        self.assertEqual(ctx.exception.kind, base.REFUSED)
        self.assertEqual(self.methods().count("load.raw_start"), 1)

    def test_a_silently_ignored_load_is_refused(self):
        self.fake.take = False
        with self.assertRaises(base.ClientError) as ctx:
            self.client().add_torrent(TORRENT)
        self.assertEqual(ctx.exception.kind, base.REFUSED)

    def test_an_unreadable_torrent_is_never_sent(self):
        with self.assertRaises(base.ClientError) as ctx:
            self.client().add_torrent(b"<html>")
        self.assertEqual(ctx.exception.kind, base.REFUSED)
        self.assertNotIn("load.raw_start", self.methods())

    def test_a_category_that_cannot_be_quoted_is_an_error(self):
        with self.assertRaises(base.ClientError):
            self.client().add_torrent(TORRENT, category='x"; execute=rm')
        self.assertNotIn("load.raw_start", self.methods())

    def test_reads(self):
        a, b, o = "A" * 40, "B" * 40, "C" * 40
        self.fake.torrents = {a: [a, "audiobooks", 1000, 1000, 2500], b: [b, "audiobooks", 250, 1000, 0],
                              o: [o, "tv", 0, 0, 0], "junk": ["x"]}
        c = self.client()
        self.assertEqual(c.progress([a.lower(), b, None]), {"a" * 40: 1.0, "b" * 40: 0.25})
        self.assertEqual(c.incomplete("audiobooks", 0.999), 1)
        self.assertEqual(c.snatch_state("audiobooks"), {"a" * 40: (1.0, 2.5), "b" * 40: (0.25, 0.0)})

    def test_rutorrent_url_encoded_labels_match(self):
        a = "A" * 40
        self.fake.torrents = {a: [a, "audio%20books", 10, 10, 0]}
        self.assertEqual(self.client().incomplete("audio books", 0.999), 0)
        self.assertEqual(list(self.client().snatch_state("audio books")), ["a" * 40])

    def test_garbage_and_faults_are_client_errors(self):
        self.fake.raw = b"<html>not xml-rpc"
        with self.assertRaisesRegex(base.ClientError, "unreadable"):
            self.client().login()

    def test_version(self):
        self.assertEqual(self.client().version(), "0.9.8")


class FakeSocket:
    def __init__(self, answer):
        self.sent, self.answer = b"", answer

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        chunk, self.answer = self.answer[:n], self.answer[n:]
        return chunk


class TestScgiFraming(unittest.TestCase):
    def test_netstring_headers_and_cgi_answer(self):
        body = xmlrpc.client.dumps(("0.9.8",), methodresponse=True).encode()
        sock = FakeSocket(b"Status: 200 OK\r\nContent-Type: text/xml\r\n\r\n" + body)
        with mock.patch.object(rtorrent.socket, "create_connection", return_value=sock) as connect:
            self.assertEqual(RTorrentClient("scgi://rt:5000").version(), "0.9.8")
        connect.assert_called_once_with(("rt", 5000), timeout=rtorrent.TIMEOUT)
        length, _, rest = sock.sent.partition(b":")
        headers, body_sent = rest[:int(length)], rest[int(length) + 1:]
        self.assertEqual(rest[int(length):int(length) + 1], b",")
        fields = headers.split(b"\x00")
        self.assertEqual(fields[:4], [b"CONTENT_LENGTH", str(len(body_sent)).encode(), b"SCGI", b"1"])
        self.assertEqual(xmlrpc.client.loads(body_sent)[1], "system.client_version")

    def test_a_non_200_status_is_an_error(self):
        sock = FakeSocket(b"Status: 500 Internal\r\n\r\n")
        with mock.patch.object(rtorrent.socket, "create_connection", return_value=sock):
            with self.assertRaisesRegex(base.ClientError, "500"):
                RTorrentClient("scgi://rt:5000").login()

    def test_connection_refused_is_unavailable(self):
        with mock.patch.object(rtorrent.socket, "create_connection", side_effect=ConnectionRefusedError):
            with self.assertRaises(base.ClientError) as ctx:
                RTorrentClient("scgi://rt:5000").login()
        self.assertEqual(ctx.exception.kind, base.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()

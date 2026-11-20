"""SEC-04: Basic credentials must not follow a redirect to another origin (or to plain http).

Two real HTTP servers on 127.0.0.1, on different ports - different origins. "A" redirects,
"B" records every request it gets, so the test sees exactly what left the process."""

import http.server
import threading
import unittest
import urllib.request

from audiobind.clients import transport
from audiobind.clients.rtorrent import RTorrentClient
from audiobind.clients.transmission import TransmissionClient


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list  # set per server

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        target = self.server.redirects.get(self.path)
        if target:
            # 302: urllib turns the POST into a GET, follows it and (unguarded) copies Authorization.
            # (It never follows a 307/308 for a POST at all.)
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        return


def _server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen, server.redirects = [], {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


AUTH = transport.basic_auth("user", "s3cret-pass")


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = _server(), _server()
        self.a_url = "http://127.0.0.1:%d" % self.a.server_address[1]
        self.b_url = "http://127.0.0.1:%d" % self.b.server_address[1]

    def tearDown(self):
        for server in (self.a, self.b):
            server.shutdown()
            server.server_close()

    def openers(self):
        """Every opener that sends basic_auth() headers."""
        return {
            "transport": transport.guarded_opener(),
            "transmission": TransmissionClient(self.a_url + "/transmission/rpc", "user", "s3cret-pass")._opener,
            "rtorrent": RTorrentClient(self.a_url + "/RPC2", "user", "s3cret-pass")._opener,
        }

    def test_same_origin_redirect_is_still_followed(self):
        self.a.redirects["/old"] = self.a_url + "/new"
        for name, opener in self.openers().items():
            self.a.seen.clear()
            status, _, answer = transport.http_post(opener, self.a_url + "/old", b"{}", dict(AUTH), "Test")
            self.assertEqual((status, answer), (200, b"ok"), name)
            self.assertEqual([p for _, p, _ in self.a.seen], ["/old", "/new"], name)
            self.assertEqual(self.a.seen[-1][2], AUTH["Authorization"], name)

    def test_cross_origin_redirect_is_refused_and_no_credentials_leave(self):
        self.a.redirects["/rpc"] = self.b_url + "/stolen"
        for name, opener in self.openers().items():
            status, _, _ = transport.http_post(opener, self.a_url + "/rpc", b"{}", dict(AUTH), "Test")
            self.assertEqual(status, 302, name)  # the redirect itself is the answer
        self.assertEqual([auth for _, _, auth in self.b.seen if auth], [])
        self.assertEqual(self.b.seen, [])

    def test_unauthenticated_cross_origin_redirect_still_follows(self):
        self.a.redirects["/rpc"] = self.b_url + "/elsewhere"
        status, _, _ = transport.http_post(transport.guarded_opener(), self.a_url + "/rpc", b"{}", {}, "Test")
        self.assertEqual(status, 200)
        self.assertEqual([p for _, p, _ in self.b.seen], ["/elsewhere"])


class OriginRuleTests(unittest.TestCase):
    """The decision itself, for schemes a plain test server cannot offer (https)."""

    def decide(self, url, newurl, headers=None):
        handler = transport.GuardedRedirects()
        req = urllib.request.Request(url, data=b"{}", method="POST", headers=dict(AUTH if headers is None else headers))
        return handler.redirect_request(req, None, 302, "Found", {}, newurl)

    def test_https_to_http_is_refused(self):
        self.assertIsNone(self.decide("https://tower.lan/rpc", "http://tower.lan/rpc"))

    def test_other_host_or_port_is_refused(self):
        self.assertIsNone(self.decide("https://tower.lan/rpc", "https://evil.example/rpc"))
        self.assertIsNone(self.decide("https://tower.lan/rpc", "https://tower.lan:8443/rpc"))

    def test_same_origin_spellings_are_followed(self):
        for newurl in ("https://tower.lan/rpc2", "https://TOWER.lan:443/rpc2"):
            new = self.decide("https://tower.lan/rpc", newurl)
            self.assertIsNotNone(new, newurl)
            self.assertEqual(new.get_header("Authorization"), AUTH["Authorization"])

    def test_http_to_https_upgrade_on_the_same_host_is_refused(self):
        # a different origin all the same; refusing is the safe side
        self.assertIsNone(self.decide("http://tower.lan/rpc", "https://tower.lan/rpc"))


if __name__ == "__main__":
    unittest.main()

"""0.19.0-dev5: the web server caps concurrent connections and bounds the time to read headers."""

import socket
import threading
import time
import unittest
from unittest import mock

from audiobind import web


class Quick(web.Handler):
    HEADER_DEADLINE = 0.5

    def do_GET(self):  # noqa: N802
        self._send(200, b"ok", "text/plain")

    def log_message(self, *args):
        pass


def start(cap):
    server = web.BoundedServer(("127.0.0.1", 0), Quick, cap)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_for(predicate, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestConnectionCap(unittest.TestCase):
    def setUp(self):
        self.server = start(3)
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.socks = []
        self.addCleanup(lambda: [s.close() for s in self.socks])

    def conn(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.socks.append(s)
        return s

    def test_over_the_cap_gets_503_and_the_slot_comes_back(self):
        held = [self.conn() for _ in range(3)]  # connected, sending nothing
        self.assertTrue(wait_for(lambda: self.server.open_connections() == 3))
        extra = self.conn()
        self.assertTrue(extra.recv(200).startswith(b"HTTP/1.1 503"))
        self.assertEqual(extra.recv(200), b"")  # and closed
        held[0].close()
        self.assertTrue(wait_for(lambda: self.server.open_connections() == 2))
        s = self.conn()
        s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"200 OK", s.recv(500))

    def test_counter_never_exceeds_the_cap_and_every_slot_is_released(self):
        results = []

        def one():
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                results.append(s.recv(500)[:12])
                s.close()
            except OSError:
                results.append(b"err")

        threads = [threading.Thread(target=one) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(self.server.peak_open, 3)
        self.assertTrue(wait_for(lambda: self.server.open_connections() == 0))
        self.assertTrue(all(r.startswith((b"HTTP/1.0 200", b"HTTP/1.1 503", b"err")) for r in results), results)
        self.assertGreater(sum(r.startswith(b"HTTP/1.0 200") for r in results), 0)
        # all slots are free again: the cap's worth of connections are accepted
        held = [self.conn() for _ in range(3)]
        self.assertTrue(wait_for(lambda: self.server.open_connections() == 3))
        del held

    def test_a_handler_that_raises_still_releases_its_slot(self):
        with mock.patch.object(web.ThreadingHTTPServer, "process_request_thread", side_effect=RuntimeError):
            s = self.conn()
            self.assertTrue(wait_for(lambda: self.server.open_connections() == 0))
            s.close()

    def test_drip_fed_headers_are_cut_at_the_total_deadline(self):
        s = self.conn()
        started = time.time()
        closed = False
        try:
            for part in (b"GET / HTTP/1.0\r\n", b"Host: localhost\r\n", b"X-A: 1\r\n", b"X-B: 2\r\n",
                         b"X-C: 3\r\n", b"X-D: 4\r\n", b"X-E: 5\r\n", b"X-F: 6\r\n"):
                s.sendall(part)
                time.sleep(0.25)  # each piece is well inside any per-recv timeout
        except OSError:
            closed = True
        if not closed:
            s.settimeout(2)
            try:
                closed = s.recv(100) == b""
            except OSError:
                closed = True
        self.assertTrue(closed)
        self.assertLess(time.time() - started, 3.0)
        self.assertTrue(wait_for(lambda: self.server.open_connections() == 0))

    def test_a_prompt_request_is_not_cut(self):
        s = self.conn()
        time.sleep(0.2)
        s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"200 OK", s.recv(500))


class TestConfig(unittest.TestCase):
    def test_env(self):
        from audiobind.config import Config
        with mock.patch.dict("os.environ", {"MAX_CONNECTIONS": "7"}):
            self.assertEqual(Config.from_env().max_connections, 7)
        with mock.patch.dict("os.environ", {"MAX_CONNECTIONS": "0"}):
            self.assertEqual(Config.from_env().max_connections, 1)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MAX_CONNECTIONS", None)
            self.assertEqual(Config.from_env().max_connections, 64)


if __name__ == "__main__":
    unittest.main()

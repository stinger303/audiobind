"""AB-AUTH-1: a rejected tracker session is its own error, with a fixed short backoff.

Everything goes through a fake transport: nothing here touches the network."""

import json
import unittest

from audiobind import tracker

BASE = "https://tracker.example"


def endpoints():
    return tracker.Endpoints.build(BASE, "/search.json", "/download.php", search_method="POST",
                                   search_body='{"q": "{{title}}"}')


class FakeTransport:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, method, url, data, headers, timeout):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def client(*answers):
    clock = Clock()
    transport = FakeTransport(*answers)
    limiter = tracker.RateLimiter(spacing=0, clock=clock, sleep=lambda s: None)
    backoff = tracker.Backoff(clock=clock)
    return tracker.TrackerClient("cookie", endpoints(), "mam_id", transport=transport,
                                 limiter=limiter, backoff=backoff), clock, transport


class AuthRejection(unittest.TestCase):
    def test_403_not_signed_in_is_an_auth_error(self):
        c, _clock, _t = client((403, {}, b"Error, you are not signed in"))
        with self.assertRaises(tracker.TrackerAuthError) as ctx:
            c.search("Dune")
        err = ctx.exception
        self.assertIsInstance(err, tracker.TrackerError)
        self.assertEqual(err.status, 403)
        self.assertIn("Settings > Watcher", str(err))
        self.assertIn("HTTP 403: you are not signed in", str(err))
        self.assertEqual(c.backoff.remaining(), 180.0)
        self.assertNotIn("cookie", str(err).replace("session cookie", "").replace("fresh cookie", ""))

    def test_401_with_empty_body_is_still_an_auth_error(self):
        c, _clock, _t = client((401, {}, b""))
        with self.assertRaises(tracker.TrackerAuthError) as ctx:
            c.search("Dune")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(c.backoff.remaining(), 180.0)

    def test_403_with_an_unrecognisable_body_stays_generic(self):
        c, _clock, _t = client((403, {}, b"<html>Forbidden by upstream</html>"))
        with self.assertRaises(tracker.TrackerError) as ctx:
            c.search("Dune")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerAuthError)
        self.assertEqual(c.backoff.remaining(), 60.0)

    def test_undecodable_body_does_not_raise_something_else(self):
        c, _clock, _t = client((403, {}, b"\xff\xfe\x00 not signed in \xff"))
        with self.assertRaises(tracker.TrackerAuthError):
            c.search("Dune")

    def test_only_the_first_512_bytes_are_read(self):
        c, _clock, _t = client((403, {}, b"x" * 600 + b"not signed in"))
        with self.assertRaises(tracker.TrackerError) as ctx:
            c.search("Dune")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerAuthError)

    def test_a_login_header_counts(self):
        c, _clock, _t = client((403, {"Location": "/login.php"}, b""))
        with self.assertRaises(tracker.TrackerAuthError):
            c.search("Dune")


class OtherFailuresUnchanged(unittest.TestCase):
    def test_429_and_500_are_plain_errors(self):
        for status in (429, 500):
            c, _clock, _t = client((status, {}, b"slow down"))
            with self.assertRaises(tracker.TrackerError) as ctx:
                c.search("Dune")
            self.assertNotIsInstance(ctx.exception, tracker.TrackerAuthError)
            self.assertEqual(ctx.exception.status, status)
            self.assertEqual(c.backoff.remaining(), 60.0)

    def test_500s_still_double(self):
        c, clock, _t = client((500, {}, b""), (500, {}, b""))
        with self.assertRaises(tracker.TrackerError):
            c.search("Dune")
        clock.now += 61
        with self.assertRaises(tracker.TrackerError):
            c.search("Dune")
        self.assertEqual(c.backoff.remaining(), 120.0)

    def test_200_json_still_returns_torrents(self):
        payload = {"data": [{"id": 7, "title": "Dune", "author_info": json.dumps({"1": "Frank Herbert"}),
                             "filetype": "m4b", "category": "14"}]}
        c, _clock, _t = client((200, {}, json.dumps(payload).encode()))
        found = c.search("Dune")
        self.assertEqual(len(found), 1)
        self.assertEqual(c.backoff.remaining(), 0.0)


class AuthBackoffDoesNotEscalate(unittest.TestCase):
    def test_two_auth_failures_keep_the_delay_at_180(self):
        denied = (403, {}, b"Error, you are not signed in")
        c, clock, _t = client(denied, denied)
        with self.assertRaises(tracker.TrackerAuthError):
            c.search("Dune")
        clock.now += 181
        with self.assertRaises(tracker.TrackerAuthError):
            c.search("Dune")
        self.assertEqual(c.backoff.remaining(), 180.0)

    def test_a_500_after_auth_failures_starts_the_normal_schedule(self):
        denied = (403, {}, b"Error, you are not signed in")
        c, clock, _t = client(denied, denied, (500, {}, b""))
        for _ in range(2):
            with self.assertRaises(tracker.TrackerAuthError):
                c.search("Dune")
            clock.now += 181
        with self.assertRaises(tracker.TrackerError) as ctx:
            c.search("Dune")
        self.assertNotIsInstance(ctx.exception, tracker.TrackerAuthError)
        self.assertEqual(c.backoff.remaining(), 60.0)

    def test_an_auth_failure_after_500s_does_not_clear_their_count(self):
        c, clock, _t = client((500, {}, b""), (403, {}, b"not signed in"), (500, {}, b""))
        with self.assertRaises(tracker.TrackerError):
            c.search("Dune")
        clock.now += 61
        with self.assertRaises(tracker.TrackerAuthError):
            c.search("Dune")
        clock.now += 181
        with self.assertRaises(tracker.TrackerError):
            c.search("Dune")
        self.assertEqual(c.backoff.remaining(), 120.0)


if __name__ == "__main__":
    unittest.main()

"""AB-AUTH-2: the Test button beside the session field. It sends the typed value, saves nothing, makes
exactly one tracker request, says what happened in plain words, and clears the backoff only on success.

No test here touches the network: the tracker client is built with a fake transport."""

import json
import unittest
from unittest import mock

from audiobind import tracker, watcher

try:
    from live import LiveServerCase, seed_account
except ImportError:
    from tests.live import LiveServerCase, seed_account
from tests.tracker_cfg import COOKIE, make_client, tracker_cfg

ROUTE = "/api/watcher/test-session"
TYPED = "typed-not-saved-value"

PAYLOAD = {"data": [{"id": 1, "title": "Dune", "author_info": json.dumps({"1": "Frank Herbert"}),
                     "filetype": "m4b"},
                    {"id": 2, "title": "Dune Messiah", "author_info": json.dumps({"1": "Frank Herbert"}),
                     "filetype": "m4b"}]}


class Net:
    """A fake transport that records every request."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append((method, url, headers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class SessionTestCase(LiveServerCase):
    answer = (200, {}, json.dumps(PAYLOAD).encode())

    def setUp(self):
        super().setUp()
        self.cfg = tracker_cfg("saved-cookie")
        self.cfg.start()
        self.net = Net(self.answer)
        # the watcher's own client: it holds the backoff the Test must clear (or leave alone)
        self.shared = make_client(transport=Net(AssertionError("the shared client must not send")))
        self.shared.backoff.fail_fixed(180)
        self.shared.backoff.failures = 2
        self.patches = [
            mock.patch.object(watcher.WATCHER, "_client", self.shared),
            mock.patch.object(watcher, "make_tracker", side_effect=self.build),
        ]
        for p in self.patches:
            p.start()

    def build(self, route=None):
        self.built_with = route.settings["tracker_session"]
        return make_client(route.settings["tracker_session"], transport=self.net)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.cfg.stop()
        super().tearDown()

    def press(self, session=TYPED):
        return self.request(ROUTE, {"session": session})


class Success(SessionTestCase):
    def test_signed_in_sentence_clears_the_backoff_and_saves_nothing(self):
        status, body = self.press()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["message"], "Signed in. The search answered with 2 results.")
        self.assertEqual(len(self.net.calls), 1)  # exactly one tracker request
        self.assertEqual(self.net.calls[0][2]["Cookie"], f"{COOKIE}={TYPED}")
        self.assertEqual(self.shared.backoff.remaining(), 0)
        self.assertEqual(self.shared.backoff.failures, 0)
        self.assertIsNone(self.state.get_meta("setting_tracker_session"))  # tested, not saved
        self.assertNotIn(TYPED, json.dumps(body))

    def test_a_blank_box_tests_the_saved_value_through_the_watchers_client(self):
        self.shared.backoff.ok()
        self.shared.transport = self.net
        self.shared._session = "saved-cookie"
        status, body = self.press("")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.net.calls), 1)
        self.assertEqual(self.net.calls[0][2]["Cookie"], f"{COOKIE}=saved-cookie")


class Rejected(SessionTestCase):
    answer = (403, {}, b"Error, you are not signed in")

    def test_the_actionable_sentence_and_the_backoff_is_left_alone(self):
        status, body = self.press()
        self.assertEqual(status, 502)
        self.assertIn("the tracker rejected the session cookie (HTTP 403: you are not signed in)",
                      body["message"])
        self.assertIn("paste a fresh cookie value in Settings > Watcher", body["message"])
        self.assertTrue(body["message"].endswith("Paste a fresh value and press Test again."))
        self.assertEqual(len(self.net.calls), 1)  # never retried
        self.assertGreater(self.shared.backoff.remaining(), 0)
        self.assertEqual(self.shared.backoff.failures, 2)

    def test_it_is_a_tracker_auth_error_underneath(self):
        with self.assertRaises(tracker.TrackerAuthError):
            make_client(transport=self.net).search("Dune")


class TimedOut(SessionTestCase):
    answer = TimeoutError("timed out")

    def test_not_a_sign_in_problem(self):
        status, body = self.press()
        self.assertEqual(status, 502)
        self.assertEqual(body["message"], "The tracker did not answer: it could not be reached. "
                                          "This is not a sign-in problem.")
        self.assertEqual(len(self.net.calls), 1)
        self.assertGreater(self.shared.backoff.remaining(), 0)


class ServerError(SessionTestCase):
    answer = (503, {}, b"down")

    def test_5xx_is_not_a_sign_in_problem(self):
        status, body = self.press()
        self.assertEqual(status, 502)
        self.assertIn("it had a problem of its own (HTTP 503). This is not a sign-in problem.",
                      body["message"])
        self.assertGreater(self.shared.backoff.remaining(), 0)


class Rendering(SessionTestCase):
    def test_the_button_sits_with_the_session_field(self):
        status, body = self.request("/settings?tab=watcher", headers={}, raw=True)
        page = body.decode()
        self.assertEqual(status, 200)
        field = page[page.index('data-setting="tracker_session"'):]
        field = field[:field.index("</div></div>") + 12]
        self.assertIn('id="ab-test-session"', field)
        self.assertIn("abTestSession()", field)
        self.assertNotIn("saved-cookie", page)

    def test_a_non_string_session_is_refused(self):
        status, _body = self.request(ROUTE, {"session": 5})
        self.assertEqual(status, 400)
        self.assertEqual(self.net.calls, [])

    def test_a_malformed_session_is_refused_without_a_request(self):
        status, body = self.press("two words")
        self.assertEqual(status, 400)
        self.assertNotIn("two words", json.dumps(body))
        self.assertEqual(self.net.calls, [])


class Guards(SessionTestCase):
    def test_cross_site_origin_is_refused(self):
        status, _ = self.request(ROUTE, {"session": TYPED},
                                 {"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.net.calls, [])

    def test_a_form_post_is_refused(self):
        status, _ = self.request(ROUTE, b"session=x", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertEqual(self.net.calls, [])

    def test_a_get_is_405(self):
        status, _ = self.request(ROUTE, headers={})
        self.assertEqual(status, 405)


class SignedOut(SessionTestCase):
    signed_in = False

    def test_an_account_exists_but_nobody_is_signed_in(self):
        seed_account(self.state)  # an account, but this request carries no session cookie
        status, _ = self.press()
        self.assertEqual(status, 401)
        self.assertEqual(self.net.calls, [])


if __name__ == "__main__":
    unittest.main()

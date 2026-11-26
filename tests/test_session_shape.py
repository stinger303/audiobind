"""AB-AUTH-5: a session value that cannot be a cookie is refused, with its length in the message and nothing
saved. Settings-Save and the Test button reach the same check, so one rule covers both."""

import unittest
from unittest import mock

from audiobind import config, wizard

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase
from tests.tracker_cfg import tracker_cfg

SAVE = "/api/settings"
TEST = "/api/watcher/test-session"
LONG_VALUE = ("s%3A" + "Ab1%2B" * 58 + "xyz%3D")[:294]  # URL-encoded, as a browser shows a cookie
FIVE = "98058"


class SessionShape(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.cfg = tracker_cfg("some-saved-cookie-value-123456")
        self.cfg.start()
        # the Test path must never reach a tracker in these tests
        self.sent = mock.patch.object(wizard, "test_tracker", side_effect=AssertionError("no request"))
        self.sent.start()

    def tearDown(self):
        self.sent.stop()
        self.cfg.stop()
        super().tearDown()

    def save(self, value):
        return self.request(SAVE, {"settings": {"tracker_session": value}})

    def stored(self):
        return self.state.get_meta("setting_tracker_session")

    def test_the_fixture_is_294_characters(self):
        self.assertEqual(len(LONG_VALUE), 294)

    def test_five_characters_are_refused_with_the_count_and_nothing_is_stored(self):
        status, body = self.save(FIVE)
        self.assertEqual(status, 400, body)
        self.assertIn("that is 5 characters", body["error"])
        self.assertIn("nothing was saved", body["error"])
        self.assertNotIn(FIVE, body["error"].replace("5 characters", ""))  # the value is never echoed
        self.assertIsNone(self.stored())

    def test_a_294_character_url_encoded_value_is_accepted_and_stored(self):
        status, body = self.save(LONG_VALUE)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.stored(), LONG_VALUE)

    def test_the_shortest_accepted_length_is_24(self):
        self.assertEqual(config.WATCHER_TEXT_CHECKS["tracker_session"][1]("a" * 24), "a" * 24)
        with self.assertRaises(ValueError) as caught:
            config.WATCHER_TEXT_CHECKS["tracker_session"][1]("a" * 23)
        self.assertIn("23 characters", str(caught.exception))

    def test_an_embedded_space_is_refused_and_nothing_is_stored(self):
        status, body = self.save("a" * 20 + " " + "b" * 20)
        self.assertEqual(status, 400, body)
        self.assertIn("no spaces", body["error"])
        self.assertIsNone(self.stored())

    def test_an_embedded_newline_is_refused_and_nothing_is_stored(self):
        for value in ("a" * 20 + "\n" + "b" * 20, "a" * 20 + "\t" + "b" * 20, "a" * 20 + "\x00" + "b" * 20):
            status, body = self.save(value)
            self.assertEqual(status, 400, repr(value))
            self.assertIsNone(self.stored())

    def test_blank_still_keeps_what_is_stored(self):
        status, body = self.save("")
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.stored())

    def test_the_test_button_is_refused_by_the_same_rule_and_sends_nothing(self):
        save_status, save_body = self.save(FIVE)
        status, body = self.request(TEST, {"session": FIVE})
        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["message"], save_body["error"])  # the very same message from the same check
        self.assertIn("that is 5 characters", body["message"])
        self.assertIsNone(self.stored())

    def test_the_test_button_refuses_a_space_too(self):
        status, body = self.request(TEST, {"session": "a" * 20 + " " + "b" * 20})
        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])

    def test_both_paths_call_the_one_check(self):
        calls = []
        real = config.WATCHER_TEXT_CHECKS["tracker_session"]

        def spy(raw):
            calls.append(raw)
            return real[1](raw)

        with mock.patch.dict(config.WATCHER_TEXT_CHECKS, {"tracker_session": (real[0], spy)}):
            self.save(FIVE)
            self.request(TEST, {"session": FIVE})
        self.assertEqual(calls, [FIVE, FIVE])

    def test_the_container_value_is_not_held_to_the_rule(self):
        # the env path keeps its start-up check: a short container value is the owner's to fix, not a crash
        self.assertEqual(config._tracker_session("short"), "short")


if __name__ == "__main__":
    unittest.main()

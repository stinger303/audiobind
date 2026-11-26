"""AB-AUTH-6: Reset to container value on a tracker setting clears the tracker backoff, like Save does;
resetting an unrelated setting does not."""

import unittest
from unittest import mock

from audiobind import watcher

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase
from tests.tracker_cfg import make_client, tracker_cfg


class ResetClearsBackoff(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.cfg = tracker_cfg("container-cookie-value-123456")
        self.cfg.start()
        self.client = make_client()
        self.client.backoff.fail_fixed(180)
        self.client.backoff.failures = 3
        self.hold = mock.patch.object(watcher.WATCHER, "_client", self.client)
        self.hold.start()
        # two overrides saved first (a save of the session clears the backoff, so arm it afterwards)
        status, body = self.request("/api/settings", {"settings": {
            "tracker_session": "override-cookie-value-0123456789", "watch_interval": 60}})
        self.assertEqual(status, 200, body)
        self.client.backoff.fail_fixed(180)
        self.client.backoff.failures = 3
        self.state.set_meta(watcher.META_BACKOFF, {"until": self.client.backoff.until, "failures": 3})

    def tearDown(self):
        self.hold.stop()
        self.cfg.stop()
        super().tearDown()

    def reset(self, *keys):
        status, body = self.request("/api/settings", {"reset_keys": list(keys)})
        self.assertEqual(status, 200, body)

    def test_resetting_an_overridden_tracker_key_clears_the_backoff(self):
        self.assertGreater(self.client.backoff.remaining(), 0)
        self.reset("tracker_session")
        self.assertIsNone(self.state.get_meta("setting_tracker_session"))  # it really was reset
        self.assertEqual(self.client.backoff.remaining(), 0)
        self.assertEqual(self.client.backoff.failures, 0)
        self.assertEqual(self.state.get_meta(watcher.META_BACKOFF), {"until": 0.0, "failures": 0})

    def test_resetting_an_unrelated_key_does_not(self):
        self.reset("watch_interval")
        self.assertIsNone(self.state.get_meta("setting_watch_interval"))
        self.assertGreater(self.client.backoff.remaining(), 0)
        self.assertEqual(self.client.backoff.failures, 3)
        self.assertGreater(self.state.get_meta(watcher.META_BACKOFF)["until"], 0)

    def test_one_tracker_key_among_several_is_enough(self):
        self.reset("watch_interval", "tracker_session")
        self.assertEqual(self.client.backoff.remaining(), 0)

    def test_a_refused_reset_changes_nothing(self):
        status, _body = self.request("/api/settings", {"reset_keys": ["no_such_key", "tracker_session"]})
        self.assertEqual(status, 400)
        self.assertGreater(self.client.backoff.remaining(), 0)


if __name__ == "__main__":
    unittest.main()

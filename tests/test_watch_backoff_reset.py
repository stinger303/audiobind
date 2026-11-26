"""AB-AUTH-4: saving the tracker settings clears the tracker backoff; an unrelated save does not."""

import unittest
from unittest import mock

from audiobind import watcher

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase
from tests.tracker_cfg import make_client, tracker_cfg


class SaveResetsBackoff(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.cfg = tracker_cfg("old-cookie")
        self.cfg.start()
        self.client = make_client()
        self.client.backoff.fail_fixed(180)
        self.client.backoff.failures = 3
        # a client handed in from outside is never replaced by a settings change
        self.hold = mock.patch.object(watcher.WATCHER, "_client", self.client)
        self.hold.start()
        self.state.set_meta(watcher.META_BACKOFF, {"until": self.client.backoff.until, "failures": 3})

    def tearDown(self):
        self.hold.stop()
        self.cfg.stop()
        super().tearDown()

    def save(self, settings):
        status, body = self.request("/api/settings", {"settings": settings})
        self.assertEqual(status, 200, body)

    def test_saving_the_session_resets_it(self):
        self.assertGreater(self.client.backoff.remaining(), 0)
        self.save({"tracker_session": "fresh-cookie"})
        self.assertEqual(self.client.backoff.remaining(), 0)
        self.assertEqual(self.client.backoff.failures, 0)
        self.assertEqual(self.state.get_meta(watcher.META_BACKOFF), {"until": 0.0, "failures": 0})

    def test_an_unrelated_save_does_not(self):
        self.save({"watch_interval": 60})
        self.assertGreater(self.client.backoff.remaining(), 0)
        self.assertEqual(self.client.backoff.failures, 3)
        self.assertGreater(self.state.get_meta(watcher.META_BACKOFF)["until"], 0)

    def test_a_rejected_save_does_not(self):
        status, _body = self.request("/api/settings", {"settings": {"tracker_session": 5}})
        self.assertEqual(status, 400)
        self.assertGreater(self.client.backoff.remaining(), 0)


if __name__ == "__main__":
    unittest.main()

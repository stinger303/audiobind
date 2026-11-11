"""v0.15.1: every manual action refuses a book already in flight, and at most
MANUAL_WORKERS manual actions run at once."""

import os
import threading
import time
import unittest
from unittest import mock

from audiobind import config as config_mod
from audiobind import web
from audiobind.control import CONTROL
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class ConfigAndQueueTests(unittest.TestCase):
    def test_manual_workers_env(self):
        with mock.patch.dict(os.environ, {"MANUAL_WORKERS": "5"}):
            self.assertEqual(config_mod.Config.from_env().manual_workers, 5)
        with mock.patch.dict(os.environ, {"MANUAL_WORKERS": "0"}):
            self.assertEqual(config_mod.Config.from_env().manual_workers, 1)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MANUAL_WORKERS", None)
            self.assertEqual(config_mod.Config.from_env().manual_workers, 2)

    def test_try_enqueue_is_check_and_add(self):
        CONTROL._queue.clear()
        try:
            self.assertTrue(CONTROL.try_enqueue("A/B"))
            self.assertFalse(CONTROL.try_enqueue("A/B"))
            self.assertEqual(CONTROL.queue_snapshot()["count"], 1)
        finally:
            CONTROL._queue.clear()


class ManualBoundsLive(LiveServerCase):
    def setUp(self):
        super().setUp()
        CONTROL._queue.clear()
        CONTROL.current_unit = ""
        web.reset_manual_slots(2)
        self.gate = threading.Event()
        self.running = []

        def slow(**kw):
            self.running.append(kw["unit"].rel_path)
            self.gate.wait(10)
            return "review"

        self.patcher = mock.patch.object(web, "process_unit", side_effect=slow)
        self.patcher.start()
        for name in ("One", "Two", "Three"):
            os.makedirs(self.lib / "Author" / name)
            (self.lib / "Author" / name / "01.mp3").write_bytes(b"audio")
            self.state.save(UnitState(path=f"Author/{name}", status="review", title=name))

    def tearDown(self):
        self.gate.set()
        self._wait(lambda: not any(t.name.startswith("manual-") for t in threading.enumerate()))
        self.patcher.stop()
        CONTROL._queue.clear()
        CONTROL.current_unit = ""
        web.reset_manual_slots()
        super().tearDown()

    def _wait(self, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def act(self, action, name="One", **extra):
        return self.post({"action": action, "path": f"Author/{name}", **extra})

    def _queued_refusal(self, action, **extra):
        CONTROL.enqueue("Author/One")
        status, body = self.act(action, **extra)
        self.assertEqual(status, 409, body)
        self.assertIn("already waiting", body["error"])

    def test_retry_refused_when_queued(self):
        self._queued_refusal("retry")

    def test_force_refused_when_queued(self):
        self._queued_refusal("force")

    def test_recheck_refused_when_queued(self):
        self._queued_refusal("recheck")

    def test_set_asin_refused_when_queued_and_not_saved(self):
        self._queued_refusal("set_asin", asin="B005EHN61O")
        self.assertEqual(self.state.get_asin_override("Author/One"), "")
        self.assertNotIn("asin", self.state.locked_fields("Author/One"))

    def test_every_action_refused_while_encoding(self):
        CONTROL.current_unit = "Author/One"
        for action in ("retry", "force", "recheck", "set_asin"):
            with self.subTest(action=action):
                status, body = self.act(action, asin="B005EHN61O")
                self.assertEqual(status, 409, body)
                self.assertIn("encoding right now", body["error"])

    def test_second_click_on_same_book_is_409(self):
        self.assertEqual(self.act("retry")[0], 200)
        self.assertEqual(self.act("force")[0], 409)

    def test_concurrency_cap_returns_429(self):
        self.assertEqual(self.act("retry", "One")[0], 200)
        self.assertEqual(self.act("retry", "Two")[0], 200)
        status, body = self.act("retry", "Three")
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "another action is already running - try again when it finishes")
        self.assertNotIn("Author/Three", CONTROL.queue_snapshot()["units"])
        self.gate.set()
        self.assertTrue(self._wait(lambda: CONTROL.queue_snapshot()["count"] == 0))
        self.assertTrue(self._wait(lambda: self.act("recheck", "Three")[0] == 200, timeout=2))

    def test_refusals_do_not_leak_slots(self):
        web.reset_manual_slots(1)
        for _ in range(3):
            self.assertEqual(self.post({"action": "retry", "path": "Nobody/Here"})[0], 404)
            self.assertEqual(self.act("set_asin", asin="nope")[0], 400)
        self.assertEqual(self.act("retry")[0], 200)
        self.assertEqual(self.act("retry", "Two")[0], 429)

    def test_unknown_action_needs_no_slot(self):
        web.reset_manual_slots(1)
        self.assertEqual(self.act("explode")[0], 400)
        self.assertEqual(self.act("retry")[0], 200)


if __name__ == "__main__":
    unittest.main()

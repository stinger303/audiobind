"""v0.14.4: a manual action always leaves the queue, however it ends.

Before, only begin_unit() dequeued, so a Retry that ended in review/held, a Convert anyway
in dry run, an already-published book or an exception left the book "Waiting to convert"
forever - and hidden from every list - until a restart.
"""

import os
import threading
import unittest
from unittest import mock

from audiobind import web
from audiobind.control import CONTROL
from audiobind.state import UnitState
from audiobind.units import BookUnit, Part

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def unit(rel="Author/Book"):
    return BookUnit(path="/library/" + rel, name="Book", parent="/library/Author", parts=[Part("Book", [])], rel_path=rel)


class TestRunManual(unittest.TestCase):
    def setUp(self):
        CONTROL._queue.clear()

    def tearDown(self):
        CONTROL._queue.clear()

    def run_with(self, side_effect=None, status=None, force=False):
        state = mock.Mock()
        u = unit()
        CONTROL.enqueue(u.rel_path)
        seen_queued = []

        def fake(**kw):
            seen_queued.append(u.rel_path in CONTROL.queue_snapshot()["units"])
            if side_effect:
                raise side_effect
            return status

        with mock.patch.object(web, "process_unit", side_effect=fake) as proc:
            web.run_manual(state, u, force=force)
        self.assertEqual(seen_queued, [True])  # still queued while it runs
        self.assertEqual(CONTROL.queue_snapshot()["count"], 0)
        return proc

    def test_review_outcome_dequeues(self):
        self.run_with(status="review")

    def test_held_outcome_dequeues(self):
        self.run_with(status="held")

    def test_dry_run_force_dequeues(self):
        proc = self.run_with(status="ready", force=True)
        self.assertTrue(proc.call_args.kwargs["force"])
        self.assertTrue(proc.call_args.kwargs["manual"])

    def test_exception_dequeues(self):
        self.run_with(side_effect=RuntimeError("catalog down"))


class TestManualOverHttp(LiveServerCase):
    def setUp(self):
        super().setUp()
        CONTROL._queue.clear()

    def tearDown(self):
        CONTROL._queue.clear()
        super().tearDown()

    def test_retry_that_stays_in_review_leaves_the_queue(self):
        os.makedirs(self.lib / "Author" / "Book")
        (self.lib / "Author" / "Book" / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="Author/Book", status="review", title="Book"))
        done = threading.Event()

        def fake(**kw):
            done.set()

        with mock.patch.object(web, "process_unit", side_effect=fake):
            status, body = self.post({"action": "retry", "path": "Author/Book"})
            self.assertEqual(status, 200, body)
            self.assertTrue(done.wait(5))
        for _ in range(50):  # the finally runs just after process_unit returns
            if not CONTROL.queue_snapshot()["count"]:
                break
            threading.Event().wait(0.05)
        self.assertEqual(CONTROL.queue_snapshot()["count"], 0)
        self.assertIn('data-path="Author/Book"', web.render(self.state))


if __name__ == "__main__":
    unittest.main()

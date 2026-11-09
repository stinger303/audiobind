"""v0.14.4 pipeline LOWs: pin message, forced dry run, convert race, ready rows re-audited."""

import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from audiobind import advice, daemon, matching, pipeline, prefetch
from audiobind.catalog import Product
from audiobind.control import CONTROL
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part


def unit():
    return BookUnit(
        path="/library/A/B", name="B", parent="/library/A", parts=[Part("B", ["/library/A/B/01.mp3"])], rel_path="A/B"
    )


def matched_audit(u):
    audit = pipeline.Audit(unit=u)
    audit.match = matching.Match(product=Product(asin="B000000001", title="B", authors=["A"], hours=1.0))
    audit.confidence = "LOW"
    audit.action = "review"
    return audit


class TestFailedPinCopy(unittest.TestCase):
    def test_message_says_nothing_was_matched(self):
        why, what = advice.explain("review", "NONE", detail={"match": {"asin_pin_failed": "B0DEADPIN0"}})
        self.assertIn("B0DEADPIN0", why)
        self.assertIn("nothing was matched", why)
        self.assertNotIn("search result", why)

    def test_a_dead_pin_never_searches(self):
        with (
            mock.patch.object(matching, "match_from_asin", return_value=matching.Match()),
            mock.patch.object(matching.catalog, "search", side_effect=AssertionError("searched")),
        ):
            m = matching.match_unit(unit(), "B0DEADPIN0")
        self.assertEqual(m.asin_pin_failed, "B0DEADPIN0")
        self.assertIsNone(m.product)

    def test_an_unpinned_search_never_reports_a_failed_pin(self):
        with mock.patch.object(matching.catalog, "search", return_value=[]):
            m = matching.match_unit(unit(), "")
        self.assertEqual(m.asin_pin_failed, "")


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        self.state = State(self.root / "state")
        self.saved_dry = CONTROL._dry_run

    def tearDown(self):
        CONTROL._dry_run = self.saved_dry
        self.state.close()
        self.dir.cleanup()


class TestForcedDryRun(StateCase):
    def run_forced(self, out_exists):
        u = unit()
        out = self.root / "out" / "B.m4b"
        if out_exists:
            out.parent.mkdir(parents=True)
            out.write_text("m4b")
        CONTROL.set_dry_run(True)
        with (
            mock.patch.object(pipeline, "audit_unit", return_value=matched_audit(u)),
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "B"}),
            mock.patch.object(pipeline.library, "m4b_path", return_value=out),
            mock.patch.object(pipeline.encode, "encode", side_effect=AssertionError("encoded in dry run")),
        ):
            pipeline.process_unit(self.state, u, log=lambda *_: None, force=True, manual=True)
        return self.state.get("A/B")

    def test_convert_anyway_in_dry_run_records_a_dry_run(self):
        row = self.run_forced(out_exists=False)
        self.assertEqual(row.status, "ready")
        self.assertIn("dry run", row.note)
        self.assertNotIn("already published", row.note)

    def test_already_published_is_still_reported_as_such(self):
        row = self.run_forced(out_exists=True)
        self.assertEqual(row.status, "ready")
        self.assertIn("already published", row.note)
        self.assertTrue(row.output.endswith("B.m4b"))


class TestConvertRace(StateCase):
    def test_output_appearing_while_waiting_for_the_lock_is_not_encoded_again(self):
        u = unit()
        audit = matched_audit(u)
        out = self.root / "out" / "B.m4b"
        CONTROL.set_dry_run(False)
        result = {}

        def worker():
            result["value"] = pipeline.convert_unit(audit, log=lambda *_: None)

        with (
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "B"}),
            mock.patch.object(pipeline.library, "m4b_path", return_value=out),
            mock.patch.object(pipeline.encode, "encode", side_effect=AssertionError("encoded twice")),
        ):
            pipeline.CONVERT_LOCK.acquire()  # the other conversion of this book is running
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(0.3)
            self.assertTrue(thread.is_alive())  # waiting on the lock
            out.parent.mkdir(parents=True)
            out.write_text("m4b")  # ...and that conversion publishes the book
            pipeline.CONVERT_LOCK.release()
            thread.join(5)
        self.assertIsNone(result["value"])
        self.assertTrue(audit.already_published)
        self.assertFalse(CONTROL.running)


class TestReadyRowsSkipped(StateCase):
    def test_ready_row_with_existing_output_is_decided(self):
        out = self.root / "B.m4b"
        out.write_text("m4b")
        CONTROL.set_dry_run(False)
        self.assertTrue(prefetch.already_decided(UnitState(path="A/B", status="ready", output=str(out))))

    def test_ready_row_whose_output_vanished_is_rechecked(self):
        CONTROL.set_dry_run(False)
        row = UnitState(path="A/B", status="ready", output=str(self.root / "gone.m4b"))
        self.assertFalse(prefetch.already_decided(row))

    def test_dry_run_ready_row_is_rechecked(self):
        CONTROL.set_dry_run(False)
        self.assertFalse(prefetch.already_decided(UnitState(path="A/B", status="ready", note="dry run")))

    def test_ready_rows_are_rechecked_while_dry_run_is_on(self):
        out = self.root / "B.m4b"
        out.write_text("m4b")
        CONTROL.set_dry_run(True)
        self.assertFalse(prefetch.already_decided(UnitState(path="A/B", status="ready", output=str(out))))

    def test_scan_skips_a_published_ready_row(self):
        out = self.root / "B.m4b"
        out.write_text("m4b")
        CONTROL.set_dry_run(False)
        self.state.save(UnitState(path="A/B", status="ready", output=str(out)))
        with (
            mock.patch.object(daemon, "discover_units", return_value=[unit()]),
            mock.patch.object(daemon, "process_unit", side_effect=AssertionError("re-audited")),
        ):
            summary = daemon.scan_once(self.state)
        self.assertEqual(summary["seen"], 0)


if __name__ == "__main__":
    unittest.main()

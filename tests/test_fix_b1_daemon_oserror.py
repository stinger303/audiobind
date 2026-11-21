"""B1: an OSError from a library book is one failed book, not the end of the scan pass.

Red on 9c9cae1: the library branch of scan_once called process_unit with no guard, so the
exception left the loop and the remaining books were never looked at.
"""

import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import daemon, ingest
from audiobind.state import State, UnitState
from audiobind.units import BookUnit


def library_unit(rel: str) -> BookUnit:
    return BookUnit(
        path=f"/library/{rel}", name=rel.split("/")[-1], parent=rel.split("/")[0], parts=[], rel_path=rel
    )


class TestB1LibraryOSErrorIsOneFailedBook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.state.close)
        self.units = [library_unit("A/One"), library_unit("A/Two"), library_unit("A/Three")]

    def scan(self, process):
        with (
            mock.patch.object(daemon, "discover_units", return_value=self.units),
            mock.patch.object(ingest, "discover", return_value=[]),
            mock.patch.object(daemon.PREFETCH, "schedule"),
            mock.patch.object(daemon, "process_unit", side_effect=process),
        ):
            return daemon.scan_once(self.state)

    def test_the_third_book_still_runs_and_the_second_is_recorded_failed(self):
        calls = []

        def process(state, unit, log=print, **_kw):
            calls.append(unit.rel_path)
            if unit.rel_path == "A/Two":
                raise OSError("disk went away")
            state.save(UnitState(path=unit.state_key, status="review"))

        with self.assertLogs("audiobind", level="WARNING") as logs:
            summary = self.scan(process)

        self.assertEqual(calls, ["A/One", "A/Two", "A/Three"])
        self.assertEqual(self.state.get("A/Three").status, "review")
        row = self.state.get("A/Two")
        self.assertIsNotNone(row, "the book that raised has no row at all")
        self.assertEqual(row.status, "failed")
        self.assertIn("disk went away", row.note)
        self.assertLessEqual(len(row.note), 300)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(any("A/Two" in line and "disk went away" in line for line in logs.output))

    def test_a_failed_book_keeps_what_its_row_already_knew(self):
        # a stopped re-encode (retried next scan) of a book already published, whose source then
        # vanished: the published output is still recorded
        self.state.save(UnitState(path="A/Two", status="interrupted", title="Two", output="/out/Two.m4b"))

        def process(state, unit, log=print, **_kw):
            if unit.rel_path == "A/Two":
                raise FileNotFoundError(2, "No such file or directory", "/library/A/Two/01.mp3")
            state.save(UnitState(path=unit.state_key, status="review"))

        with self.assertLogs("audiobind", level="WARNING"):
            self.scan(process)
        row = self.state.get("A/Two")
        self.assertEqual(row.status, "failed")
        self.assertEqual((row.title, row.output), ("Two", "/out/Two.m4b"))

    def test_a_stop_and_ctrl_c_still_propagate(self):
        from audiobind.control import Stopped

        for exc in (Stopped("stopped by operator"), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):

                def process(state, unit, log=print, _exc=exc, **_kw):
                    raise _exc

                with self.assertRaises(type(exc)):
                    self.scan(process)


if __name__ == "__main__":
    unittest.main()

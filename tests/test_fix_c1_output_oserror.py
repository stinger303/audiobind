"""C1: the B1 guard fails a book only for an error about its *source*; an output-side OSError ends
the pass as it did before B1, and no row's status is rewritten on a guess.

Red on 4945ad7: scan_once caught every OSError from process_unit and recorded it as "a source file
could not be read" - an EIO from the output mount (missing_beside stats the published folder)
flipped a converted book to failed.
"""

import contextlib
import errno
import unittest
from unittest import mock

from audiobind import catalog, daemon, encode, pipeline
from audiobind.config import CONFIG
from audiobind.probe import Probe
from audiobind.state import UnitState

try:
    from test_ingest_discovery import touch
    from test_ingest_pipeline import PHM, PipelineCase
except ImportError:
    from tests.test_ingest_discovery import touch
    from tests.test_ingest_pipeline import PHM, PipelineCase

REL = "Andy Weir/Project Hail Mary"


class TestC1OutputErrorIsNotASourceFailure(PipelineCase):
    def setUp(self):
        super().setUp()
        self.src = CONFIG.library_root / REL
        for name in ("01.mp3", "02.mp3"):
            touch(str(self.src / name))
        self.m4b = CONFIG.output_root / "Andy Weir" / "Project Hail Mary" / "Project Hail Mary.m4b"
        touch(str(self.m4b), b"m4b")
        self.state.save(UnitState(path=REL, status="converted", output=str(self.m4b),
                                  title="Project Hail Mary", confidence="HIGH", asin=PHM.asin,
                                  note="converted earlier"))

    @contextlib.contextmanager
    def patched(self):
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe",
                              return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            yield

    def test_an_eio_on_the_output_mount_aborts_the_pass_and_leaves_the_row(self):
        before = self.state.get(REL)
        eio = OSError(errno.EIO, "Input/output error", str(self.m4b.with_suffix(".mp3")))
        with self.patched(), mock.patch.object(pipeline, "missing_beside", side_effect=eio), \
                self.assertLogs("audiobind", level="ERROR"):
            with self.assertRaises(OSError) as ctx:
                daemon.scan_once(self.state, force=True)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        row = self.state.get(REL)
        self.assertEqual(row.status, "converted")
        self.assertEqual(row.note, before.note)
        self.assertEqual(row.output, str(self.m4b))

    def test_an_error_with_no_path_is_not_guessed_to_be_the_source(self):
        stale = OSError(errno.ESTALE, "Stale file handle")
        with self.patched(), mock.patch.object(pipeline, "missing_beside", side_effect=stale), \
                self.assertLogs("audiobind", level="ERROR"):
            with self.assertRaises(OSError):
                daemon.scan_once(self.state, force=True)
        self.assertEqual(self.state.get(REL).status, "converted")

    def test_a_missing_source_file_still_fails_only_that_book(self):
        other = "Someone Else/Other Book"
        touch(str(CONFIG.library_root / other / "01.mp3"))
        gone = str(self.src / "02.mp3")
        seen = []

        def process(state, unit, log=print, **_kw):
            seen.append(unit.rel_path)
            if unit.rel_path == REL:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", gone)
            state.save(UnitState(path=unit.state_key, status="review"))

        with mock.patch.object(daemon, "process_unit", side_effect=process), \
                self.assertLogs("audiobind", level="WARNING"):
            summary = daemon.scan_once(self.state, force=True)
        self.assertEqual(sorted(seen), sorted([REL, other]))
        row = self.state.get(REL)
        self.assertEqual(row.status, "failed")
        self.assertIn("a source file could not be read", row.note)
        self.assertEqual(self.state.get(other).status, "review")
        self.assertEqual(summary["failed"], 1)

    def test_classification_is_by_path_not_by_message(self):
        unit = next(u for u in daemon.discover_units(CONFIG.library_root, []) if u.rel_path == REL)
        src_file = str(self.src / "01.mp3")
        cases = [
            (FileNotFoundError(errno.ENOENT, "gone", src_file), True),
            (PermissionError(errno.EACCES, "denied", src_file), True),
            (OSError(errno.EIO, "io", src_file), True),
            (OSError(errno.ESTALE, "stale", str(self.src)), True),
            (OSError(errno.EIO, "a source file could not be read", str(self.m4b)), False),
            (FileNotFoundError(errno.ENOENT, "gone", str(self.src) + "-sibling/01.mp3"), False),
            (OSError(errno.EIO, "no path at all"), False),
        ]
        for exc, expected in cases:
            with self.subTest(exc=exc):
                self.assertIs(daemon.source_side_error(unit, exc), expected)


if __name__ == "__main__":
    unittest.main()

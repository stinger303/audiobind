"""C3: a book Audiobind declines to verify (B2: one file could not be measured) is held, not failed,
and the next ordinary scan still offers it.

Red on 4945ad7: encode._encode's B2 refusal raised a plain RuntimeError, process_unit recorded the
book "failed", and failed rows are skipped by ordinary scans - the book silently stopped coming back.

The refusal is only reachable through Convert anyway (force): an ordinary scan already stops a book
with an unreadable file at review. The refusal itself is unchanged: no ffmpeg is started and nothing
is published.
"""

import contextlib
import unittest
from unittest import mock

from audiobind import catalog, daemon, encode, pipeline
from audiobind.config import CONFIG
from audiobind.probe import Probe

try:
    from test_ingest_discovery import touch
    from test_ingest_pipeline import PHM, PipelineCase
except ImportError:
    from tests.test_ingest_discovery import touch
    from tests.test_ingest_pipeline import PHM, PipelineCase

REL = "Andy Weir/Project Hail Mary"
BROKEN = "02 Broken.mp3"


class TestC3UnmeasuredBookIsHeldAndOffered(PipelineCase):
    def setUp(self):
        super().setUp()
        for name in ("01.mp3", BROKEN, "03.mp3"):
            touch(str(CONFIG.library_root / REL / name))
        self.audited = []
        self.launched = []

    def probe(self, path, **_kw):
        # ffprobe failed on the broken file for a reason other than a timeout: Probe() -> 0.0 s
        if path.endswith(BROKEN):
            return Probe()
        return Probe(duration=3600.0, codec="mp3", channels="2")

    @contextlib.contextmanager
    def patched(self):
        real_audit = pipeline.audit_unit

        def audit(unit, *a, **kw):
            self.audited.append(unit.state_key)
            return real_audit(unit, *a, **kw)

        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", side_effect=self.probe),
            mock.patch.object(encode, "probe", side_effect=self.probe),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode.subprocess, "Popen",
                              side_effect=lambda cmd, **kw: self.launched.append(cmd)),
            mock.patch.object(pipeline, "audit_unit", side_effect=audit),
        ):
            yield

    def unit(self):
        return next(u for u in daemon.discover_units(CONFIG.library_root, []) if u.rel_path == REL)

    def convert_anyway(self):
        """What the Convert anyway button runs (web.run_manual)."""
        pipeline.process_unit(self.state, self.unit(), log=lambda *_: None, force=True, manual=True)

    def test_the_refusal_is_held_with_its_reason(self):
        with self.patched():
            self.convert_anyway()
        self.assertEqual([c for c in self.launched if c and c[0] == encode.FFMPEG], [],
                         "the refusal no longer happens before ffmpeg")
        row = self.state.get(REL)
        self.assertEqual(row.status, "held", row.note)
        self.assertIn(BROKEN, row.note)
        self.assertIn("could not be measured", row.note)
        self.assertIn("not converted", row.note.lower())
        self.assertLessEqual(len(row.note), 300)
        self.assertFalse(row.output)

    def test_a_second_ordinary_scan_still_offers_it(self):
        with self.patched():
            self.convert_anyway()
            self.assertEqual(self.state.get(REL).status, "held")
            self.audited.clear()
            daemon.scan_once(self.state)  # an ordinary scan: not forced
        self.assertEqual(self.audited, [REL], "the held book was skipped by the next scan")
        # the file is still broken: the ordinary scan's own unreadable-file check decides it
        self.assertEqual(self.state.get(REL).status, "review")

    def test_once_the_file_is_fixed_the_next_scan_converts_it(self):
        with self.patched():
            self.convert_anyway()
        fixed = []

        def fake_encode(unit, match, book, work_dir, out_path, **kw):
            fixed.append(unit.state_key)
            return self.fake_encode(unit, match, book, work_dir, out_path, **kw)

        whole = Probe(duration=2400.0, codec="mp3", channels="2")  # the broken file was replaced
        with self.patched(), mock.patch.object(pipeline, "probe", return_value=whole), \
                mock.patch.object(encode, "encode", side_effect=fake_encode):
            daemon.scan_once(self.state)
        self.assertEqual(fixed, [REL])
        self.assertEqual(self.state.get(REL).status, "converted")

    def test_an_ordinary_held_row_is_still_skipped(self):
        # the carve-out is for the refusal only: a runtime hold stays decided
        from audiobind.prefetch import already_decided
        from audiobind.state import UnitState

        self.assertTrue(already_decided(UnitState(path=REL, status="held", note="runtime disagrees")))
        self.assertTrue(already_decided(UnitState(path=REL, status="held",
                                                  note="could not be measured", detail="{}")))


if __name__ == "__main__":
    unittest.main()

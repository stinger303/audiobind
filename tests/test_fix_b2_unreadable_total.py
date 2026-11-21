"""B2: a file ffprobe could not read leaves the book's length unknown, never shorter.

Red on 9c9cae1: the unreadable file's 0 s was summed into the plan's total, the encode was run, and
the duration check then blamed the merge ("files were dropped") for a file that was never measured.
"""

import pathlib
import unittest
from unittest import mock

from audiobind import encode
from audiobind.units import BookUnit, Part
from tests.test_encode_correctness import EncodeHarness, FakeProc

FILES = ["/src/Book/01.mp3", "/src/Book/02 Broken.mp3", "/src/Book/03.mp3"]


class TestB2UnreadableFileIsNamed(EncodeHarness):
    def setUp(self):
        super().setUp()
        self.unit = BookUnit(path="/src/Book", name="Book", parent="", parts=[Part("Book", list(FILES))])
        self.out = self.root / "out" / "Book.m4b"
        self.launched = []

    def plan(self, total):
        # the middle file: ffprobe failed for a reason other than a timeout (Probe() -> 0.0)
        return encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": 7200.0, "title": "a"}],
            source="per-file (1 chapters)",
            durations={FILES[0]: 3600.0, FILES[1]: 0.0, FILES[2]: 3600.0},
        )

    def encode_book(self):
        def popen(cmd, **kw):
            self.launched.append(cmd)
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return FakeProc()

        # ffmpeg itself decodes all three files: the output is the full 3 h
        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=10800.0)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": "aac"}]),
        ):
            return encode.encode(self.unit, self.match, {"title": "T"}, self.work, self.out,
                                 log=lambda *_: None, plan=self.plan(0), publish=True)

    def test_the_note_names_the_unreadable_file_not_a_dropped_one(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.encode_book()
        note = str(ctx.exception)[:300]  # what process_unit records on the row
        self.assertIn("02 Broken.mp3", note)
        self.assertNotIn("dropped", note.lower())
        self.assertFalse(self.out.exists(), "a book with an unmeasured file was published")

    def test_no_encode_is_started_for_a_length_nobody_can_check(self):
        with self.assertRaises(RuntimeError):
            self.encode_book()
        self.assertEqual(self.launched, [])

    def test_the_plan_total_is_unknown_not_short(self):
        plan = self.plan(0)
        self.assertIsNone(plan.total)
        self.assertEqual(plan.unmeasured, [FILES[1]])
        whole = encode.ChapterPlan(chapters=[], source="", durations={FILES[0]: 3600.0, FILES[2]: 3600.0})
        self.assertEqual((whole.total, whole.unmeasured), (7200.0, []))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the HIGH-impact defects of the v0.24.0 audit (AB-FIX-A), one class per ID."""

import contextlib
import pathlib
import unittest
from unittest import mock

from audiobind import catalog, encode, ingest, matching, pipeline
from audiobind.probe import Probe
from audiobind.units import BookUnit, Part

try:
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase


# --------------------------------------------------------------------------- A2 + A3
class CollectionShapeTests(unittest.TestCase):
    """looks_like_collection must not read one long book as "several whole books"."""

    def audit(self, parts: list[Part], hours_by_file: dict[str, float]):
        unit = BookUnit(path="/lib/Some Author/Some Book", name="Some Book", parent="/lib/Some Author",
                        parts=parts)

        def fake_probe(path, raise_on_timeout=False):
            return Probe(duration=hours_by_file[path] * 3600, codec="mp3", channels="2")

        with (
            mock.patch.object(pipeline, "probe", side_effect=fake_probe),
            mock.patch.object(matching, "match_unit", return_value=matching.Match()),  # NONE
        ):
            return pipeline.audit_unit(unit)

    def test_a2_one_long_file_plus_a_short_extra_is_not_a_collection(self):
        # a 15 h book and a 2 h bonus file: 17 h / 2 files = 8.5 h "per book" under the old rule
        files = {"/b/book.mp3": 15.0, "/b/bonus.mp3": 2.0}
        audit = self.audit([Part(name="", files=list(files))], files)
        self.assertEqual(audit.confidence, "NONE")

    def test_a3_a_multi_disc_novel_is_not_a_collection(self):
        # the audit's example: Disc 1/2/3 of a 25 h novel, one file per disc (8.3 h each)
        files = {f"/b/Disc {n}/d{n}.mp3": 25.0 / 3 for n in (1, 2, 3)}
        parts = [Part(name=f"Disc {n}", files=[f"/b/Disc {n}/d{n}.mp3"]) for n in (1, 2, 3)]
        audit = self.audit(parts, files)
        self.assertEqual(audit.confidence, "NONE")

    def test_a_flat_folder_of_whole_books_is_still_a_collection(self):
        files = {f"/b/book{n}.mp3": 10.0 for n in (1, 2, 3)}
        audit = self.audit([Part(name="", files=list(files))], files)
        self.assertEqual(audit.confidence, "COLLECTION")

    def test_parts_that_are_each_far_longer_than_one_book_are_still_a_collection(self):
        files = {f"/b/Vol {n}/v{n}.mp3": 20.0 for n in (1, 2, 3)}
        parts = [Part(name=f"Vol {n}", files=[f"/b/Vol {n}/v{n}.mp3"]) for n in (1, 2, 3)]
        audit = self.audit(parts, files)
        self.assertEqual(audit.confidence, "COLLECTION")


# --------------------------------------------------------------------------- A4
class ConvertAnywayAtCurrentLayoutTests(PipelineCase):
    """Convert anyway must re-encode a book whose output already sits at the current layout path."""

    @contextlib.contextmanager
    def patched(self):
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            yield

    def converted_at_current_layout(self) -> pathlib.Path:
        self.add_torrent()
        with self.patched():
            pipeline.process_unit(self.state, ingest.find_any_unit(KEY), log=lambda *_: None)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        out = pathlib.Path(row.output)
        out.write_bytes(b"the previous encode")
        self.encode_calls.clear()
        return out

    def test_a4_convert_anyway_re_encodes_over_the_existing_output(self):
        out = self.converted_at_current_layout()
        logs = []
        with self.patched():
            pipeline.process_unit(self.state, ingest.find_any_unit(KEY), log=logs.append,
                                  force=True, manual=True, reencode=True)
        self.assertEqual(len(self.encode_calls), 1, logs)
        self.assertEqual(out.read_bytes(), b"m4b")  # the new encode replaced the old file
        row = self.state.get(KEY)
        self.assertEqual((row.status, row.output), ("converted", str(out)))
        self.assertFalse(any("already published" in m for m in logs), logs)

    def test_a_manual_retry_still_leaves_the_existing_output_alone(self):
        out = self.converted_at_current_layout()
        with self.patched():
            pipeline.process_unit(self.state, ingest.find_any_unit(KEY), log=lambda *_: None, manual=True)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual(out.read_bytes(), b"the previous encode")

    def test_a_forced_recheck_without_reencode_does_not_encode(self):
        out = self.converted_at_current_layout()
        with self.patched():
            pipeline.process_unit(self.state, ingest.find_any_unit(KEY), log=lambda *_: None, force=True)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual(out.read_bytes(), b"the previous encode")


if __name__ == "__main__":
    unittest.main()

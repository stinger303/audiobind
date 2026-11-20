"""Regression tests for the HIGH-impact defects of the v0.24.0 audit (AB-FIX-A), one class per ID."""

import unittest
from unittest import mock

from audiobind import matching, pipeline
from audiobind.probe import Probe
from audiobind.units import BookUnit, Part


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


if __name__ == "__main__":
    unittest.main()

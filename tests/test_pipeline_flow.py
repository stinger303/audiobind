"""0.21.0-dev10: pipeline flow fixes in audiobind/pipeline.py.

1. An ingest unit whose folder name matched only at LOW is also re-checked against its embedded
   tags, and the tags' match replaces the folder's only when it is genuinely better.
"""

import unittest
from unittest import mock

from audiobind import ingest, matching, pipeline
from audiobind.catalog import Product
from audiobind.probe import Probe
from audiobind.state import SOURCE_INGEST
from audiobind.units import BookUnit, Part

WRONG = Product(asin="B0WRONG001", title="Project Hail Mary Companion", authors=["Other Person"], hours=2.0)
RIGHT = Product(asin="B08G9PRS1K", title="Project Hail Mary", authors=["Andy Weir"], hours=2.0)
OTHER = Product(asin="B0OTHER001", title="Hail Mary Pass", authors=["Some Coach"], hours=2.0)

EMBEDDED_NOTE = "matched on the audio file's embedded tags"


def ingest_unit(source=SOURCE_INGEST):
    path = "/ingest/Weir.PHM.2021.MP3-GRP/01.mp3"
    return BookUnit(
        path="/ingest/Weir.PHM.2021.MP3-GRP",
        name="Weir.PHM.2021.MP3-GRP",
        parent="/ingest",
        parts=[Part(name="Weir.PHM.2021.MP3-GRP", files=[path])],
        author_guess="Weir",
        title_guess="PHM",
        rel_path="Weir.PHM.2021.MP3-GRP",
        source=source,
    )


class TestEmbeddedRetryOnLow(unittest.TestCase):
    def audit(self, folder, embedded, unit=None, asin_override=""):
        """audit_unit with the folder search answering ``folder`` and the tag search ``embedded``."""
        unit = unit or ingest_unit()
        calls = []

        def match_unit(u, override="", local_hours=0.0):
            calls.append(u)
            return folder if len(calls) == 1 else embedded

        with (
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=7200.0, codec="mp3", channels="2")),
            mock.patch.object(pipeline.os.path, "getsize", return_value=1000),
            mock.patch.object(pipeline.matching, "match_unit", side_effect=match_unit),
            mock.patch.object(ingest, "embedded_names", return_value=("Andy Weir", "Project Hail Mary")),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
        ):
            audit = pipeline.audit_unit(unit, asin_override)
        return audit, calls

    def test_a_better_embedded_match_replaces_a_low_folder_match(self):
        folder = matching.Match(product=WRONG, confidence="LOW", score=0.55)
        embedded = matching.Match(product=RIGHT, confidence="HIGH", score=0.97)
        audit, calls = self.audit(folder, embedded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(audit.match.asin, RIGHT.asin)
        self.assertEqual(audit.confidence, "HIGH")
        self.assertEqual(audit.name_source, pipeline.NAME_SOURCE_EMBEDDED)
        self.assertIn(EMBEDDED_NOTE, audit.notes)

    def test_a_worse_embedded_match_keeps_the_low_folder_match(self):
        folder = matching.Match(product=WRONG, confidence="LOW", score=0.60)
        embedded = matching.Match(product=OTHER, confidence="LOW", score=0.62)  # not clearly better
        audit, calls = self.audit(folder, embedded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(audit.match.asin, WRONG.asin)
        self.assertEqual(audit.confidence, "LOW")
        self.assertEqual(audit.name_source, pipeline.NAME_SOURCE_FOLDER)
        self.assertNotIn(EMBEDDED_NOTE, audit.notes)
        self.assertTrue(any("kept the folder match" in n for n in audit.notes), audit.notes)

    def test_same_tier_with_a_clearly_better_score_is_adopted(self):
        folder = matching.Match(product=WRONG, confidence="LOW", score=0.50)
        embedded = matching.Match(product=RIGHT, confidence="LOW", score=0.75)
        audit, _ = self.audit(folder, embedded)
        self.assertEqual(audit.match.asin, RIGHT.asin)
        self.assertIn(EMBEDDED_NOTE, audit.notes)

    def test_the_tags_finding_nothing_keeps_the_low_folder_match_silently(self):
        folder = matching.Match(product=WRONG, confidence="LOW", score=0.60)
        audit, calls = self.audit(folder, matching.Match())
        self.assertEqual(len(calls), 2)
        self.assertEqual(audit.match.asin, WRONG.asin)
        self.assertEqual(audit.name_source, pipeline.NAME_SOURCE_FOLDER)
        self.assertFalse(any("embedded" in n for n in audit.notes), audit.notes)

    def test_none_still_takes_any_embedded_match(self):
        embedded = matching.Match(product=RIGHT, confidence="LOW", score=0.40)
        audit, calls = self.audit(matching.Match(), embedded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(audit.match.asin, RIGHT.asin)
        self.assertEqual(audit.name_source, pipeline.NAME_SOURCE_EMBEDDED)
        self.assertIn(EMBEDDED_NOTE, audit.notes)

    def test_medium_and_high_folder_matches_never_ask_the_tags(self):
        for tier in ("MEDIUM", "HIGH"):
            folder = matching.Match(product=RIGHT, confidence=tier, score=0.9)
            audit, calls = self.audit(folder, matching.Match(product=OTHER, confidence="HIGH", score=1.0))
            self.assertEqual(len(calls), 1, tier)
            self.assertEqual(audit.name_source, pipeline.NAME_SOURCE_FOLDER)

    def test_an_asin_override_or_a_library_unit_never_asks_the_tags(self):
        folder = matching.Match(product=WRONG, confidence="LOW", score=0.5)
        better = matching.Match(product=RIGHT, confidence="HIGH", score=0.97)
        _, calls = self.audit(folder, better, asin_override="B0WRONG001")
        self.assertEqual(len(calls), 1)
        _, calls = self.audit(folder, better, unit=ingest_unit(source="library"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

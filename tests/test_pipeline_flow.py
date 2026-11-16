"""0.21.0-dev10: pipeline flow fixes in audiobind/pipeline.py.

1. An ingest unit whose folder name matched only at LOW is also re-checked against its embedded
   tags, and the tags' match replaces the folder's only when it is genuinely better.
2. A book's work directory has the same name in every process, and is emptied before an encode,
   so a crashed run's leftovers are found, removed and never mistaken for new output.
"""

import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from audiobind import catalog, encode, ingest, matching, pipeline
from audiobind.config import CONFIG
from audiobind.catalog import Product
from audiobind.probe import Probe
from audiobind.state import SOURCE_INGEST
from audiobind.units import BookUnit, Part

try:
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase

REPO = pathlib.Path(__file__).resolve().parent.parent

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


class TestWorkDir(PipelineCase):
    def convert(self):
        unit = ingest.find_any_unit(KEY)
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=PHM, confidence="HIGH"), action="convert")
        with (
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "plan_chapters", return_value=mock.Mock(chapters=[])),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            return pipeline.convert_unit(audit, log=lambda *_: None)

    def test_the_same_book_gets_the_same_directory_on_every_call(self):
        self.add_torrent()
        self.convert()
        for out in CONFIG.output_root.rglob("*.m4b"):
            out.unlink()  # so the second call encodes again instead of "already published"
        self.convert()
        first, second = (call["work_dir"].parent for call in self.encode_calls)
        self.assertEqual(first, second)
        self.assertEqual(first, pipeline.work_dir_for(ingest.find_any_unit(KEY)))
        self.assertFalse(first.exists())  # and the finally still removes it

    def test_the_name_does_not_depend_on_the_process_hash_seed(self):
        code = (
            "import sys; from audiobind import pipeline; from audiobind.units import BookUnit;"
            "u = BookUnit(path='', name='', parent='', parts=[], rel_path=sys.argv[1], source='ingest');"
            "print(pipeline.work_dir_for(u).name)"
        )
        names = set()
        for seed in ("1", "2", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", code, "Some Book"], cwd=REPO, env=env,
                                 capture_output=True, text=True, check=True)
            names.add(out.stdout.strip())
        self.assertEqual(len(names), 1, names)

    def test_a_crashed_runs_leftovers_are_gone_before_the_new_encode(self):
        self.add_torrent()
        work = pipeline.work_dir_for(ingest.find_any_unit(KEY))
        stale = work / "m4b" / "output.m4b"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"half an encode from before the crash")
        seen = {}
        real = self.fake_encode

        def encode_checking(unit, match, book, work_dir, out_path, **kw):
            seen["stale_present"] = stale.exists()
            seen["work_dir_present"] = work.exists()
            return real(unit, match, book, work_dir, out_path, **kw)

        self.fake_encode = encode_checking
        self.convert()
        self.assertEqual(seen, {"stale_present": False, "work_dir_present": False})
        self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()

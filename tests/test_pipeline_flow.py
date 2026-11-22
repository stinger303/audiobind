"""0.21.0-dev10: pipeline flow fixes in audiobind/pipeline.py.

1. An ingest unit whose folder name matched only at LOW is also re-checked against its embedded
   tags, and the tags' match replaces the folder's only when it is genuinely better.
2. Every attempt has a private work directory and cleans only its own scratch files.
4. A published file recorded on a row is honoured whatever the row's status: a re-check that ends
   in review/held keeps the output path, and no re-check encodes a duplicate into a newer layout.
5. (Every format still encodes before anything publishes - deliberate.) A later format that
   fails to *publish* takes the formats already placed by that call back out of the library.
"""

import json
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
from audiobind.state import SOURCE_INGEST, UnitState
from audiobind.units import BookUnit, Part

try:
    from live import published_row
    from test_ingest_discovery import touch
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.live import published_row
    from tests.test_ingest_discovery import touch
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

        def match_unit(u, override="", local_hours=0.0, pin_failures=0):
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
            return pipeline.convert_unit(audit, state=self.state, log=lambda *_: None)

    def test_the_same_book_gets_a_private_directory_on_every_call(self):
        self.add_torrent()
        self.convert()
        for out in CONFIG.output_root.rglob("*.m4b"):
            out.unlink()  # so the second call encodes again instead of "already published"
        self.convert()
        first, second = (call["work_dir"].parent for call in self.encode_calls)
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_independent_processes_get_unique_names(self):
        code = "from audiobind import pipeline; print(pipeline.attempt_work_dir().name)"
        names = set()
        for seed in ("1", "2", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                                 capture_output=True, text=True, check=True)
            names.add(out.stdout.strip())
        self.assertEqual(len(names), 3, names)

    def test_a_crashed_runs_leftovers_are_left_alone_by_the_new_encode(self):
        self.add_torrent()
        work = pipeline.attempt_work_dir()
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
        self.assertEqual(seen, {"stale_present": True, "work_dir_present": True})
        self.assertEqual(stale.read_bytes(), b"half an encode from before the crash")


class TestPublishedFileIsHonoured(PipelineCase):
    WEAK = Product(asin="B0WEAK0001", title="Project Hail Mary Companion", authors=["Other Person"],
                   hours=1.0, language="english")

    @property
    def out(self):
        return CONFIG.output_root

    def unit(self):
        return ingest.find_any_unit(KEY)

    def old_file(self):
        old = self.out / "Andy Weir" / "Old Layout Folder" / "Project Hail Mary.m4b"
        touch(str(old), b"old m4b")
        return old

    def old_layout_row(self):
        old = self.old_file()
        detail = json.dumps({"match": {"asin": PHM.asin}})
        self.state.save(published_row(UnitState(path=KEY, status="converted", output=str(old),
                                                confidence="HIGH", asin=PHM.asin, detail=detail)))
        return old

    def run_unit(self, search=None, **kw):
        with (
            mock.patch.object(catalog, "search", return_value=search or [PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None, **kw)
        return self.state.get(KEY)

    def test_an_unverified_ready_row_under_an_older_layout_goes_to_review(self):
        self.add_torrent()
        old = self.old_file()
        self.state.save(UnitState(path=KEY, status="ready", output=str(old), source="ingest",
                                  confidence="HIGH", asin=PHM.asin))
        row = self.run_unit(manual=True)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual((row.status, row.output), ("review", str(old)))
        self.assertEqual(list(self.out.rglob("*.m4b")), [old])  # no duplicate in the new layout

    def test_a_converted_book_rechecked_into_review_keeps_its_output(self):
        self.add_torrent()
        old = self.old_layout_row()
        row = self.run_unit(search=[self.WEAK], manual=True)
        self.assertEqual(row.status, "review")
        self.assertEqual(row.output, str(old))  # the row still agrees with the disk
        self.assertTrue(old.exists())
        # ... and a later Retry that matches again does not encode a second copy
        row = self.run_unit(manual=True)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual((row.status, row.output), ("ready", str(old)))
        self.assertEqual(list(self.out.rglob("*.m4b")), [old])

    def test_a_row_whose_file_is_gone_is_not_given_a_stale_output(self):
        self.add_torrent()
        self.old_layout_row().unlink()
        row = self.run_unit(search=[self.WEAK], manual=True)
        self.assertEqual((row.status, row.output), ("review", ""))

    def test_convert_anyway_still_re_encodes_a_ready_row(self):
        self.add_torrent()
        old = self.old_file()
        self.state.save(UnitState(path=KEY, status="ready", output=str(old), source="ingest",
                                  confidence="HIGH", asin=PHM.asin))
        row = self.run_unit(force=True, manual=True, reencode=True)
        self.assertEqual(len(self.encode_calls), 1)
        self.assertEqual(row.status, "converted")
        self.assertNotEqual(row.output, str(old))


class TestAllFormatsOrNone(PipelineCase):
    """m4b+mp3 with the real publish_result; only the file placement is made to fail."""

    def convert(self, fail_ext=None, setting="M4B + MP3"):
        unit = ingest.find_any_unit(KEY)
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=PHM, confidence="HIGH"), action="convert")
        cover = self.root / "cover-src.jpg"
        cover.write_bytes(b"jpg")
        self.work_dirs = []

        def fake_encode(unit, match, book, work_dir, out_path, **kw):
            work_dir.mkdir(parents=True, exist_ok=True)
            self.work_dirs.append(work_dir)
            temp = work_dir / f"output.{kw['fmt']}"
            temp.write_bytes(kw["fmt"].encode())
            return encode.EncodeResult(path=temp, duration=7200.0, size=3, chapters=2,
                                       chapters_source="test", cover=cover)

        real_place = encode._place

        def place(source, dest, move):
            if fail_ext and dest.suffix == fail_ext:
                raise OSError(28, "No space left on device")
            return real_place(source, dest, move)

        with (
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(pipeline, "effective_val",
                              side_effect=lambda k: setting if k == "output_format" else None),
            mock.patch.object(encode, "plan_chapters", return_value=mock.Mock(chapters=[])),
            mock.patch.object(encode, "encode", side_effect=fake_encode),
            mock.patch.object(encode, "_place", side_effect=place),
        ):
            return pipeline.convert_unit(audit, state=self.state, log=lambda *_: None)

    def library_files(self):
        return sorted(p.relative_to(CONFIG.output_root).as_posix()
                      for p in CONFIG.output_root.rglob("*") if p.is_file())

    def test_both_formats_publish_together(self):
        self.add_torrent()
        self.convert()
        files = self.library_files()
        self.assertTrue(any(f.endswith(".m4b") for f in files), files)
        self.assertTrue(any(f.endswith(".mp3") for f in files), files)

    def test_a_failed_mp3_publish_takes_the_m4b_back_out(self):
        self.add_torrent()
        CONFIG.output_root.mkdir(parents=True, exist_ok=True)
        before = set(CONFIG.output_root.rglob("*"))
        with self.assertRaises(OSError):
            self.convert(fail_ext=".mp3")
        self.assertEqual(self.library_files(), [])  # no m4b, no cover.jpg
        self.assertEqual(set(CONFIG.output_root.rglob("*")), before)  # no stray author/title folders
        self.assertTrue(self.work_dirs)
        self.assertTrue(all(not w.exists() for w in self.work_dirs))  # the work dir is gone too

    def test_an_unowned_cover_and_notes_are_left_in_place(self):
        self.add_torrent()
        # A cover and notes the owner already had in the book's folder.
        folder = CONFIG.output_root / "Andy Weir" / "Project Hail Mary"
        folder.mkdir(parents=True)
        (folder / "cover.jpg").write_bytes(b"theirs")
        (folder / "notes.txt").write_bytes(b"theirs")
        self.assertIsNone(self.convert())
        self.assertEqual((folder / "cover.jpg").read_bytes(), b"theirs")
        self.assertEqual((folder / "notes.txt").read_bytes(), b"theirs")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["cover.jpg", "notes.txt"])


if __name__ == "__main__":
    unittest.main()

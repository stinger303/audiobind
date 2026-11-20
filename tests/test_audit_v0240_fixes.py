"""Regression tests for the HIGH-impact defects of the v0.24.0 audit (AB-FIX-A), one class per ID."""

import contextlib
import http.client
import io
import os
import pathlib
import unittest
from unittest import mock

from audiobind import catalog, daemon, encode, ingest, matching, pipeline
from audiobind.config import CONFIG
from audiobind.probe import Probe
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part

try:
    from live import LiveServerCase
    from test_ingest_discovery import touch
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.live import LiveServerCase
    from tests.test_ingest_discovery import touch
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


class ForcedConvertKeepsThePreviousEditionTests(PipelineCase):
    """AB-FIX-A2 #1: a forced convert over an existing output moves the old files aside first.

    The output path comes from catalog metadata alone, so a *different* source that resolves to
    the same author/title shares it: forcing that second source must not destroy the first
    book's audio, and a failed dual-format publish must leave the first pair exactly as it was.
    """

    OTHER = "Andy Weir - Project Hail Mary (Other Edition) [TRACKER]"

    def setUp(self):
        super().setUp()
        self.add_torrent()
        self.add_torrent(name=self.OTHER)
        self.cover = self.root / "cover-src.jpg"

    def convert(self, key, tag, fail_ext=None, reencode=False, logs=None):
        unit = ingest.find_any_unit(key)
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=PHM, confidence="HIGH"), action="convert")

        def fake_encode(unit, match, book, work_dir, out_path, **kw):
            work_dir.mkdir(parents=True, exist_ok=True)
            temp = work_dir / f"output.{kw['fmt']}"
            temp.write_bytes(f"{tag}-{kw['fmt']}".encode())
            self.cover.write_bytes(f"{tag}-cover".encode())
            return encode.EncodeResult(path=temp, duration=7200.0, size=3, chapters=2,
                                       chapters_source="test", cover=self.cover)

        real_place = encode._place

        def place(source, dest, move):
            if fail_ext and dest.suffix == fail_ext:
                raise OSError(28, "No space left on device")
            return real_place(source, dest, move)

        with (
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(pipeline, "effective_val",
                              side_effect=lambda k: "M4B + MP3" if k == "output_format" else None),
            mock.patch.object(encode, "plan_chapters", return_value=mock.Mock(chapters=[])),
            mock.patch.object(encode, "encode", side_effect=fake_encode),
            mock.patch.object(encode, "_place", side_effect=place),
        ):
            pipeline.convert_unit(audit, log=(logs.append if logs is not None else lambda *_: None),
                                  reencode=reencode)
        return pathlib.Path(audit.out_path)

    @staticmethod
    def backups(path: pathlib.Path) -> list[pathlib.Path]:
        return sorted(path.parent.glob(path.name + ".bak-*"))

    def test_forcing_a_different_unit_onto_the_same_path_keeps_the_first_book_recoverable(self):
        m4b = self.convert(KEY, "first")
        mp3 = m4b.with_suffix(".mp3")
        self.assertEqual((m4b.read_bytes(), mp3.read_bytes()), (b"first-m4b", b"first-mp3"))

        logs = []
        self.assertEqual(self.convert("ingest:" + self.OTHER, "second",
                                      reencode=True, logs=logs), m4b)  # the same path
        self.assertEqual((m4b.read_bytes(), mp3.read_bytes()), (b"second-m4b", b"second-mp3"))
        for path, old in ((m4b, b"first-m4b"), (mp3, b"first-mp3"),
                          (m4b.parent / "cover.jpg", b"first-cover")):
            (backup,) = self.backups(path)
            self.assertEqual(backup.read_bytes(), old)
            self.assertTrue(any(str(backup) in m for m in logs), logs)  # a human can find it

    def test_a_failed_dual_format_publish_leaves_the_original_pair_intact(self):
        m4b = self.convert(KEY, "first")
        mp3 = m4b.with_suffix(".mp3")
        cover = m4b.parent / "cover.jpg"
        before = sorted(p.name for p in m4b.parent.iterdir())

        with self.assertRaises(OSError):  # the m4b is replaced, then the mp3 runs out of space
            self.convert("ingest:" + self.OTHER, "second", fail_ext=".mp3", reencode=True)
        self.assertEqual((m4b.read_bytes(), mp3.read_bytes(), cover.read_bytes()),
                         (b"first-m4b", b"first-mp3", b"first-cover"))
        self.assertEqual(sorted(p.name for p in m4b.parent.iterdir()), before)  # backups restored

    def test_nothing_is_backed_up_when_nothing_was_there(self):
        m4b = self.convert(KEY, "first", reencode=True)
        self.assertEqual(sorted(p.name for p in m4b.parent.iterdir()),
                         ["Project Hail Mary.m4b", "Project Hail Mary.mp3", "cover.jpg", "metadata.json"])


# --------------------------------------------------------------------------- A5
class VanishedSourceFileTests(PipelineCase):
    """A library file removed between os.path.exists and os.path.getsize must not abort the scan."""

    VANISHING = "Andy Weir - Artemis"  # sorts before the second book, so the scan has more to do
    SECOND = "Andy Weir - Project Hail Mary"

    def setUp(self):
        super().setUp()
        lib = CONFIG.library_root
        touch(str(lib / self.VANISHING / "01.mp3"), b"x" * 1_000_000)
        touch(str(lib / self.VANISHING / "02.mp3"), b"x" * 2_000_000)
        touch(str(lib / self.SECOND / "01.mp3"), b"x" * 1_000_000)
        self.victim = str(lib / self.VANISHING / "02.mp3")
        real_getsize = os.path.getsize

        def racing_getsize(path):
            if os.fspath(path) == self.victim and os.path.exists(path):
                os.unlink(path)  # gone after exists() said it was there
            return real_getsize(path)

        self.racing_getsize = racing_getsize

    def scan(self):
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
            mock.patch.object(os.path, "getsize", side_effect=self.racing_getsize),
        ):
            return daemon.scan_once(self.state)

    def test_a5_the_scan_finishes_and_reaches_the_next_book(self):
        summary = self.scan()
        self.assertEqual(summary["seen"], 2)
        self.assertIsNotNone(self.state.get(self.VANISHING))
        self.assertIsNotNone(self.state.get(self.SECOND))

    def test_a5_the_vanished_file_is_left_out_of_the_source_size(self):
        unit = ingest.find_any_unit(self.VANISHING)
        with (
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(matching, "match_unit", return_value=matching.Match()),
            mock.patch.object(os.path, "getsize", side_effect=self.racing_getsize),
        ):
            audit = pipeline.audit_unit(unit)
        self.assertEqual(audit.src_mb, 1.0)


# --------------------------------------------------------------------------- A7
class _CutOffResponse(io.BytesIO):
    """A chunked response whose connection drops mid-body."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_args):
        raise http.client.IncompleteRead(b'{"asin": "B0', 40)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IncompleteReadTests(unittest.TestCase):
    def setUp(self):
        catalog.cache_clear()
        self.addCleanup(catalog.cache_clear)
        sleeper = mock.patch.object(catalog.time, "sleep")  # no politeness gap / backoff waits
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_a7_a_cut_off_body_is_a_failed_lookup_not_an_exception(self):
        with mock.patch.object(catalog.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _CutOffResponse()) as opened:
            self.assertIsNone(catalog._get_json("https://example.test/a7"))
        self.assertEqual(opened.call_count, 3)  # retried like any other transport failure
        self.assertIsNone(catalog.cache_get("https://example.test/a7"))

    def test_a7_every_caller_sees_an_empty_result(self):
        with mock.patch.object(catalog.urllib.request, "urlopen", side_effect=lambda *a, **k: _CutOffResponse()):
            self.assertEqual(catalog.search(title="Dune"), [])
            self.assertEqual(catalog.book("B005EHN61O"), {})

    def test_a7_a_retry_after_a_cut_off_body_still_gets_the_answer(self):
        answers = iter([_CutOffResponse(), _Response(b'{"asin": "B005EHN61O"}')])
        with mock.patch.object(catalog.urllib.request, "urlopen", side_effect=lambda *a, **k: next(answers)):
            self.assertEqual(catalog._get_json("https://example.test/a7b"), {"asin": "B005EHN61O"})


# --------------------------------------------------------------------------- A8
OLD = 1000.0  # a row last written long ago


def age_row(state: State, path: str) -> None:
    with state._lock:
        state.connection.execute("UPDATE units SET updated_at = ? WHERE path = ?", (OLD, path))


class UpsertsMoveRevTests(unittest.TestCase):
    """Every operator edit that upserts a units row must move updated_at, so rev() notices it."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(path="A/B", status="review", title="B"))
        age_row(self.state, "A/B")

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def check(self, edit):
        before = self.state.rev()
        edit()
        self.assertGreater(self.state.get("A/B").updated_at, OLD)
        self.assertNotEqual(self.state.rev(), before)

    def test_a8_set_asin_override_moves_rev(self):
        self.check(lambda: self.state.set_asin_override("A/B", "B005EHN61O"))
        self.assertEqual(self.state.get_asin_override("A/B"), "B005EHN61O")

    def test_a8_set_overrides_moves_rev(self):
        self.check(lambda: self.state.set_overrides("A/B", {"title": "Corrected"}))
        self.assertEqual(self.state.get_overrides("A/B"), {"title": "Corrected"})

    def test_a8_set_alternates_moves_rev(self):
        self.check(lambda: self.state.set_alternates("A/B", [{"asin": "B005EHN61O"}]))
        self.assertEqual(self.state.get_alternates("A/B"), [{"asin": "B005EHN61O"}])

    def test_the_rest_of_the_row_is_untouched(self):
        self.state.set_overrides("A/B", {"title": "Corrected"})
        row = self.state.get("A/B")
        self.assertEqual((row.status, row.title), ("review", "B"))


class MetadataSaveMovesRevTests(LiveServerCase):
    """The narrowed A8 repro: a metadata Save must be visible to another tab's poll."""

    def test_a8_set_meta_moves_rev(self):
        (self.lib / "A" / "B").mkdir(parents=True)
        (self.lib / "A" / "B" / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="A/B", status="review", title="B"))
        age_row(self.state, "A/B")
        before = self.state.rev()
        status, body = self.post({"action": "set_meta", "path": "A/B", "fields": {"narrator": "Someone"}})
        self.assertEqual(status, 200, body)
        self.assertNotEqual(self.state.rev(), before)


if __name__ == "__main__":
    unittest.main()

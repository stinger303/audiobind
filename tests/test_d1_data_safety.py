"""D1: data-safety fixes from the 2026-11-20 audit (CORE-02/03/04, WEBAPI-01/02, STATECFG-01).

Each test asks one question: can this make a book disappear, duplicate, or be recorded as done
when it is not?
"""

import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from audiobind import catalog, encode, ingest, matching, pipeline, watcher, web
from audiobind.control import CONTROL
from audiobind.probe import Probe
from audiobind.state import State, UnitState, WriterBusy

try:
    from live import LiveServerCase, audited_row, published_row
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.live import LiveServerCase, audited_row, published_row
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase
# the watcher fakes (test_watcher_budget itself imports them this way)
from tests.test_watcher import TORRENT, answer, entry
from tests.test_watcher_budget import DAY, BudgetCase


class PublishCase(PipelineCase):
    """convert_unit with a fake encoder, the output format and failures under the test's control."""

    OTHER = "Andy Weir - Project Hail Mary (Other Edition) [TRACKER]"
    BOOK = {"title": "Project Hail Mary", "authors": [{"name": "Andy Weir"}]}

    def setUp(self):
        super().setUp()
        self.add_torrent()
        self.add_torrent(name=self.OTHER)
        self.cover_src = self.root / "cover-src.jpg"
        self.output_format = "M4B + MP3"

    def fake_encode(self, unit, match, book, work_dir, out_path, **kw):
        self.encode_calls.append(kw["fmt"])
        work_dir.mkdir(parents=True, exist_ok=True)
        temp = work_dir / f"output.{kw['fmt']}"
        temp.write_bytes(f"{self.tag}-{kw['fmt']}".encode())
        self.cover_src.write_bytes(f"{self.tag}-cover".encode())
        return encode.EncodeResult(path=temp, duration=7200.0, size=3, chapters=2,
                                   chapters_source="test", cover=self.cover_src)

    def patches(self, fail_place=None, fail_sidecar=False):
        real_place = encode._place
        real_meta = encode.write_abs_metadata
        real_val = pipeline.effective_val

        def place(source, dest, move, **kwargs):
            if fail_place is not None and fail_place(dest):
                raise OSError(28, "No space left on device")
            return real_place(source, dest, move, **kwargs)

        def meta(path, *args, **kw):
            if fail_sidecar:
                if path.exists():
                    path.write_bytes(b"")  # a full volume can cut the old file short first
                raise OSError(28, "No space left on device")
            return real_meta(path, *args, **kw)

        return [
            mock.patch.object(catalog, "book", return_value=self.BOOK),
            mock.patch.object(pipeline, "effective_val",
                              side_effect=lambda k: self.output_format if k == "output_format" else real_val(k)),
            mock.patch.object(encode, "plan_chapters", return_value=mock.Mock(chapters=[])),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
            mock.patch.object(encode, "_place", side_effect=place),
            mock.patch.object(encode, "write_abs_metadata", side_effect=meta),
        ]

    def convert(self, key, tag, reencode=False, **fail):
        self.tag = tag
        unit = ingest.find_any_unit(key)
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=PHM, confidence="HIGH"), action="convert")
        patches = self.patches(**fail)
        for p in patches:
            p.start()
        try:
            pipeline.convert_unit(audit, state=self.state, log=lambda *_: None, reencode=reencode)
        finally:
            for p in reversed(patches):
                p.stop()
        return pathlib.Path(audit.out_path)

    def run_process(self, key, tag, logs=None, fail_place=None, fail_sidecar=False, **kw):
        """process_unit with the catalog and probe answered, so the recorded row can be checked."""
        self.tag = tag
        patches = self.patches(fail_place=fail_place, fail_sidecar=fail_sidecar) + [
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe",
                              return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
        ]
        for p in patches:
            p.start()
        try:
            pipeline.process_unit(self.state, ingest.find_any_unit(key),
                                  log=(logs.append if logs is not None else lambda *_: None), **kw)
        finally:
            for p in reversed(patches):
                p.stop()

    @staticmethod
    def listing(folder: pathlib.Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------- CORE-02
class Core02RollbackCoversPlacedAudioTests(PublishCase):
    """An error after publish_result placed the audio (the cover, the stat) must roll it back."""

    @staticmethod
    def cover_fails(dest):
        return dest.name == "cover.jpg"

    def test_a_cover_error_on_the_first_format_leaves_no_audio_and_no_cover(self):
        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=self.cover_fails)
        m4b = pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")
        self.assertFalse(m4b.exists(), "the M4B placed before the cover error was left published")
        self.assertFalse(m4b.with_suffix(".mp3").exists())
        self.assertFalse((m4b.parent / "cover.jpg").exists())
        self.assertEqual(self.listing(m4b.parent), [])

    def test_a_cover_error_during_a_replace_leaves_the_previous_pair_intact(self):
        m4b = self.convert(KEY, "first")
        mp3, cover = m4b.with_suffix(".mp3"), m4b.parent / "cover.jpg"
        before = self.listing(m4b.parent)
        with self.assertRaises(OSError):
            self.convert(KEY, "second", reencode=True, fail_place=self.cover_fails)
        self.assertEqual((m4b.read_bytes(), mp3.read_bytes(), cover.read_bytes()),
                         (b"first-m4b", b"first-mp3", b"first-cover"))
        self.assertEqual(self.listing(m4b.parent), before)


# --------------------------------------------------------------------------- CORE-03
class Core03SidecarInsideThePublishTests(PublishCase):
    """metadata.json is part of the same publish: a sidecar failure undoes audio and cover too."""

    def process(self, key, tag, **fail):
        """The manual Convert anyway through process_unit, so the recorded row can be checked."""
        self.run_process(key, tag, force=True, manual=True, reencode=True, **fail)

    def first_edition(self):
        m4b = self.convert(KEY, "first")
        sidecar = m4b.parent / "metadata.json"
        sidecar.chmod(0o640)
        return m4b, sidecar

    def test_a_failed_sidecar_write_restores_audio_cover_and_metadata(self):
        m4b, sidecar = self.first_edition()
        mp3, cover = m4b.with_suffix(".mp3"), m4b.parent / "cover.jpg"
        before = self.listing(m4b.parent)

        original_meta = sidecar.read_bytes()
        self.process(KEY, "second", fail_sidecar=True)

        self.assertEqual((m4b.read_bytes(), mp3.read_bytes(), cover.read_bytes()),
                         (b"first-m4b", b"first-mp3", b"first-cover"),
                         "the new edition's audio stayed published after the sidecar failed")
        self.assertEqual(sidecar.read_bytes(), original_meta)
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o640)  # SEC-03b: the mode comes back too
        self.assertEqual(self.listing(m4b.parent), before)  # every backup went back
        self.assertEqual(self.state.get(KEY).status, "failed")

    def test_a_first_publish_whose_sidecar_fails_leaves_nothing(self):
        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_sidecar=True)
        m4b = pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")
        self.assertEqual(self.listing(m4b.parent), [])

    def test_a_successful_replace_keeps_the_previous_metadata_with_its_audio(self):
        m4b, sidecar = self.first_edition()
        original_meta = sidecar.read_bytes()
        self.process(KEY, "second")
        self.assertEqual(m4b.read_bytes(), b"second-m4b")
        self.assertIn('"title": "Project Hail Mary"', sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o640)  # SEC-03b
        (backup,) = sorted(m4b.parent.glob("metadata.json.bak-*"))
        self.assertEqual(backup.read_bytes(), original_meta)

    def test_a_first_publish_with_a_stray_sidecar_goes_to_review(self):
        m4b = pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")
        m4b.parent.mkdir(parents=True)
        (m4b.parent / "metadata.json").write_text("{}", encoding="utf-8")
        self.run_process(KEY, "first")
        self.assertEqual(self.state.get(KEY).status, "review")
        self.assertEqual(self.listing(m4b.parent), ["metadata.json"])
        self.assertEqual((m4b.parent / "metadata.json").read_text(encoding="utf-8"), "{}")


# --------------------------------------------------------------------------- CORE-04
class Core04EveryRequestedFormatTests(PublishCase):
    """"Already published" means every requested format is on disk, not only the first one."""

    def retry(self, tag="second", logs=None):
        self.encode_calls.clear()
        self.run_process(KEY, tag, logs=logs, manual=True)  # Retry now: no force, no re-encode

    def converted(self, output_format):
        self.output_format = output_format
        self.run_process(KEY, "first")
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        return pathlib.Path(row.output)

    def test_a_m4b_book_retried_after_switching_to_dual_gets_its_mp3(self):
        m4b = self.converted("M4B")
        self.assertFalse(m4b.with_suffix(".mp3").exists())
        self.output_format = "M4B + MP3"
        self.retry()
        self.assertEqual(m4b.with_suffix(".mp3").read_bytes(), b"second-mp3",
                         "Retry kept the M4B-only book and never made the requested MP3")
        self.assertEqual(self.encode_calls, ["mp3"])  # the published M4B is not encoded again
        self.assertEqual(m4b.read_bytes(), b"first-m4b")
        self.assertEqual(self.backups(m4b), [])  # nothing replaced, nothing moved aside
        row = self.state.get(KEY)
        self.assertEqual((row.status, row.output), ("converted", str(m4b)))

    def test_without_a_stored_output_a_partial_set_goes_to_review(self):
        m4b = self.converted("M4B")
        self.state.delete(KEY)
        self.output_format = "M4B + MP3"
        self.retry()
        self.assertFalse(m4b.with_suffix(".mp3").exists())
        self.assertEqual(m4b.read_bytes(), b"first-m4b")
        row = self.state.get(KEY)
        self.assertEqual((row.status, row.output), ("review", ""))

    def test_with_both_formats_present_nothing_is_encoded(self):
        m4b = self.converted("M4B + MP3")
        before = self.state.get(KEY)
        self.retry()
        self.assertEqual(self.encode_calls, [])
        self.assertEqual((m4b.read_bytes(), m4b.with_suffix(".mp3").read_bytes()), (b"first-m4b", b"first-mp3"))
        self.assertEqual(self.state.get(KEY).status, before.status)

    def test_an_mp3_deleted_after_a_dual_publish_is_restored_by_the_next_pass(self):
        m4b = self.converted("M4B + MP3")
        m4b.with_suffix(".mp3").unlink()
        self.retry()
        self.assertEqual(m4b.with_suffix(".mp3").read_bytes(), b"second-mp3")
        self.assertEqual(self.encode_calls, ["mp3"])
        self.assertEqual(m4b.read_bytes(), b"first-m4b")

    def test_a_book_kept_at_an_older_layout_gets_the_mp3_beside_it_not_a_second_copy(self):
        current = self.converted("M4B")
        old = pathlib.Path(pipeline.CONFIG.output_root) / "Old Layout" / current.name
        old.parent.mkdir(parents=True)
        current.rename(old)
        row = self.state.get(KEY)
        detail = json.loads(row.detail)
        detail["output"]["path"] = str(old)
        row.detail = json.dumps(detail)
        row.output = str(old)
        self.state.save(row)
        self.output_format = "M4B + MP3"
        self.retry()
        self.assertEqual(old.with_suffix(".mp3").read_bytes(), b"second-mp3")
        self.assertFalse(current.exists(), "a duplicate was encoded at the current layout path")
        self.assertFalse(current.with_suffix(".mp3").exists())
        self.assertEqual(self.state.get(KEY).output, str(old))

    def test_a_failed_mp3_leaves_the_kept_m4b_and_records_failed_not_ready(self):
        m4b = self.converted("M4B")
        self.output_format = "M4B + MP3"
        self.encode_calls.clear()
        self.run_process(KEY, "second", manual=True, fail_place=lambda dest: dest.suffix == ".mp3")
        self.assertEqual(m4b.read_bytes(), b"first-m4b")
        self.assertFalse(m4b.with_suffix(".mp3").exists())
        row = self.state.get(KEY)
        self.assertEqual((row.status, row.output), ("failed", str(m4b)))

    @staticmethod
    def backups(path: pathlib.Path) -> list[pathlib.Path]:
        return sorted(path.parent.glob(path.name + ".bak-*"))


# --------------------------------------------------------------------------- WEBAPI-01/02
class DeleteCase(LiveServerCase):
    REL = "Author/Book"

    def setUp(self):
        super().setUp()
        CONTROL._queue.clear()
        self.addCleanup(CONTROL._queue.clear)
        self.src = self.lib / "Author" / "Book"
        self.src.mkdir(parents=True)
        (self.src / "01.mp3").write_bytes(b"audio")
        self.out_book = self.out / "Author" / "Book"
        self.out_book.mkdir(parents=True)
        (self.out_book / "Book.m4b").write_bytes(b"m4b")
        row = UnitState(
            path=self.REL, status="converted", title="Book", output=str(self.out_book / "Book.m4b")
        )
        self.state.save(audited_row(self.lib, published_row(row)))

    def delete(self):
        return self.post({"action": "delete", "path": self.REL, "confirm": True})

    def assert_untouched(self, status, body):
        self.assertEqual(status, 409, body)
        self.assertTrue((self.src / "01.mp3").exists(), "the source was removed")
        self.assertTrue((self.out_book / "Book.m4b").exists(), "the output was removed")
        self.assertIsNotNone(self.state.get(self.REL), "the row was dropped")

    def then(self, name, action):
        """Patch web.<name> so ``action`` runs right after the real call (a concurrent request)."""
        real = getattr(web, name)

        def side_effect(*args, **kw):
            result = real(*args, **kw)
            action()
            return result

        return mock.patch.object(web, name, side_effect=side_effect)


class WebApi01DeleteVersusConversionTests(DeleteCase):
    """A Convert anyway queued while a delete walks the disk must stop the delete untouched."""

    def test_a_force_queued_before_the_delete_is_refused(self):
        CONTROL.try_enqueue(self.REL)
        self.assert_untouched(*self.delete())

    def test_a_force_cannot_queue_during_delete_planning(self):
        queued = []
        with self.then("plan_target", lambda: queued.append(CONTROL.try_enqueue(self.REL))):
            status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertTrue(queued)
        self.assertFalse(any(queued))

    def test_an_active_audit_blocks_delete(self):
        self.assertTrue(CONTROL.begin_audit(self.REL))
        try:
            self.assert_untouched(*self.delete())
        finally:
            CONTROL.end_audit(self.REL)

    def test_a_conversion_holding_the_book_claim_blocks_delete(self):
        with self.state.unit_writer(self.REL):
            status, body = self.delete()
        self.assert_untouched(status, body)
        self.assertIn("another run is working on this book", body["error"])

    def test_a_normal_delete_still_removes_source_output_and_row(self):
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual((body["source_removed"], body["output"]), (True, "removed"))
        self.assertFalse(self.src.exists())
        self.assertFalse(self.out_book.exists())
        self.assertIsNone(self.state.get(self.REL))
        with self.state.unit_writer(self.REL), self.state.publication_lock(blocking=False):
            self.assertIsNone(self.state.get(self.REL))


class WebApi02DeleteVersusLayoutMoveTests(DeleteCase):
    """A layout move between the delete's read of the row and its removal must never yield a 200
    with the published files still on disk."""

    def move_layout(self):
        """What abs_layout.apply does for one book: rename the folder, then record the new output.
        Once: it runs after the delete's first read of the row (the window the audit found)."""
        if getattr(self, "moved", None) is not None:
            return
        moved = self.out / "Author" / "Series" / "1 - Book"
        moved.parent.mkdir(parents=True, exist_ok=True)
        self.out_book.rename(moved)
        web.abs_layout._record_output(self.state, self.REL, moved / "Book.m4b")
        self.moved = moved

    def test_a_layout_move_during_the_walk_removes_the_new_folder_or_refuses(self):
        with self.then("output_sharers", self.move_layout):
            status, body = self.delete()
        if status == 200:
            self.assertEqual(body["output"], "removed", body)
            self.assertFalse(self.moved.exists(), "200 OK, but the moved book is still published")
            self.assertIsNone(self.state.get(self.REL))
        else:
            self.assertEqual(status, 409, body)
            self.assertTrue((self.moved / "Book.m4b").exists())
            self.assertIsNotNone(self.state.get(self.REL))

    def test_the_output_is_moved_aside_under_the_publication_lock(self):
        # WEBAPI-02b: the published folder leaves its name (a rename) while the lock is held; the
        # slow removal of the renamed folder runs after it is released
        held = []
        real_rename = web.os.rename

        def rename(src, dst, *args, **kw):
            if pathlib.Path(src) == self.out_book:
                with self.assertRaises(WriterBusy):
                    with self.state.publication_lock(blocking=False):
                        pass
                held.append(True)
            return real_rename(src, dst, *args, **kw)

        with mock.patch.object(web.os, "rename", side_effect=rename):
            status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual(held, [True], "a layout move could run while the output was being moved aside")
        self.assertFalse(self.out_book.exists())
        with self.state.publication_lock(blocking=False):
            pass


# --------------------------------------------------------------------------- STATECFG-01
class StateCfg01CreditLedgerOutlivesTheWishlistTests(BudgetCase):
    """A spent freeleech credit stays counted after the book lands and its wishlist row goes."""

    def test_a_landed_book_still_counts_against_the_daily_cap(self):
        self.set_budget(wedge_per_day=1)
        self.want("B000000001", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        self.assertEqual(self.cycle()["sent"], 1)
        spent_at = time.time()

        # the book is converted: the watcher drops its wishlist row (watcher.py, "in the library")
        self.assertTrue(self.state.wishlist_remove("B000000001"))
        self.assertEqual(self.state.wedges_spent_since(spent_at - DAY), 1,
                         "the spend vanished with the wishlist row")

        self.want("B000000002", title="Artemis")
        self.fake_tracker(answer(entry(1002, "Artemis")))
        summary = self.cycle()
        self.assertEqual((summary["sent"], summary["skipped_budget"]), (0, 1))
        self.assertEqual(self.downloads(), [], "the daily cap was spent a second time")

        # a day later the first spend has left the window, and the next credit may be spent
        later = spent_at + DAY + 60
        self.fake_tracker(answer(entry(1002, "Artemis")), (200, {}, TORRENT))
        with mock.patch.object(watcher.time, "time", return_value=later):
            self.assertEqual(self.cycle()["sent"], 1)
        self.assertEqual(self.state.wedges_spent_since(later - DAY), 1)
        self.assertEqual(self.state.wedges_spent_since(0), 2)

    def test_a_spend_on_a_row_removed_mid_cycle_is_still_counted(self):
        self.state.mark_wedge_spent("4242", time.time())  # no wishlist row carries this tid
        self.assertEqual(self.state.wedges_spent_since(time.time() - DAY), 1)


class StateCfg01MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def test_an_old_database_keeps_its_spends_and_reopening_is_idempotent(self):
        state = State(self.dir)
        now = time.time()
        # a database from before the ledger table: the spends live on the wishlist rows only
        state.connection.execute("DROP TABLE IF EXISTS credit_spends")
        for asin, tid, spent in (("B0000000A1", "11", 1), ("B0000000A2", "12", 1), ("B0000000A3", "13", 0)):
            state.connection.execute(
                "INSERT INTO wishlist (asin, title, added_at, tracker_tid, wedge_spent, wedge_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (asin, asin, now, tid, spent, now - 60 if spent else None))
        state.close()

        for _ in range(2):  # every start runs the migration; the back-fill happens once
            state = State(self.dir)
            self.assertEqual(state.wedges_spent_since(now - DAY), 2)
            state.close()
        state = State(self.dir)
        self.addCleanup(state.close)
        self.assertEqual(state.connection.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0], 3)  # additive


if __name__ == "__main__":
    unittest.main()

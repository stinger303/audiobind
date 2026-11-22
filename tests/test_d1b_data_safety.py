"""D1b: the data-loss holes the CX-D1 cross-review found in D1 (CORE-02b/03b/04a/04b, WEBAPI-02b).

The theme: never remove or overwrite a file this process did not create.
"""

import errno
import json
import os
import pathlib
import unittest
from unittest import mock

from audiobind import abs_layout, pipeline, web

try:
    from test_d1_data_safety import DeleteCase, PublishCase
    from test_ingest_pipeline import KEY
except ImportError:
    from tests.test_d1_data_safety import DeleteCase, PublishCase
    from tests.test_ingest_pipeline import KEY


def published_m4b() -> pathlib.Path:
    return pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")


# --------------------------------------------------------------------------- CORE-02b
class Core02bRollbackRemovesOnlyWhatItCreatedTests(PublishCase):
    """A failed publish unlinks only the paths it placed - never a link or file it found."""

    def test_a_dangling_audio_link_survives_a_first_placement_failure(self):
        m4b = published_m4b()
        m4b.parent.mkdir(parents=True)
        target = m4b.parent / "moved-away.m4b"  # inside the output root, and no longer there
        os.symlink(target, m4b)
        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=lambda dest: dest == m4b)
        self.assertTrue(os.path.islink(m4b), "the pre-existing link was removed by the rollback")
        self.assertEqual(os.readlink(m4b), str(target))
        self.assertEqual(self.listing(m4b.parent), [m4b.name])  # no backup or new file left over

    def test_a_file_another_writer_made_after_the_snapshot_is_left_alone(self):
        m4b = published_m4b()
        mp3 = m4b.with_suffix(".mp3")

        def other_writer_then_fail(dest):
            if dest == mp3:
                mp3.write_bytes(b"other-writer")  # appears after the snapshot, before placement
                return True
            return False

        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=other_writer_then_fail)
        self.assertEqual(mp3.read_bytes(), b"other-writer", "another writer's file was unlinked")
        # what this call did place is still cleaned up
        self.assertFalse(m4b.exists())
        self.assertFalse((m4b.parent / "cover.jpg").exists())


# --------------------------------------------------------------------------- CORE-03b
class Core03bCoverBackedUpWhenOnlyAFormatIsAddedTests(PublishCase):
    """Adding a missing format writes cover.jpg too: the existing art must be backed up first."""

    def kept_m4b_with_cover(self):
        self.output_format = "M4B"
        m4b = self.convert(KEY, "first")
        cover = m4b.parent / "cover.jpg"
        cover.write_bytes(b"owner-cover")
        self.output_format = "MP3"  # only the missing format is requested; the M4B is not replaced
        return m4b, cover

    def test_a_failed_sidecar_leaves_the_existing_cover_byte_identical(self):
        m4b, cover = self.kept_m4b_with_cover()
        before = self.listing(m4b.parent)
        with self.assertRaises(OSError):
            self.convert(KEY, "second", fail_sidecar=True)
        self.assertEqual(cover.read_bytes(), b"owner-cover", "the rollback left the new cover over the old art")
        self.assertEqual(m4b.read_bytes(), b"first-m4b")
        self.assertEqual(self.listing(m4b.parent), before)  # the MP3 went, the backup came back

    def test_a_successful_add_keeps_the_previous_cover_as_a_backup(self):
        m4b, cover = self.kept_m4b_with_cover()
        self.convert(KEY, "second")
        self.assertEqual(m4b.with_suffix(".mp3").read_bytes(), b"second-mp3")
        self.assertEqual(cover.read_bytes(), b"second-cover")
        (backup,) = sorted(m4b.parent.glob("cover.jpg.bak-*"))
        self.assertEqual(backup.read_bytes(), b"owner-cover")


# --------------------------------------------------------------------------- CORE-04b
class Core04bEveryDestinationIsContainedTests(PublishCase):
    """A saved anchor is checked file by file: its siblings, cover and metadata must all land in a
    book folder inside the output root, or nothing is written."""

    def anchored_at(self, anchor: pathlib.Path) -> pathlib.Path:
        """A converted M4B-only book whose row points at ``anchor``, a symlink to the real M4B."""
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        real = pathlib.Path(self.state.get(KEY).output)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real, anchor)
        row = self.state.get(KEY)
        row.output = str(anchor)
        self.state.save(row)
        self.output_format = "M4B + MP3"  # the MP3 is missing beside the anchor
        return real

    def assert_refused(self, real: pathlib.Path):
        row = self.state.get(KEY)
        self.assertEqual(row.status, "failed", "the conversion was not refused")
        self.assertIn("outside", row.note)
        self.assertEqual(self.listing(real.parent), [real.name, "cover.jpg", "metadata.json"])

    def test_an_anchor_link_from_outside_the_root_writes_nothing_beside_it(self):
        outside = self.root / "outside"
        anchor = outside / "Project Hail Mary.m4b"
        real = self.anchored_at(anchor)
        self.run_process(KEY, "second", manual=True)
        self.assertEqual(self.listing(outside), [anchor.name], "files were published outside the output root")
        self.assert_refused(real)

    def test_an_anchor_link_directly_in_the_root_does_not_publish_into_the_root(self):
        out_root = pathlib.Path(pipeline.CONFIG.output_root)
        anchor = out_root / "Project Hail Mary.m4b"
        real = self.anchored_at(anchor)
        before = self.listing(out_root)
        self.run_process(KEY, "second", manual=True)
        self.assertEqual(self.listing(out_root), before, "files were published into the output root itself")
        self.assert_refused(real)


# --------------------------------------------------------------------------- CORE-04a
def wrote(row) -> list:
    return json.loads(row.detail)["output"]["wrote"]


class Core04aTheRowRecordsTheFilesItWroteTests(PublishCase):
    """A converted row names the exact files Audiobind wrote into the book's folder."""

    NAMES = ["Project Hail Mary.m4b", "Project Hail Mary.mp3", "cover.jpg", "metadata.json"]

    def add_mp3(self):
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "second", manual=True)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        return row

    def test_an_owner_m4b_given_an_mp3_records_only_what_audiobind_wrote(self):
        m4b = published_m4b()
        m4b.parent.mkdir(parents=True)
        m4b.write_bytes(b"owner-m4b")  # the owner's own file: an M4B-mode scan records it ready
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        self.assertEqual(self.state.get(KEY).status, "ready")
        row = self.add_mp3()
        self.assertEqual(wrote(row), ["Project Hail Mary.mp3", "cover.jpg", "metadata.json"])
        self.assertEqual(m4b.read_bytes(), b"owner-m4b")

    def test_a_fully_written_book_records_every_file(self):
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "first")
        self.assertEqual(wrote(self.state.get(KEY)), self.NAMES)

    def test_adding_a_format_to_audiobinds_own_book_keeps_what_it_wrote_before(self):
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        row = self.add_mp3()
        # the old cover and metadata were kept as backups (CORE-03b): they were Audiobind's, so
        # the backups are too
        folder = published_m4b().parent
        backups = [p.name for p in [*folder.glob("cover.jpg.bak-*"), *folder.glob("metadata.json.bak-*")]]
        self.assertEqual(len(backups), 2)
        self.assertEqual(wrote(row), sorted([*self.NAMES, *backups]))

    def test_a_row_from_before_the_list_keeps_legacy_files_unverified(self):
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        row = self.state.get(KEY)
        detail = json.loads(row.detail)
        del detail["output"]["wrote"]  # recorded by a release without the list
        row.detail = json.dumps(detail)
        self.state.save(row)
        row = self.add_mp3()
        self.assertEqual(wrote(row), ["Project Hail Mary.mp3", "cover.jpg", "metadata.json"])


class Core04aDeleteRemovesOnlyWhatAudiobindWroteTests(DeleteCase):
    """Delete goes by the recorded list: the folder only when every file in it is Audiobind's."""

    def record_wrote(self, names):
        """What a publish records (CORE-04d: each name with the file's size and mtime)."""
        row = self.state.get(self.REL)
        ids = {}
        for name in names:
            st = os.lstat(self.out_book / name)
            ids[name] = [st.st_size, st.st_mtime_ns]
        detail = json.loads(row.detail)
        detail["output"] = {"path": row.output, "wrote": names, "wrote_id": ids}
        row.detail = json.dumps(detail)
        self.state.save(row)

    def test_an_owner_m4b_survives_when_only_the_mp3_was_audiobinds(self):
        for name in ("Book.mp3", "cover.jpg", "metadata.json"):
            (self.out_book / name).write_bytes(b"ours")
        self.record_wrote(["Book.mp3", "cover.jpg", "metadata.json"])
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual((self.out_book / "Book.m4b").read_bytes(), b"m4b", "the owner's M4B was deleted")
        self.assertEqual(sorted(p.name for p in self.out_book.iterdir()), ["Book.m4b"])
        # C2: Audiobind's own MP3 was removed - the answer says so, and names the M4B left beside it
        self.assertEqual(body["output"], "removed")
        self.assertIn("did not write", body["output_other_files_left"])
        self.assertIn("Book.m4b", body["output_other_files_left"])
        self.assertEqual(body["output_removed_files"], ["Book.mp3", "cover.jpg", "metadata.json"])
        self.assertIsNone(self.state.get(self.REL))

    def test_a_layout_rename_carries_the_list_to_the_new_names(self):
        (self.out_book / "Book.mp3").write_bytes(b"ours")
        self.record_wrote(["Book.m4b", "Book.mp3"])
        moved = self.out / "Author" / "Series" / "1 - Book"
        moved.parent.mkdir(parents=True)
        self.out_book.rename(moved)
        for name in ("Book.m4b", "Book.mp3"):  # what abs_layout._rename_files does
            (moved / name).rename(moved / name.replace("Book", "1 - Book"))
        abs_layout._record_output(self.state, self.REL, moved / "1 - Book.m4b",
                                  old=self.out_book / "Book.m4b")
        row = self.state.get(self.REL)
        self.assertEqual(wrote(row), ["1 - Book.m4b", "1 - Book.mp3"])
        status, body = self.delete()
        self.assertEqual((status, body["output"]), (200, "removed"), body)
        self.assertFalse(moved.exists())

    def test_a_book_audiobind_wrote_entirely_is_still_removed_with_its_folder(self):
        (self.out_book / "cover.jpg").write_bytes(b"ours")
        self.record_wrote(["Book.m4b", "cover.jpg"])
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "removed")
        self.assertFalse(self.out_book.exists())


# --------------------------------------------------------------------------- WEBAPI-02b
class WebApi02bSlowRemovalOutsideTheLockTests(DeleteCase):
    """Delete decides under CONVERT_LOCK and moves its targets aside there (a rename); the slow
    removal and the HTTP answer happen after the lock is released."""

    def leftovers(self):
        """Anything still sitting beside the source or output folders (a trash name left behind)."""
        return sorted(p.name for p in [*self.src.parent.iterdir(), *self.out_book.parent.iterdir()]
                      if p.name not in ("Book",))

    def test_the_lock_is_free_during_the_removal_and_when_the_answer_is_sent(self):
        real_rename, real_remove, real_send = web.os.rename, web.remove_trashed, web.Handler._send
        renamed_locked, free_while_removing, sent_locked = [], [], []

        def rename(src, dst, *args, **kw):
            if pathlib.Path(src) in (self.src, self.out_book):
                renamed_locked.append(pipeline.CONVERT_LOCK.locked())
            return real_rename(src, dst, *args, **kw)

        def slow_remove(path, *args, **kw):
            got = pipeline.CONVERT_LOCK.acquire(blocking=False)  # an encode could start right now
            if got:
                pipeline.CONVERT_LOCK.release()
            free_while_removing.append(got)
            return real_remove(path, *args, **kw)

        def send(handler, *args, **kw):
            sent_locked.append(pipeline.CONVERT_LOCK.locked())
            return real_send(handler, *args, **kw)

        with (
            mock.patch.object(web.os, "rename", side_effect=rename),
            mock.patch.object(web, "remove_trashed", side_effect=slow_remove),
            mock.patch.object(web.Handler, "_send", new=send),
        ):
            status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual((body["source_removed"], body["output"]), (True, "removed"))
        self.assertEqual(renamed_locked, [True, True], "the targets were not moved aside under the lock")
        self.assertEqual(free_while_removing, [True, True], "the slow removal ran with the encoder lock held")
        self.assertEqual(sent_locked, [False], "the answer was sent with the encoder lock held")
        self.assertFalse(self.src.exists())
        self.assertFalse(self.out_book.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertIsNone(self.state.get(self.REL))

    def test_a_failed_rename_leaves_everything_in_place_and_says_so(self):
        real_rename = web.os.rename

        def rename(src, dst, *args, **kw):
            if pathlib.Path(src) == self.out_book:
                raise OSError(errno.EACCES, "Permission denied")
            return real_rename(src, dst, *args, **kw)

        with mock.patch.object(web.os, "rename", side_effect=rename):
            status, body = self.delete()
        self.assertEqual(status, 500, body)
        self.assertIn("nothing was removed", body["error"])
        self.assertIn("EACCES", body["error"])
        self.assertFalse(body["source_removed"])
        self.assertTrue((self.src / "01.mp3").exists(), "the source was moved aside and not put back")
        self.assertTrue((self.out_book / "Book.m4b").exists())
        self.assertEqual(self.leftovers(), [])
        self.assertIsNotNone(self.state.get(self.REL))
        self.assertFalse(pipeline.CONVERT_LOCK.locked())


if __name__ == "__main__":
    unittest.main()

"""D1c: the last three data-safety items from the CX-D1b review (CORE-04c, CORE-04d, WEBAPI-02c).

The theme: a file is Audiobind's to remove only when Audiobind can prove it wrote it.
"""

import json
import os
import pathlib
import unittest
from unittest import mock

from audiobind import daemon, web

try:
    from test_d1_data_safety import DeleteCase, PublishCase
    from test_d1b_data_safety import published_m4b, wrote
    from test_ingest_pipeline import KEY
except ImportError:
    from tests.test_d1_data_safety import DeleteCase, PublishCase
    from tests.test_d1b_data_safety import published_m4b, wrote
    from tests.test_ingest_pipeline import KEY


def listing(folder) -> list:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------- CORE-04c
class Core04cUnknownProvenanceIsNotOwnershipTests(DeleteCase):
    """Unverifiable names require their own confirmation."""

    def legacy(self):
        row = self.state.get(self.REL)
        detail = json.loads(row.detail)
        del detail["output"]
        row.detail = json.dumps(detail)
        self.state.save(row)

    def test_a_legacy_row_leaves_the_owner_files_and_says_so(self):
        self.legacy()
        (self.out_book / "notes.txt").write_bytes(b"owner")
        (self.out_book / "cover.jpg").write_bytes(b"owner-art")
        status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertEqual(body["legacy_output_paths"], ["Author/Book/Book.m4b"])
        self.assertEqual(listing(self.out_book), ["Book.m4b", "cover.jpg", "notes.txt"])
        self.assertIsNotNone(self.state.get(self.REL))

    def test_unreadable_detail_requires_a_source_recheck(self):
        row = self.state.get(self.REL)
        row.detail = "{not json"
        self.state.save(row)
        status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertIn("re-check", body["error"])
        self.assertEqual(listing(self.out_book), ["Book.m4b"])

    def test_legacy_candidates_are_removed_only_after_separate_confirmation(self):
        self.legacy()
        (self.out_book / "cover.jpg").write_bytes(b"ours")
        (self.out_book / "metadata.json").write_bytes(b"{}")
        status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertEqual(body["legacy_output_paths"],
                         ["Author/Book/Book.m4b", "Author/Book/cover.jpg", "Author/Book/metadata.json"])
        snapshot = body["legacy_output_snapshot"]
        self.assertIsInstance(snapshot, str)  # browser JSON numbers cannot round-trip nanosecond mtimes
        status, body = self.post({"action": "delete", "path": self.REL, "confirm": True,
                                  "legacy_output": snapshot})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "removed")
        self.assertEqual(listing(self.out_book), [])


class Core04cLegacyRowRepublishTests(PublishCase):
    """A Convert anyway into a legacy row's folder must not record the owner's files as Audiobind's."""

    def test_an_owner_file_in_a_legacy_folder_is_not_adopted(self):
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        folder = published_m4b().parent
        (folder / "notes.txt").write_bytes(b"owner")
        row = self.state.get(KEY)
        detail = json.loads(row.detail)
        del detail["output"]["wrote"]  # recorded by a release without the list
        detail["output"].pop("wrote_id", None)
        row.detail = json.dumps(detail)
        self.state.save(row)
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "second", manual=True)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        self.assertNotIn("notes.txt", wrote(row))
        self.assertNotIn("Project Hail Mary.m4b", wrote(row))  # the old file has no recorded identity


# --------------------------------------------------------------------------- CORE-04d
def identity(path) -> list:
    st = os.lstat(path)
    return [st.st_size, st.st_mtime_ns]


class Core04dARecordedNameIsCheckedAgainstTheFileTests(DeleteCase):
    """A name in the row's list is Audiobind's only while the file on disk is still the one it wrote."""

    def record(self, names):
        """What a publish records: each name with its size and mtime at that moment."""
        row = self.state.get(self.REL)
        detail = json.loads(row.detail)
        detail["output"] = {"path": row.output, "wrote": sorted(names),
                            "wrote_id": {n: identity(self.out_book / n) for n in names}}
        row.detail = json.dumps(detail)
        self.state.save(row)

    def test_a_recorded_file_replaced_by_an_owner_copy_is_left_and_the_answer_says_why(self):
        (self.out_book / "Book.mp3").write_bytes(b"ours")
        self.record(["Book.m4b", "Book.mp3"])
        (self.out_book / "Book.mp3").write_bytes(b"the owner's own, longer mp3")  # same name, new file
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual((self.out_book / "Book.mp3").read_bytes(), b"the owner's own, longer mp3",
                         "the owner's file was deleted under a stale recorded name")
        self.assertEqual(listing(self.out_book), ["Book.mp3"])
        self.assertEqual(body["output_removed_files"], ["Book.m4b"])
        self.assertIn("changed since", body["output_left"]["Book.mp3"])

    def test_same_size_but_a_different_mtime_is_not_ours_either(self):
        (self.out_book / "Book.mp3").write_bytes(b"ours")
        self.record(["Book.m4b", "Book.mp3"])
        st = os.stat(self.out_book / "Book.mp3")
        os.utime(self.out_book / "Book.mp3", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual(listing(self.out_book), ["Book.mp3"])

    def test_an_untouched_recorded_file_is_still_deleted(self):
        (self.out_book / "Book.mp3").write_bytes(b"ours")
        self.record(["Book.m4b", "Book.mp3"])
        status, body = self.delete()
        self.assertEqual((status, body["output"]), (200, "removed"), body)
        self.assertFalse(self.out_book.exists())


class Core04dTheRecordFollowsThePublishTests(PublishCase):
    """Every publish refreshes the record, and drops a name whose file is gone or no longer ours."""

    def test_a_removed_format_replaced_by_the_owner_is_not_carried_into_the_new_record(self):
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "first")
        mp3 = published_m4b().with_suffix(".mp3")
        mp3.unlink()  # the owner drops the MP3 format ...
        mp3.write_bytes(b"the owner's own mp3, from elsewhere")  # ... and later puts their own there
        self.output_format = "M4B"
        self.run_process(KEY, "second", manual=True, reencode=True)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        self.assertNotIn(mp3.name, wrote(row), "a stale name now covers the owner's file")

    def test_the_record_holds_the_size_and_mtime_of_each_file_written(self):
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "first")
        folder = published_m4b().parent
        ids = json.loads(self.state.get(KEY).detail)["output"]["wrote_id"]
        self.assertEqual(sorted(ids), wrote(self.state.get(KEY)))
        for name, ident in ids.items():
            self.assertEqual(ident, identity(folder / name), name)


# --------------------------------------------------------------------------- WEBAPI-02c
class WebApi02cDeleteIntentIsDurableTests(DeleteCase):
    """Delete writes where each target went before the first rename; a crash leaves that record,
    and startup puts every listed path back."""

    def journal(self) -> pathlib.Path:
        return pathlib.Path(self.state.path).parent / "delete-intent.json"  # web.DELETE_JOURNAL

    def test_a_crash_after_the_rename_is_put_back_at_startup(self):
        # what a crash right after move_aside leaves: the folder under its hidden name, the row
        # still pointing at it, and the intent on disk
        trash = self.out_book.with_name(".Book.audiobind-delete-1-deadbeef")
        self.out_book.rename(trash)
        self.journal().write_text(json.dumps({"entries": [{"path": str(self.out_book), "trash": str(trash)}]}))
        with self.assertLogs("audiobind", level="INFO") as logs:
            web.recover_deletes(self.journal())
        self.assertEqual((self.out_book / "Book.m4b").read_bytes(), b"m4b", "the folder was not put back")
        self.assertFalse(trash.exists())
        self.assertFalse(self.journal().exists(), "the intent was not cleared")
        self.assertTrue(any(str(self.out_book) in line for line in logs.output), logs.output)
        # idempotent: a second startup finds nothing to do and changes nothing
        web.recover_deletes(self.journal())
        self.assertEqual(listing(self.out_book), ["Book.m4b"])
        self.assertFalse(self.journal().exists())

    def test_a_normal_delete_records_the_intent_before_removing_and_leaves_none_behind(self):
        seen = []
        real = web.remove_trashed

        def remove(*args):
            seen.append(json.loads(self.journal().read_text()) if self.journal().exists() else None)
            return real(*args)

        with mock.patch.object(web, "remove_trashed", side_effect=remove):
            status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertTrue(seen and all(seen), f"no intent on disk while the trash was removed: {seen}")
        recorded = {entry["path"] for entry in seen[0]["entries"]}
        self.assertEqual(recorded, {str(self.src), str(self.out_book)})
        self.assertFalse(self.journal().exists(), "a finished delete left its intent behind")


class WebApi02cStartupRunsTheRecoveryTests(unittest.TestCase):
    def test_the_daemon_recovers_interrupted_deletes_before_anything_else_reads_the_state(self):
        calls = []

        class Stop(Exception):
            pass

        state = mock.Mock(path=pathlib.Path("/state/audiobind.db"))
        with (
            mock.patch.object(daemon, "State", return_value=state),
            mock.patch.object(daemon.CONFIG, "ensure_dirs"),
            mock.patch.object(daemon.web, "recover_deletes", side_effect=lambda j: calls.append(("recover", j))),
            mock.patch.object(daemon, "restore_controls", side_effect=lambda s: (calls.append(("restore",)), _raise(Stop()))),
        ):
            with self.assertRaises(Stop):
                daemon.run()
        self.assertEqual(calls, [("recover", pathlib.Path("/state") / web.DELETE_JOURNAL), ("restore",)])


def _raise(exc):
    raise exc


if __name__ == "__main__":
    unittest.main()

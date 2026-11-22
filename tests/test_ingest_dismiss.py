"""0.19.0-dev13: Delete on an ingest book removes Audiobind's converted copy, forgets the row and
dismisses its key, while the torrent under /ingest keeps seeding untouched. The scan skips a
dismissed key; a dismissal is pruned once its folder is gone, but only while ingest is reachable."""

import shutil
import unittest
from unittest import mock

from audiobind import daemon, ingest, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState
from tests.live import published_row

try:
    from test_ingest_discovery import IngestConfigCase, touch, tree_snapshot
    from test_ingest_pipeline import PipelineCase
    from test_ingest_web import IngestWebCase
except ImportError:
    from tests.test_ingest_discovery import IngestConfigCase, touch, tree_snapshot
    from tests.test_ingest_pipeline import PipelineCase
    from tests.test_ingest_web import IngestWebCase

TORRENT = "Andy Weir - Project Hail Mary (2021) [TRACKER]"
KEY = "ingest:" + TORRENT
SEEDING = "the files are a torrent that is still seeding in qBittorrent"


class TestDismissedTable(IngestConfigCase):
    def setUp(self):
        super().setUp()
        self.state = State(self.root / "state")

    def tearDown(self):
        self.state.close()  # before the temp dir goes (cleanups would run too late)
        super().tearDown()

    def test_dismiss_round_trip(self):
        self.assertFalse(self.state.is_dismissed(KEY))
        self.assertEqual(self.state.dismissed_keys(), set())
        self.state.dismiss(KEY)
        self.state.dismiss(KEY)  # idempotent
        self.assertTrue(self.state.is_dismissed(KEY))
        self.assertEqual(self.state.dismissed_keys(), {KEY})
        self.assertTrue(self.state.undismiss(KEY))
        self.assertFalse(self.state.undismiss(KEY))
        self.assertFalse(self.state.is_dismissed(KEY))

    def test_survives_a_reopen(self):
        self.state.dismiss(KEY)
        self.state.close()
        self.state = State(self.root / "state")
        self.assertTrue(self.state.is_dismissed(KEY))

    def test_a_unit_delete_does_not_touch_dismissals(self):
        self.state.save(UnitState(path=KEY, status="review"))
        self.state.dismiss(KEY)
        self.state.delete(KEY)
        self.assertTrue(self.state.is_dismissed(KEY))


class TestIngestDeleteRemovesOutputOnly(IngestWebCase):
    def published(self):
        out = self.out / "Andy Weir" / "Project Hail Mary" / "Project Hail Mary.m4b"
        out.parent.mkdir(parents=True)
        out.write_text("m4b")
        row = UnitState(
            path=KEY, status="converted", output=str(out), source="ingest", title="Project Hail Mary"
        )
        self.state.save(published_row(row))
        return out

    def test_output_removed_ingest_untouched_row_gone_key_dismissed(self):
        out = self.published()
        before = tree_snapshot(self.ingest_root)
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertFalse(out.exists())
        self.assertFalse(out.parent.exists())
        self.assertEqual(tree_snapshot(self.ingest_root), before)
        self.assertIsNone(self.state.get(KEY))
        self.assertTrue(self.state.is_dismissed(KEY))
        self.assertTrue(body["ok"])
        self.assertTrue(body["source_kept"])
        self.assertEqual(body["source_kept_reason"], SEEDING)
        self.assertTrue(body["dismissed"])
        self.assertFalse(body["source_removed"])
        self.assertEqual(body["output"], "removed")
        self.assertEqual(body["deleted"], KEY)

    def test_missing_output_still_forgets_and_dismisses(self):
        gone = self.out / "Andy Weir" / "Gone" / "Gone.m4b"  # never written
        self.state.save(UnitState(path=KEY, status="converted", output=str(gone), source="ingest"))
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["output"], "absent")
        self.assertIsNone(self.state.get(KEY))
        self.assertTrue(self.state.is_dismissed(KEY))
        self.assertTrue((self.ingest_root / TORRENT / "01.mp3").exists())

    def test_without_confirmation_nothing_changes(self):
        out = self.published()
        before = tree_snapshot(self.ingest_root)
        status, body = self.post({"action": "delete", "path": KEY})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "confirmation required")
        self.assertTrue(out.exists())
        self.assertIsNotNone(self.state.get(KEY))
        self.assertFalse(self.state.is_dismissed(KEY))
        self.assertEqual(tree_snapshot(self.ingest_root), before)

    def test_output_outside_output_root_is_refused_and_the_row_kept(self):
        stray = self.root / "elsewhere" / "Book" / "Book.m4b"
        stray.parent.mkdir(parents=True)
        stray.write_text("m4b")
        self.state.save(UnitState(path=KEY, status="converted", output=str(stray), source="ingest"))
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("output path escapes output root", body["error"])
        self.assertTrue(stray.exists())
        self.assertIsNotNone(self.state.get(KEY))
        self.assertFalse(self.state.is_dismissed(KEY))
        self.assertTrue((self.ingest_root / TORRENT / "01.mp3").exists())

    def test_library_output_outside_output_root_is_refused_too(self):
        stray = self.root / "elsewhere" / "Lib" / "Lib.m4b"
        stray.parent.mkdir(parents=True)
        stray.write_text("m4b")
        self.state.save(UnitState(path="Author/Book", status="converted", output=str(stray)))
        status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("output path escapes output root", body["error"])
        self.assertTrue(stray.exists())
        self.assertIsNotNone(self.state.get("Author/Book"))
        self.assertTrue((self.lib / "Author" / "Book" / "01.mp3").exists())

    def test_output_removal_failure_keeps_the_row_like_a_library_delete(self):
        # dev15: a failed removal is a failure - the row is kept and the key is NOT dismissed
        out = self.published()
        with mock.patch.object(web, "remove_trashed", side_effect=PermissionError(13, "Permission denied")):
            status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 500, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["output"], "failed")
        self.assertIn("EACCES", body["error"])
        self.assertTrue(out.exists())
        self.assertIsNotNone(self.state.get(KEY))
        self.assertFalse(self.state.is_dismissed(KEY))
        self.assertTrue((self.ingest_root / TORRENT / "01.mp3").exists())

    def test_shared_output_is_kept_with_the_usual_reason(self):
        out = self.published()
        self.state.save(UnitState(path="Author/Book", status="converted", output=str(out)))
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "kept")
        self.assertIn("also used by 1 other book", body["output_kept_reason"])
        self.assertTrue(out.exists())
        self.assertTrue(self.state.is_dismissed(KEY))


class TestScanSkipsDismissed(PipelineCase):
    def test_a_dismissed_ingest_unit_is_skipped_and_a_normal_one_processes(self):
        self.add_torrent()
        self.add_torrent("Second Torrent")
        self.state.dismiss(KEY)
        calls = []

        def record(state, unit, log=print, **_kw):
            calls.append(unit.state_key)
            state.save(UnitState(path=unit.state_key, status="review", source=unit.source))

        with mock.patch.object(daemon, "process_unit", side_effect=record):
            summary = daemon.scan_once(self.state)
        self.assertEqual(calls, ["ingest:Second Torrent"])
        self.assertEqual(summary["seen"], 1)
        self.assertIsNone(self.state.get(KEY))
        self.assertTrue(self.state.is_dismissed(KEY))  # its folder is still there: kept

    def test_a_forced_scan_still_skips_it(self):
        self.add_torrent()
        self.state.dismiss(KEY)
        with mock.patch.object(daemon, "process_unit", side_effect=AssertionError("processed")):
            summary = daemon.scan_once(self.state, force=True)
        self.assertEqual(summary["seen"], 0)

    def test_a_library_book_with_the_same_name_is_not_affected(self):
        touch(str(CONFIG.library_root / TORRENT / "01.mp3"))
        self.state.dismiss(TORRENT)  # never written by Delete, but a library key is never skipped
        calls = []
        with mock.patch.object(daemon, "process_unit", side_effect=lambda s, u, **_kw: calls.append(u.state_key)):
            daemon.scan_once(self.state)
        self.assertEqual(calls, [TORRENT])


class TestPruneDismissals(PipelineCase):
    def test_a_dismissal_whose_folder_is_gone_is_pruned(self):
        self.add_torrent()
        self.add_torrent("Second Torrent")
        self.state.dismiss(KEY)
        self.state.dismiss("ingest:Second Torrent")
        shutil.rmtree(self.ingest_root / TORRENT)  # the torrent was removed in qBittorrent
        with mock.patch.object(daemon, "process_unit", side_effect=AssertionError("processed")):
            daemon.scan_once(self.state)
        self.assertEqual(self.state.dismissed_keys(), {"ingest:Second Torrent"})

    def test_a_single_file_torrent_and_a_part_key_are_kept_while_on_disk(self):
        touch(str(self.ingest_root / "Solo (Unabridged).m4b"))
        self.add_torrent("Multi", files=("CD1/01.mp3",))
        self.state.dismiss("ingest:Solo (Unabridged).m4b")
        self.state.dismiss("ingest:Multi#foo")
        self.assertEqual(daemon.prune_dismissals(self.state), 0)
        (self.ingest_root / "Solo (Unabridged).m4b").unlink()
        shutil.rmtree(self.ingest_root / "Multi")
        self.assertEqual(daemon.prune_dismissals(self.state), 2)
        self.assertEqual(self.state.dismissed_keys(), set())

    def test_re_added_torrent_converts_again_after_pruning(self):
        self.state.dismiss(KEY)  # removed earlier; the torrent has since gone from qBittorrent
        daemon.scan_once(self.state)  # ingest is reachable and the folder is gone: pruned
        self.assertFalse(self.state.is_dismissed(KEY))
        self.add_torrent()  # added again later
        calls = []
        with mock.patch.object(daemon, "process_unit", side_effect=lambda s, u, **_kw: calls.append(u.state_key)):
            daemon.scan_once(self.state)
        self.assertEqual(calls, [KEY])

    def test_not_pruned_while_ingest_is_off(self):
        self.state.dismiss(KEY)  # no folder on disk at all
        CONFIG.ingest_dir = ""
        self.assertEqual(daemon.prune_dismissals(self.state), 0)
        daemon.scan_once(self.state)
        self.assertTrue(self.state.is_dismissed(KEY))

    def test_not_pruned_while_ingest_was_disabled_at_start(self):
        self.state.dismiss(KEY)
        ingest._DISABLED_AT_START = "INGEST_DIR overlaps OUTPUT_ROOT"
        daemon.scan_once(self.state)
        self.assertTrue(self.state.is_dismissed(KEY))

    def test_not_pruned_while_the_ingest_folder_is_unmounted(self):
        self.add_torrent()
        self.state.dismiss(KEY)
        shutil.rmtree(self.ingest_root)  # the mount went away mid-run
        daemon.scan_once(self.state)
        self.assertTrue(self.state.is_dismissed(KEY))

    def test_an_unreadable_ingest_folder_keeps_every_dismissal(self):
        self.state.dismiss(KEY)
        with mock.patch.object(daemon.os.path, "lexists", side_effect=PermissionError(13, "denied")):
            self.assertEqual(daemon.prune_dismissals(self.state), 0)
        self.assertTrue(self.state.is_dismissed(KEY))


if __name__ == "__main__":
    unittest.main()

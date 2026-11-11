"""0.16.0 ingest: an ingest unit goes through the same audit -> match -> convert -> publish path.

The Audible HTTP is faked at the catalog module (search/book/chapters), ffprobe at
pipeline.probe, and ffmpeg at encode.encode - exactly the seams the rest of the suite uses.
"""

import os
import pathlib
import shutil
import time
import unittest
from unittest import mock

from audiobind import catalog, daemon, encode, ingest, pipeline
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.encode import EncodeResult
from audiobind.probe import Probe
from audiobind.state import State, UnitState

try:
    from test_ingest_discovery import IngestConfigCase, touch, tree_snapshot
except ImportError:
    from tests.test_ingest_discovery import IngestConfigCase, touch, tree_snapshot

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

TORRENT = "Andy Weir - Project Hail Mary (2021) [MAM]"
KEY = "ingest:" + TORRENT
PHM = Product(asin="B08G9PRS1K", title="Project Hail Mary", authors=["Andy Weir"], hours=2.0, language="english")


class PipelineCase(IngestConfigCase):
    def setUp(self):
        super().setUp()
        self.state = State(self.root / "state")
        self.work = self.root / "work"
        self.saved_work = CONFIG.work_dir
        CONFIG.work_dir = self.work
        self.saved_dry = CONTROL._dry_run
        CONTROL.set_dry_run(False)
        self.encode_calls = []
        # the lookahead thread would reach the real catalog for a second unit in a scan
        prefetch = mock.patch.object(daemon.PREFETCH, "schedule", return_value=False)
        prefetch.start()
        self.addCleanup(prefetch.stop)

    def tearDown(self):
        # put write permission back first, or the temp dir cannot be cleaned up
        for dirpath, _dirs, _files in os.walk(self.ingest_root):
            os.chmod(dirpath, 0o755)
        CONTROL._dry_run = self.saved_dry
        CONFIG.work_dir = self.saved_work
        self.state.close()
        super().tearDown()

    def add_torrent(self, name=TORRENT, files=("01.mp3", "02.mp3")):
        for f in files:
            touch(str(self.ingest_root / name / f))

    def make_read_only(self):
        """The production mount is :ro - so is the test's, where the OS allows it."""
        for dirpath, _dirs, _files in os.walk(self.ingest_root):
            os.chmod(dirpath, 0o555)

    def fake_encode(self, unit, match, book, work_dir, out_path, log=print, copy_audio=False, plan=None):
        """Same contract as encode.encode: build in work_dir, then move into out_path."""
        self.encode_calls.append({"unit": unit, "work_dir": work_dir, "out_path": out_path})
        work_dir.mkdir(parents=True, exist_ok=True)
        temp = work_dir / "output.m4b"
        temp.write_bytes(b"m4b")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp), str(out_path))
        return EncodeResult(path=out_path, duration=7200.0, size=3, chapters=2, chapters_source="test")

    def scan(self, products=(PHM,), hours_per_file=3600.0):
        with (
            mock.patch.object(catalog, "search", return_value=list(products)),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=hours_per_file, codec="mp3",
                                                                    channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            return daemon.scan_once(self.state)


class TestIngestConverts(PipelineCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_a_high_confidence_ingest_book_is_published_to_the_output_root(self):
        self.add_torrent()
        self.make_read_only()
        before = tree_snapshot(self.ingest_root)

        summary = self.scan()

        self.assertEqual(summary["converted"], 1)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        self.assertEqual(row.source, "ingest")
        self.assertEqual(row.confidence, "HIGH")
        self.assertEqual(row.asin, "B08G9PRS1K")
        out = pathlib.Path(row.output)
        self.assertTrue(out.is_file())
        self.assertIn(CONFIG.output_root.resolve(), out.resolve().parents)
        self.assertTrue((out.parent / "metadata.json").is_file())
        # encoded in the work dir, published under OUTPUT_ROOT, sources read from the ingest root
        (call,) = self.encode_calls
        self.assertIn(self.work, call["work_dir"].parents)
        self.assertFalse(call["work_dir"].exists())  # the work dir is cleaned up afterwards
        self.assertTrue(all(f.startswith(str(self.ingest_root)) for f in call["unit"].files))
        # the torrent is still seeding: not one byte, name or mode under the ingest root changed
        self.assertEqual(tree_snapshot(self.ingest_root), before)
        # no library row was made for it
        self.assertIsNone(self.state.get(TORRENT))

    def test_tier_gating_is_the_library_rule(self):
        self.add_torrent()
        with mock.patch.object(CONFIG, "auto_convert", "off"):
            self.scan()
        self.assertEqual(self.state.get(KEY).status, "review")
        self.assertEqual(self.encode_calls, [])

    def test_runtime_disagreement_is_held_like_a_library_book(self):
        self.add_torrent()
        long_edition = Product(asin="B0LONG0001", title="Project Hail Mary", authors=["Andy Weir"],
                               hours=16.0, language="english")
        self.scan(products=(long_edition,))
        row = self.state.get(KEY)
        self.assertEqual(row.status, "held")
        self.assertEqual(self.encode_calls, [])

    def test_no_match_lands_in_review(self):
        self.add_torrent()
        self.scan(products=())
        row = self.state.get(KEY)
        self.assertEqual(row.status, "review")
        self.assertEqual(row.source, "ingest")

    def test_dry_run_records_ready_never_converted(self):
        self.add_torrent()
        CONTROL.set_dry_run(True)
        self.scan()
        row = self.state.get(KEY)
        self.assertEqual(row.status, "ready")
        self.assertIn("dry run", row.note)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual(list(CONFIG.output_root.iterdir()), [])

    def test_library_and_ingest_copies_of_one_book_are_separate_rows(self):
        self.add_torrent()
        touch(str(CONFIG.library_root / TORRENT / "01.mp3"))
        CONTROL.set_dry_run(True)
        self.scan()
        self.assertEqual(self.state.get(TORRENT).source, "library")
        self.assertEqual(self.state.get(KEY).source, "ingest")


class TestIngestDisabled(PipelineCase):
    def test_unset_ingest_dir_never_looks_at_the_folder(self):
        CONFIG.ingest_dir = ""
        self.add_torrent()
        touch(str(CONFIG.library_root / "Author" / "Book" / "01.mp3"))
        CONTROL.set_dry_run(True)
        with mock.patch.object(ingest, "discover_ingest_units", side_effect=AssertionError("scanned ingest")):
            summary = self.scan()
        self.assertEqual(summary["seen"], 1)
        self.assertEqual([u.path for u in self.state.all()], ["Author/Book"])
        self.assertEqual(self.state.get("Author/Book").source, "library")


class TestIngestVanishes(PipelineCase):
    def test_a_torrent_removed_between_scans_keeps_its_row_and_crashes_nothing(self):
        self.add_torrent()
        self.scan(products=())  # first scan: review
        self.assertEqual(self.state.get(KEY).status, "review")
        shutil.rmtree(self.ingest_root / TORRENT)  # qBittorrent removed the torrent
        with mock.patch.object(daemon, "process_unit", side_effect=AssertionError("processed")):
            summary = daemon.scan_once(self.state, force=True)
        self.assertEqual(summary["seen"], 0)
        self.assertEqual(self.state.get(KEY).status, "review")  # nothing recorded over it

    def test_a_torrent_removed_after_listing_is_skipped(self):
        self.add_torrent()
        listed = ingest.discover()
        shutil.rmtree(self.ingest_root / TORRENT)
        with (
            mock.patch.object(ingest, "discover", return_value=listed),
            mock.patch.object(daemon, "process_unit", side_effect=AssertionError("processed")),
        ):
            summary = daemon.scan_once(self.state)
        self.assertEqual(summary["seen"], 0)
        self.assertIsNone(self.state.get(KEY))

    def test_files_vanishing_mid_audit_skip_one_book_not_the_scan(self):
        self.add_torrent()
        self.add_torrent("Second Torrent")
        calls = []

        def flaky(state, unit, log=print, **_kw):
            calls.append(unit.state_key)
            if unit.rel_path == TORRENT:
                raise FileNotFoundError(2, "No such file or directory", unit.files[0])
            state.save(UnitState(path=unit.state_key, status="review"))

        with mock.patch.object(daemon, "process_unit", side_effect=flaky):
            daemon.scan_once(self.state)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.state.get(KEY))
        self.assertEqual(self.state.get("ingest:Second Torrent").status, "review")


class TestIngestManualActions(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.ingest_root = self.root / "ingest"
        self.ingest_root.mkdir()
        self.saved_ingest = (CONFIG.ingest_dir, ingest._DISABLED_AT_START)
        CONFIG.ingest_dir = str(self.ingest_root)
        ingest._DISABLED_AT_START = ""

    def tearDown(self):
        CONFIG.ingest_dir, ingest._DISABLED_AT_START = self.saved_ingest
        super().tearDown()

    def test_retry_runs_the_ingest_unit_from_the_ingest_root(self):
        touch(str(self.ingest_root / TORRENT / "01.mp3"))
        self.state.save(UnitState(path=KEY, status="review"))
        seen = []
        with mock.patch("audiobind.web.run_manual", side_effect=lambda state, unit, force=False: seen.append(unit)):
            status, body = self.post({"action": "retry", "path": KEY})
            for _ in range(50):
                if seen:
                    break
                time.sleep(0.02)
        CONTROL.dequeue(KEY)
        self.assertEqual(status, 200, body)
        self.assertEqual(seen[0].source, "ingest")
        self.assertEqual(seen[0].state_key, KEY)
        self.assertTrue(seen[0].path.startswith(str(self.ingest_root)))

    def test_retry_on_a_vanished_torrent_says_so(self):
        self.state.save(UnitState(path=KEY, status="review"))
        status, body = self.post({"action": "retry", "path": KEY})
        self.assertEqual(status, 404)
        self.assertIn("no longer in the ingest folder", body["error"])
        self.assertEqual(self.state.get(KEY).status, "review")


if __name__ == "__main__":
    unittest.main()

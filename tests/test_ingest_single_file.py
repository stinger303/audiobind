"""0.16.0-dev3: a one-file torrent lands as a bare audio file in the ingest root - it is a book.

Observed live: ``The Guilty Will Robie, Book 4 (Unabridged).m4b`` (351 MB) sat directly in
INGEST_DIR and was never discovered, because only direct-child folders were units.
"""

import os
import pathlib
import shutil
import unittest
from unittest import mock

from audiobind import catalog, daemon, ingest
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.units import AUDIO_EXT, discover_ingest_units, discover_units, is_single_file, owned_paths

try:
    from test_ingest_discovery import IngestConfigCase, touch, tree_snapshot
    from test_ingest_pipeline import PipelineCase
except ImportError:
    from tests.test_ingest_discovery import IngestConfigCase, touch, tree_snapshot
    from tests.test_ingest_pipeline import PipelineCase

ROBIE = "The Guilty Will Robie, Book 4 (Unabridged).m4b"
SINGLE = "Andy Weir - Project Hail Mary.m4b"
SINGLE_KEY = "ingest:" + SINGLE
PHM = Product(asin="B08G9PRS1K", title="Project Hail Mary", authors=["Andy Weir"], hours=2.0, language="english")


class TestSingleFileDiscovery(IngestConfigCase):
    def units(self):
        return {u.rel_path: u for u in discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)}

    def test_a_bare_audio_file_in_the_root_is_a_unit(self):
        touch(str(self.ingest_root / ROBIE))
        unit = self.units()[ROBIE]
        self.assertEqual(unit.source, "ingest")
        self.assertEqual(unit.state_key, "ingest:" + ROBIE)
        self.assertEqual(unit.path, str(self.ingest_root / ROBIE))
        self.assertEqual(unit.files, [str(self.ingest_root / ROBIE)])
        self.assertFalse(unit.multi_part)
        self.assertTrue(is_single_file(unit))
        self.assertEqual(owned_paths(unit), [unit.path])

    def test_the_title_is_the_file_name_without_its_extension(self):
        touch(str(self.ingest_root / SINGLE))
        unit = self.units()[SINGLE]
        self.assertEqual(unit.name, "Andy Weir - Project Hail Mary")
        self.assertEqual(unit.author_guess, "Andy Weir")
        self.assertEqual(unit.title_guess, "Project Hail Mary")
        self.assertNotIn("m4b", unit.title_guess.lower())

    def test_every_known_audio_extension_counts_and_other_files_do_not(self):
        for ext in (".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".wav"):
            self.assertIn(ext, AUDIO_EXT)
            touch(str(self.ingest_root / f"Book{ext}"))
        touch(str(self.ingest_root / "Book.M4B"))  # extension case does not matter
        touch(str(self.ingest_root / "readme.txt"), b"txt")
        touch(str(self.ingest_root / "cover.jpg"), b"jpg")
        touch(str(self.ingest_root / "Book.m4b.!qB"))  # still downloading in qBittorrent
        rels = sorted(self.units())
        self.assertEqual(
            rels, sorted(["Book.m4b", "Book.m4a", "Book.mp3", "Book.flac", "Book.ogg", "Book.wav", "Book.M4B"])
        )

    def test_neighbouring_files_are_separate_books_never_grouped(self):
        # in the library, "Foo Part 1.mp3" + "Foo Part 2.mp3" would group; in the ingest root each
        # bare file is its own torrent, and a folder of the same name is a third book
        touch(str(self.ingest_root / "Foo Part 1.mp3"))
        touch(str(self.ingest_root / "Foo Part 2.mp3"))
        touch(str(self.ingest_root / "Foo" / "01.mp3"))
        units = self.units()
        self.assertEqual(sorted(units), ["Foo", "Foo Part 1.mp3", "Foo Part 2.mp3"])
        self.assertTrue(all(not u.key and not u.multi_part for u in units.values()))
        self.assertFalse(is_single_file(units["Foo"]))

    def test_hidden_and_ignored_files_are_skipped(self):
        touch(str(self.ingest_root / ".partial.m4b"))
        with mock.patch.object(CONFIG, "ignore_globs", ["Skip Me.mp3"]):
            touch(str(self.ingest_root / "Skip Me.mp3"))
            touch(str(self.ingest_root / "Keep Me.mp3"))
            self.assertEqual(sorted(self.units()), ["Keep Me.mp3"])

    def test_find_and_source_dir_work_for_a_file_key(self):
        touch(str(self.ingest_root / ROBIE))
        unit = ingest.find_any_unit("ingest:" + ROBIE)
        self.assertIsNotNone(unit)
        self.assertEqual(unit.files, [str(self.ingest_root / ROBIE)])
        self.assertEqual(ingest.source_dir("ingest:" + ROBIE), str(self.ingest_root / ROBIE))

    def test_discovery_never_writes_under_the_ingest_root(self):
        touch(str(self.ingest_root / ROBIE))
        before = tree_snapshot(self.ingest_root)
        self.units()
        ingest.discover()
        ingest.find_any_unit("ingest:" + ROBIE)
        self.assertEqual(tree_snapshot(self.ingest_root), before)

    def test_library_loose_files_are_unchanged(self):
        # the library has always treated loose root files as units; its naming is untouched
        touch(str(CONFIG.library_root / "loose.mp3"))
        touch(str(CONFIG.library_root / "Foo Part 1.mp3"))
        touch(str(CONFIG.library_root / "Foo Part 2.mp3"))
        units = {u.rel_path: u for u in discover_units(CONFIG.library_root, CONFIG.ignore_globs)}
        self.assertIn("loose.mp3", units)
        self.assertTrue(all(u.source == "library" for u in units.values()))
        grouped = [u for u in units.values() if u.multi_part]
        self.assertEqual(len(grouped), 1)  # library grouping of loose part files still applies


class TestSingleFilePipeline(PipelineCase):
    def add_single(self, name=SINGLE):
        touch(str(self.ingest_root / name))

    def test_a_single_file_torrent_converts_and_publishes(self):
        self.add_single()
        before = tree_snapshot(self.ingest_root)
        summary = self.scan(hours_per_file=7200.0)
        self.assertEqual(summary["converted"], 1)
        row = self.state.get(SINGLE_KEY)
        self.assertEqual(row.status, "converted")
        self.assertEqual(row.source, "ingest")
        self.assertEqual(row.asin, "B08G9PRS1K")
        out = pathlib.Path(row.output)
        self.assertTrue(out.is_file())
        self.assertIn(CONFIG.output_root.resolve(), out.resolve().parents)
        (call,) = self.encode_calls
        self.assertEqual(call["unit"].files, [str(self.ingest_root / SINGLE)])
        self.assertEqual(tree_snapshot(self.ingest_root), before)

    def test_a_single_file_m4b_of_aac_is_remuxed_not_reencoded(self):
        self.add_single()
        CONTROL.set_dry_run(True)
        from audiobind import pipeline
        from audiobind.probe import Probe

        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=7200.0, codec="aac", channels="2")),
        ):
            unit = ingest.find_any_unit(SINGLE_KEY)
            audit = pipeline.audit_unit(unit)
        self.assertEqual(audit.action, "remux")

    def test_a_removed_single_file_torrent_is_skipped(self):
        self.add_single()
        listed = ingest.discover()
        os.remove(self.ingest_root / SINGLE)
        with (
            mock.patch.object(ingest, "discover", return_value=listed),
            mock.patch.object(daemon, "process_unit", side_effect=AssertionError("processed")),
        ):
            summary = daemon.scan_once(self.state)
        self.assertEqual(summary["seen"], 0)
        self.assertIsNone(self.state.get(SINGLE_KEY))

    def test_a_present_single_file_is_not_mistaken_for_gone(self):
        self.add_single()
        (unit,) = ingest.discover()
        self.assertFalse(daemon._ingest_source_gone(unit))
        shutil.rmtree(self.ingest_root)
        self.assertTrue(daemon._ingest_source_gone(unit))
        self.ingest_root.mkdir()


if __name__ == "__main__":
    unittest.main()

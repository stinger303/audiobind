"""0.16.0 ingest: config default, the source column migration, and read-only discovery."""

import logging
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from audiobind import config, ingest
from audiobind.config import CONFIG, Config
from audiobind.state import State, UnitState, key_source
from audiobind.units import discover_ingest_units, discover_units


def touch(path, data=b"audio"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def tree_snapshot(root):
    """Every entry under root with its size and mtime - proof that nothing was written."""
    found = {}
    for dirpath, dirnames, files in os.walk(root):
        for name in dirnames + files:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            found[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return found


class IngestConfigCase(unittest.TestCase):
    """Points CONFIG.ingest_dir somewhere for one test, then puts everything back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.ingest_root = self.root / "ingest"
        self.ingest_root.mkdir()
        self.saved = (CONFIG.ingest_dir, CONFIG.library_root, CONFIG.output_root, ingest._DISABLED_AT_START)
        CONFIG.library_root = self.root / "library"
        CONFIG.output_root = self.root / "output"
        CONFIG.library_root.mkdir()
        CONFIG.output_root.mkdir()
        CONFIG.ingest_dir = str(self.ingest_root)
        ingest._DISABLED_AT_START = ""

    def tearDown(self):
        CONFIG.ingest_dir, CONFIG.library_root, CONFIG.output_root, ingest._DISABLED_AT_START = self.saved
        self.tmp.cleanup()


class TestIngestOffByDefault(unittest.TestCase):
    def test_unset_env_means_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Config.from_env().ingest_dir, "")

    def test_env_is_read(self):
        with mock.patch.dict(os.environ, {"INGEST_DIR": " /ingest "}, clear=True):
            self.assertEqual(Config.from_env().ingest_dir, "/ingest")

    def test_off_means_no_root_and_no_units(self):
        with mock.patch.object(CONFIG, "ingest_dir", ""):
            self.assertIsNone(ingest.ingest_root())
            self.assertEqual(ingest.discover(), [])
            self.assertFalse(ingest.check_at_start())

    def test_the_setting_is_read_through_the_effective_getter(self):
        with (
            mock.patch.object(CONFIG, "ingest_dir", "/raw-config-value"),
            mock.patch.object(ingest, "effective_val", return_value="/effective") as getter,
            mock.patch.object(ingest, "_DISABLED_AT_START", ""),
        ):
            self.assertEqual(ingest.ingest_root(), pathlib.Path("/effective"))
        getter.assert_called_with("ingest_dir")

    def test_library_keys_and_rows_are_unchanged(self):
        self.assertEqual(key_source("Author/Book"), "library")
        self.assertEqual(key_source("Author#foo"), "library")
        self.assertEqual(key_source("ingest:Author - Book"), "ingest")
        self.assertEqual(config.effective_val("ingest_dir"), CONFIG.ingest_dir)


class TestSourceMigration(unittest.TestCase):
    OLD_SCHEMA = """CREATE TABLE units (
        path TEXT PRIMARY KEY, status TEXT NOT NULL, confidence TEXT, asin TEXT, title TEXT,
        author TEXT, hours REAL, files INTEGER, output TEXT, note TEXT, updated_at REAL,
        src_mb REAL DEFAULT 0, out_mb REAL DEFAULT 0, chapters INTEGER DEFAULT 0,
        detail TEXT DEFAULT '', asin_override TEXT DEFAULT '', overrides TEXT DEFAULT '',
        alternates TEXT DEFAULT '')"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def columns(self):
        con = sqlite3.connect(self.dir / "audiobind.db")
        try:
            return {row[1] for row in con.execute("PRAGMA table_info(units)")}
        finally:
            con.close()

    def test_a_pre_ingest_database_gains_the_column_and_keeps_its_rows(self):
        con = sqlite3.connect(self.dir / "audiobind.db")
        con.execute(self.OLD_SCHEMA)
        con.execute(
            "INSERT INTO units (path, status, title, updated_at) VALUES ('Old/Book', 'converted', 'Old', 1.0)"
        )
        con.commit()
        con.close()
        self.assertNotIn("source", self.columns())

        state = State(self.dir)
        try:
            row = state.get("Old/Book")
            self.assertEqual(row.status, "converted")
            self.assertEqual(row.title, "Old")
            self.assertEqual(row.source, "library")
        finally:
            state.close()
        self.assertIn("source", self.columns())

        # opening again is a no-op: the pragma check guards the ALTER
        State(self.dir).close()

    def test_save_stores_the_source_the_key_names(self):
        state = State(self.dir)
        try:
            state.save(UnitState(path="ingest:Author - Book", status="review"))
            state.save(UnitState(path="Author/Book", status="review", source="ingest"))  # cannot lie
            self.assertEqual(state.get("ingest:Author - Book").source, "ingest")
            self.assertEqual(state.get("Author/Book").source, "library")
            state.set_overrides("ingest:New", {"title": "T"})
            self.assertEqual(state.get("ingest:New").source, "ingest")
        finally:
            state.close()

    def test_same_folder_name_in_both_roots_is_two_rows(self):
        state = State(self.dir)
        try:
            state.save(UnitState(path="Author - Book", status="converted"))
            state.save(UnitState(path="ingest:Author - Book", status="review"))
            self.assertEqual(state.get("Author - Book").status, "converted")
            self.assertEqual(state.get("ingest:Author - Book").status, "review")
            self.assertEqual(len(state.all()), 2)
        finally:
            state.close()


class TestIngestDiscovery(IngestConfigCase):
    def build(self):
        r = self.ingest_root
        touch(str(r / "Brandon Sanderson - The Way of Kings (2010) [MAM v5] {B003ZWFO7E}" / "01.mp3"))
        touch(str(r / "Brandon Sanderson - The Way of Kings (2010) [MAM v5] {B003ZWFO7E}" / "02.mp3"))
        touch(str(r / "Andy Weir - Project Hail Mary" / "CD1" / "a.mp3"))
        touch(str(r / "Andy Weir - Project Hail Mary" / "CD2" / "b.mp3"))
        touch(str(r / "Andy Weir - Project Hail Mary" / "cover.jpg"), b"jpg")
        touch(str(r / "stray-file.mp3"))  # a loose file is not a torrent folder
        touch(str(r / ".incomplete" / "Some Book" / "01.mp3"))  # hidden folder
        touch(str(r / "Linux ISO" / "distro.iso"), b"iso")  # no audio at all
        touch(str(r / "notes.txt"), b"txt")

    def test_direct_child_dirs_with_audio_are_units(self):
        self.build()
        units = discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)
        rels = sorted(u.rel_path for u in units)
        self.assertEqual(
            rels,
            [
                "Andy Weir - Project Hail Mary",
                "Brandon Sanderson - The Way of Kings (2010) [MAM v5] {B003ZWFO7E}",
            ],
        )
        for unit in units:
            self.assertEqual(unit.source, "ingest")
            self.assertEqual(unit.state_key, "ingest:" + unit.rel_path)

    def test_stray_files_and_dot_dirs_are_ignored(self):
        self.build()
        keys = {u.rel_path for u in discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)}
        self.assertFalse(any("stray" in k or "incomplete" in k or "Linux" in k for k in keys))

    def test_disc_folders_inside_a_torrent_group_into_one_book(self):
        self.build()
        units = {u.rel_path: u for u in discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)}
        phm = units["Andy Weir - Project Hail Mary"]
        self.assertTrue(phm.multi_part)
        self.assertEqual([p.name for p in phm.parts], ["CD1", "CD2"])
        self.assertEqual(phm.author_guess, "Andy Weir")
        self.assertEqual(phm.title_guess, "Project Hail Mary")

    def test_messy_torrent_names_use_the_existing_parser(self):
        self.build()
        units = {u.rel_path: u for u in discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)}
        wok = units["Brandon Sanderson - The Way of Kings (2010) [MAM v5] {B003ZWFO7E}"]
        self.assertEqual(wok.author_guess, "Brandon Sanderson")
        self.assertTrue(wok.title_guess.startswith("The Way of Kings"))

    def test_discovery_never_writes_under_the_ingest_root(self):
        self.build()
        before = tree_snapshot(self.ingest_root)
        discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)
        ingest.discover()
        ingest.find_any_unit("ingest:Andy Weir - Project Hail Mary")
        self.assertEqual(tree_snapshot(self.ingest_root), before)

    def test_library_discovery_is_unchanged_by_the_refactor(self):
        lib = CONFIG.library_root
        touch(str(lib / "loose.mp3"))
        touch(str(lib / "Author" / "Book" / "01.mp3"))
        touch(str(lib / "Author" / "Foo Part 1" / "01.mp3"))
        touch(str(lib / "Author" / "Foo Part 2" / "01.mp3"))
        units = discover_units(lib, CONFIG.ignore_globs)
        # loose files stay units in the library, and shared-parent buckets keep their "#key"
        self.assertEqual(sorted(u.rel_path for u in units), ["Author#foo", "Author/Book", "loose.mp3"])
        self.assertTrue(all(u.source == "library" and u.state_key == u.rel_path for u in units))

    def test_find_any_unit_dispatches_on_the_key(self):
        self.build()
        touch(str(CONFIG.library_root / "Andy Weir - Project Hail Mary" / "01.mp3"))
        lib_unit = ingest.find_any_unit("Andy Weir - Project Hail Mary")
        ing_unit = ingest.find_any_unit("ingest:Andy Weir - Project Hail Mary")
        self.assertEqual(lib_unit.source, "library")
        self.assertTrue(lib_unit.path.startswith(str(CONFIG.library_root)))
        self.assertEqual(ing_unit.source, "ingest")
        self.assertTrue(ing_unit.path.startswith(str(self.ingest_root)))
        self.assertIsNone(ingest.find_any_unit("ingest:Not There"))

    def test_source_dir_is_the_container_path(self):
        self.assertEqual(ingest.source_dir("ingest:Author - Book"), str(self.ingest_root / "Author - Book"))
        self.assertEqual(ingest.source_dir("ingest:Torrent#parts"), str(self.ingest_root / "Torrent"))


class TestIngestStartCheck(IngestConfigCase):
    def test_existing_dir_enables(self):
        self.assertTrue(ingest.check_at_start())
        self.assertEqual(ingest.ingest_root(), self.ingest_root)

    def test_missing_dir_warns_and_keeps_running_disabled(self):
        CONFIG.ingest_dir = str(self.root / "not-mounted")
        with self.assertLogs("audiobind", level=logging.WARNING) as logs:
            self.assertFalse(ingest.check_at_start())
        self.assertIn("does not exist", "\n".join(logs.output))
        self.assertIn("DISABLED", "\n".join(logs.output))
        self.assertIsNone(ingest.ingest_root())
        # disabled for the run: the folder appearing later does not switch it on mid-run
        (self.root / "not-mounted").mkdir()
        self.assertIsNone(ingest.ingest_root())
        self.assertEqual(ingest.discover(), [])

    def test_overlapping_output_or_library_is_refused(self):
        for overlap in (CONFIG.output_root, CONFIG.output_root / "sub", CONFIG.library_root):
            overlap.mkdir(exist_ok=True)
            CONFIG.ingest_dir = str(overlap)
            with self.assertLogs("audiobind", level=logging.WARNING):
                self.assertFalse(ingest.check_at_start(), overlap)
            self.assertIsNone(ingest.ingest_root())

    def test_folder_vanishing_mid_run_skips_ingest_without_crashing(self):
        self.assertTrue(ingest.check_at_start())
        self.ingest_root.rmdir()
        with self.assertLogs("audiobind", level=logging.WARNING):
            self.assertEqual(ingest.discover(), [])


if __name__ == "__main__":
    unittest.main()

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobind import drift
from audiobind.config import CONFIG
from audiobind.state import State, UnitState


class DriftCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lib = self.root / "library"
        self.out = self.root / "output"
        self.lib.mkdir()
        self.out.mkdir()
        self.state = State(self.root / "state")
        self.addCleanup(self.state.close)
        settings = patch.multiple(CONFIG, library_root=self.lib, output_root=self.out, ingest_dir="")
        settings.start()
        self.addCleanup(settings.stop)

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else None
            for path in self.root.rglob("*")
            if path.name != "audiobind.db-shm"  # SQLite read marks, not persisted database content
        }


class TestDrift(DriftCase):
    def test_three_contradictions_and_no_writes(self):
        (self.lib / "z orphan").mkdir()
        (self.lib / "a orphan").mkdir()
        (self.lib / "ingest:known").mkdir()
        self.state.save(UnitState("./ingest:known", "ready", output=str(self.out / "gone.m4b")))
        self.state.save(UnitState("old name", "held"))
        self.state.save(UnitState("ingest:external", "review"))
        before = self.snapshot()
        self.assertEqual(drift.report(self.state), {
            "missing_sources": [{"path": "old name"}],
            "missing_outputs": [{"path": "./ingest:known", "output": str(self.out / "gone.m4b")}],
            "orphans": [{"path": "a orphan"}, {"path": "z orphan"}],
            "truncated": False,
        })
        self.assertEqual(self.snapshot(), before)

    def test_empty_consistent_library(self):
        before = self.snapshot()
        self.assertEqual(drift.report(self.state), {
            "missing_sources": [], "missing_outputs": [], "orphans": [], "truncated": False,
        })
        self.assertEqual(self.snapshot(), before)

    def test_limit_counts_visited_directories_and_skips_noise_and_symlinks(self):
        (self.lib / "a" / "nested").mkdir(parents=True)
        (self.lib / "b").mkdir()
        (self.lib / "@eaDir" / "noise").mkdir(parents=True)
        (self.lib / "custom-trash" / "noise").mkdir(parents=True)
        (self.lib / ".hidden").mkdir()
        (self.lib / "link").symlink_to(self.lib / "b", target_is_directory=True)
        self.state.save(UnitState("a", "held"))
        with patch.object(CONFIG, "ignore_globs", ["@eaDir", "custom-trash"]):
            limited = drift.report(self.state, limit=2)
            exact = drift.report(self.state, limit=3)
            zero = drift.report(self.state, limit=0)
        self.assertEqual(limited["orphans"], [{"path": "a/nested"}])
        self.assertTrue(limited["truncated"])
        self.assertEqual(exact["orphans"], [{"path": "a/nested"}, {"path": "b"}])
        self.assertFalse(exact["truncated"])
        self.assertEqual(zero["orphans"], [])
        self.assertTrue(zero["truncated"])

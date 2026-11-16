"""0.21.0-dev8: a re-save never wipes the detail blob, detail_for_asin finds pinned books,
and the database runs in WAL mode (converting an existing rollback-journal file)."""

import json
import pathlib
import sqlite3
import tempfile
import unittest

from audiobind.state import State, UnitState

BLOB = json.dumps({"match": {"title": "Dune", "image": "https://example.invalid/c.jpg"}, "reopen": True})


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()


class TestDetailSurvivesResave(_Base):
    def test_fresh_unitstate_without_detail_keeps_the_blob(self):
        self.state.save(UnitState(path="A/Dune", status="converted", title="Dune", detail=BLOB))
        self.state.save(UnitState(path="A/Dune", status="review", title="Dune", detail=""))
        row = self.state.get("A/Dune")
        self.assertEqual(row.detail, BLOB)
        self.assertEqual(row.status, "review")  # the rest of the row still updates

    def test_a_new_blob_still_replaces_the_old_one(self):
        self.state.save(UnitState(path="A/Dune", status="converted", detail=BLOB))
        newer = json.dumps({"match": {"title": "Dune Messiah"}})
        self.state.save(UnitState(path="A/Dune", status="converted", detail=newer))
        self.assertEqual(self.state.get("A/Dune").detail, newer)

    def test_clear_detail_removes_it(self):
        self.state.save(UnitState(path="A/Dune", status="converted", detail=BLOB))
        self.assertTrue(self.state.clear_detail("A/Dune"))
        self.assertEqual(self.state.get("A/Dune").detail, "")
        self.assertFalse(self.state.clear_detail("A/Dune"))  # nothing left to clear
        self.assertFalse(self.state.clear_detail("nowhere"))


class TestDetailForAsin(_Base):
    def test_pinned_but_never_matched(self):
        self.state.save(UnitState(path="P/Pinned", status="review", asin="", detail=BLOB))
        self.state.set_asin_override("P/Pinned", "B0PIN00001")
        self.assertEqual(self.state.get("P/Pinned").asin, "")
        self.assertEqual(self.state.detail_for_asin("B0PIN00001"), BLOB)

    def test_matched_only(self):
        self.state.save(UnitState(path="M/Matched", status="converted", asin="B0MATCH001", detail=BLOB))
        self.assertEqual(self.state.detail_for_asin("B0MATCH001"), BLOB)

    def test_unknown_or_empty_asin(self):
        self.state.save(UnitState(path="M/Matched", status="converted", asin="B0MATCH001", detail=BLOB))
        self.assertEqual(self.state.detail_for_asin("B0NOPE0000"), "")
        self.assertEqual(self.state.detail_for_asin(""), "")


class TestWal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    @staticmethod
    def _mode(state: State) -> str:
        return state.connection.execute("PRAGMA journal_mode").fetchone()[0].lower()

    def test_first_run_creates_the_directory_and_uses_wal(self):
        target = self.root / "nested" / "state"
        state = State(target)
        try:
            self.assertTrue((target / "audiobind.db").is_file())
            self.assertEqual(self._mode(state), "wal")
            self.assertIsNone(state.get("anything"))  # schema applied
        finally:
            state.close()

    def test_existing_rollback_journal_database_converts(self):
        # the pre-dev8 constructor: no journal_mode pragma, so SQLite's default rollback journal
        old = sqlite3.connect(self.root / "audiobind.db", isolation_level=None)
        old.execute("PRAGMA journal_mode=DELETE")
        old.execute("CREATE TABLE units (path TEXT PRIMARY KEY, status TEXT, updated_at REAL)")
        old.execute("INSERT INTO units (path, status, updated_at) VALUES ('Old/Book', 'converted', 1)")
        self.assertEqual(old.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
        old.close()

        state = State(self.root)
        try:
            self.assertEqual(self._mode(state), "wal")
            self.assertEqual(state.get("Old/Book").status, "converted")  # data and migration intact
        finally:
            state.close()
        # persistent: a plain connection sees WAL without asking for it
        plain = sqlite3.connect(self.root / "audiobind.db")
        try:
            self.assertEqual(plain.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            plain.close()

    def test_write_then_read_on_the_same_connection(self):
        state = State(self.root)
        try:
            state.save(UnitState(path="W/Book", status="converted", title="Book", detail=BLOB))
            row = state.get("W/Book")
            self.assertEqual((row.title, row.detail), ("Book", BLOB))
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()

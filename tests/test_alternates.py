"""Alternate editions: matching keeps runners-up, state persists them, panel shows them."""
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from audiobind.catalog import Product
from audiobind.matching import match_unit
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part


def product(asin, title, author="Michael Lewis", hours=10.0):
    return Product(asin=asin, title=title, authors=[author], hours=hours,
                   language="english")


def unit():
    return BookUnit(path="L/M", name="M", parent="L",
                    parts=[Part(name="M", files=["/src/a.mp3"])],
                    author_guess="Michael Lewis", title_guess="Moneyball")


class TestMatchingAlternates(unittest.TestCase):
    def _match(self, products):
        with patch("audiobind.matching.catalog") as cat:
            cat.search.return_value = products
            return match_unit(unit())

    def test_runners_up_are_kept_best_excluded(self):
        best = product("B0BEST", "Moneyball")
        other = product("B0ALT1", "Moneyball (Unabridged)")
        match = self._match([best, other])
        self.assertEqual(match.product.asin, "B0BEST")
        self.assertEqual([a["asin"] for a in match.alternates], ["B0ALT1"])
        self.assertEqual(match.alternates[0]["title"], "Moneyball (Unabridged)")

    def test_top_few_only_and_ordered_by_score(self):
        products = [product(f"B0{i}", "Moneyball") for i in range(1, 7)]
        match = self._match(products)
        self.assertLessEqual(len(match.alternates), 4)
        scores = [a["score"] for a in match.alternates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_search_no_alternates(self):
        with patch("audiobind.matching.catalog") as cat:
            cat.book.return_value = {}
            match = match_unit(unit(), "B0PINNED")
        self.assertEqual(match.alternates, [])


class TestStateAlternates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_roundtrip_on_an_existing_unit(self):
        self.state.save(UnitState(path="L/M", status="review", title="M"))
        alts = [{"asin": "B0ALT1", "title": "Moneyball (Unabridged)",
                 "authors": ["Michael Lewis"], "score": 0.9}]
        self.state.set_alternates("L/M", alts)
        self.assertEqual(self.state.get_alternates("L/M"), alts)

    def test_default_is_empty_list(self):
        self.state.save(UnitState(path="L/M", status="review"))
        self.assertEqual(self.state.get_alternates("L/M"), [])

    def test_get_all_includes_alternates(self):
        self.state.save(UnitState(path="L/M", status="review"))
        self.state.set_alternates("L/M", [{"asin": "B0ALT1", "title": "T"}])
        self.assertEqual(self.state.get(path="L/M").alternates,
                         '[{"asin": "B0ALT1", "title": "T"}]')

    def test_migration_adds_the_column_to_a_pre_013_database(self):
        with tempfile.TemporaryDirectory() as old:
            db = pathlib.Path(old) / "audiobind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE units (path TEXT PRIMARY KEY, status TEXT, confidence TEXT,"
                " title TEXT, author TEXT, hours REAL, files INTEGER, src_mb REAL,"
                " out_mb REAL, chapters INTEGER, detail TEXT,"
                " asin_override TEXT DEFAULT '', overrides TEXT DEFAULT '',"
                " updated_at REAL)")
            conn.execute("INSERT INTO units (path, status) VALUES ('L/M', 'review')")
            conn.commit()
            conn.close()
            migrated = State(pathlib.Path(old))
            try:
                alts = [{"asin": "B0ALT1", "title": "Moneyball (Unabridged)"}]
                migrated.set_alternates("L/M", alts)
                self.assertEqual(migrated.get_alternates("L/M"), alts)
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()

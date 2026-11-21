"""M1b: the grading gates that could auto-convert (or download) the wrong book unseen.

Each case comes from AUDIT-matching-2026-11-20 (MATCHING-01..06). Unless a case says otherwise the
folder is "Avery Writer - <title>", every catalog search returns the case's products, both the
files and the catalog say 10 hours, and Audnex knows nothing.
"""

import unittest
from unittest import mock

from audiobind import catalog, matching
from audiobind.catalog import Product
from tests.test_match_fixtures import library_unit

AUTHOR = "Avery Writer"
KINGDOM = "Chronicles of the Northern Kingdom"


def product(asin, title, subtitle="", sequence="", hours=10.0, language="english", authors=(AUTHOR,)):
    return Product(asin=asin, title=title, subtitle=subtitle, authors=list(authors), sequence=sequence,
                   series="Realm" if sequence else "", hours=hours, language=language)


def run(folder, products, audnex=None, unit_hook=None):
    """match_unit for library folder ``folder`` against ``products`` (every search) and ``audnex``
    ({asin: record}, everything else unknown)."""
    audnex = audnex or {}
    unit = library_unit(f"{AUTHOR}/{folder}")
    if unit_hook:
        unit_hook(unit)
    with mock.patch.object(catalog, "search", lambda *a, **k: list(products)), \
            mock.patch.object(catalog, "book", lambda asin: dict(audnex.get(asin) or {})):
        return matching.match_unit(unit, local_hours=10.0)


class TestMatching01SeriesPosition(unittest.TestCase):
    """A different volume of the same series must never grade HIGH (it is vetoed: LOW, with the
    conflict named), whether the position sits in the title, the subtitle or the catalog sequence."""

    def assertVetoed(self, match, wrong_asin):
        self.assertEqual(match.asin, wrong_asin)
        self.assertEqual(match.confidence, "LOW", vars(match))
        self.assertIn("2", match.veto)
        self.assertIn("1", match.veto)

    def test_book_one_in_the_title_never_grades_high_for_book_two(self):
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [product("B0SYNK0001", f"{KINGDOM} Book 1")])
        self.assertVetoed(m, "B0SYNK0001")

    def test_bare_trailing_number_on_the_catalog_title_conflicts_with_an_explicit_book(self):
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [product("B0SYNK0001", f"{KINGDOM} 1")])
        self.assertVetoed(m, "B0SYNK0001")

    def test_position_in_the_subtitle(self):
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [product("B0SYNK0001", KINGDOM, subtitle="Realm, Book 1")])
        self.assertVetoed(m, "B0SYNK0001")

    def test_position_in_the_catalog_sequence(self):
        m = run(f"{AUTHOR} - {KINGDOM}, Book Two", [product("B0SYNK0001", KINGDOM, sequence="1")])
        self.assertVetoed(m, "B0SYNK0001")

    def test_the_right_volume_wins_over_a_vetoed_one(self):
        m = run(f"{AUTHOR} - {KINGDOM} Book 2",
                [product("B0SYNK0001", f"{KINGDOM} Book 1"), product("B0SYNK0002", f"{KINGDOM} Book 2")])
        self.assertEqual(m.asin, "B0SYNK0002")
        self.assertEqual(m.veto, "")

    def test_matching_positions_and_no_position_are_untouched(self):
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [product("B0SYNK0002", KINGDOM, subtitle="Realm, Book 2",
                                                          sequence="2")])
        self.assertEqual((m.confidence, m.veto), ("HIGH", ""))
        m = run(f"{AUTHOR} - {KINGDOM}", [product("B0SYNK0001", KINGDOM, sequence="1")])
        self.assertEqual((m.confidence, m.veto), ("HIGH", ""))
        # a number that is part of a title is not a position: nothing on the folder side names one
        m = run(f"{AUTHOR} - Fahrenheit 451", [product("B0SYNF0451", "Fahrenheit 451", sequence="1")])
        self.assertEqual((m.confidence, m.veto), ("HIGH", ""))

    def test_parser(self):
        p = matching.series_positions
        self.assertEqual(p("Rise of Empire - Riyria Revelations, Book 2")[0], {"2"})
        self.assertEqual(p("Heir of Novron - Riyira Revelations Book 03")[0], {"3"})
        self.assertEqual(p("Vol. II")[0], {"2"})
        self.assertEqual(p("Books 1-3")[0], set())
        self.assertEqual(p("Book 1-3")[0], set())
        self.assertEqual(p("The #1 New York Times Bestseller")[0], set())
        self.assertEqual(p("Kingdom 2"), (set(), {"2"}))
        self.assertEqual(p("13 Hours"), (set(), set()))
        self.assertEqual(p("Book 2.5")[0], {"2.5"})

    def test_the_veto_reaches_the_note_and_nothing_converts(self):
        key = f"{AUTHOR}/{AUTHOR} - {KINGDOM} Book 2"
        note, detail, status, converted = pipeline_run(key, [product("B0SYNK0001", f"{KINGDOM} Book 1")])
        self.assertIn("Book 1", note)
        self.assertIn("folder says", note)
        self.assertTrue(detail["match"]["veto"])
        self.assertEqual(converted, [])
        self.assertEqual(status, "review")


def pipeline_run(key, products, audnex=None, auto=("HIGH",)):
    """process_unit for library folder ``key``: (note, detail, status, convert calls)."""
    import json
    import tempfile
    from pathlib import Path

    from audiobind import pipeline
    from audiobind.probe import Probe
    from audiobind.state import State
    from audiobind.units import BookUnit, Part

    audnex = audnex or {}
    converted = []
    with tempfile.TemporaryDirectory() as tmp:
        state = State(Path(tmp) / "state")
        try:
            with mock.patch.object(pipeline, "probe", return_value=Probe(duration=600 * 60, codec="mp3", channels="2")), \
                    mock.patch.object(pipeline.os.path, "getsize", return_value=1), \
                    mock.patch.object(matching.catalog, "book", lambda asin: dict(audnex.get(asin) or {})), \
                    mock.patch.object(matching.catalog, "search", lambda *a, **k: list(products)), \
                    mock.patch.object(pipeline.catalog, "chapters", return_value=[]), \
                    mock.patch.object(pipeline, "effective_auto_tiers", return_value=set(auto)), \
                    mock.patch.object(pipeline, "convert_unit", side_effect=lambda audit, **_k: converted.append(1)):
                path = "/library/" + key
                unit = library_unit(key)
                unit.parts = [Part("x", [path + "/01.mp3"])]
                pipeline.process_unit(state, unit, log=lambda *_: None)
            row = state.get(unit.rel_path)
            return row.note, json.loads(row.detail), row.status, converted
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()

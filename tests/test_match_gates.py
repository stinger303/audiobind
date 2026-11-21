"""M1b: the grading gates that could auto-convert (or download) the wrong book unseen.

Each case comes from AUDIT-matching-2026-11-20 (MATCHING-02..06). Unless a case says otherwise the
folder is "Avery Writer - <title>", every catalog search returns the case's products, both the
files and the catalog say 10 hours, and Audnex knows nothing.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import catalog, markers, matching
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


def pipeline_run(key, products, audnex=None, auto=("HIGH",)):
    """process_unit for library folder ``key``: (note, detail, status, convert calls)."""
    from audiobind import pipeline
    from audiobind.probe import Probe
    from audiobind.state import State
    from audiobind.units import Part

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


class TestMatching02EditionMarkers(unittest.TestCase):
    """A box set, an abridgment or a synopsis must never grade HIGH as the plain edition (nor the
    plain edition for a folder that names one): the markers are read before brackets are removed."""

    def assertHeld(self, match, asin):
        self.assertEqual(match.asin, asin)
        self.assertEqual(match.confidence, "LOW", vars(match))
        self.assertTrue(match.edition_conflict)

    def test_bracketed_marker_on_a_sole_candidate(self):
        for tag in ("(Abridged)", "[Synopsis]", "(Box Set)", "[Books 1-3]"):
            with self.subTest(tag=tag):
                m = run(f"{AUTHOR} - {KINGDOM}", [product("B0SYNK0009", f"{KINGDOM} {tag}")])
                self.assertHeld(m, "B0SYNK0009")

    def test_marker_in_the_subtitle(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [product("B0SYNK0009", KINGDOM, subtitle="Abridged Edition")])
        self.assertHeld(m, "B0SYNK0009")

    def test_box_set_folder_never_takes_a_single_volume(self):
        m = run(f"{AUTHOR} - {KINGDOM} (Box Set)", [product("B0SYNK0001", KINGDOM, sequence="1")])
        self.assertHeld(m, "B0SYNK0001")

    def test_summary_folder_never_takes_the_plain_book(self):
        m = run(f"{AUTHOR} - {KINGDOM} [Summary]", [product("B0SYNK0001", KINGDOM)])
        self.assertHeld(m, "B0SYNK0001")

    def test_unabridged_is_the_plain_edition(self):
        m = run(f"{AUTHOR} - {KINGDOM} (Unabridged)", [product("B0SYNK0001", KINGDOM)])
        self.assertEqual((m.confidence, m.edition_conflict), ("HIGH", ""))
        m = run(f"{AUTHOR} - {KINGDOM}", [product("B0SYNK0001", f"{KINGDOM} (Unabridged)")])
        self.assertEqual((m.confidence, m.edition_conflict), ("HIGH", ""))

    def test_the_matching_edition_wins_over_a_marked_one(self):
        m = run(f"{AUTHOR} - {KINGDOM}",
                [product("B0SYNK0009", f"{KINGDOM} (Abridged)"), product("B0SYNK0001", KINGDOM)])
        self.assertEqual(m.asin, "B0SYNK0001")
        self.assertEqual(m.edition_conflict, "")

    def test_same_marker_on_both_sides_is_no_conflict(self):
        m = run(f"{AUTHOR} - {KINGDOM} (Box Set)", [product("B0SYNK0BOX", f"{KINGDOM}: Box Set")])
        self.assertEqual((m.confidence, m.edition_conflict), ("HIGH", ""))

    def test_parser(self):
        e = markers.edition_markers
        self.assertEqual(e("Kingdom (Abridged)"), {markers.ABRIDGED})
        self.assertEqual(e("Kingdom (Unabridged)"), set())
        self.assertEqual(e("Kingdom [Synopsis]"), {markers.DERIVATIVE})
        self.assertEqual(e("Kingdom: The Complete Series"), {markers.SCOPE})
        self.assertEqual(e("Kingdom, Books 1 to 3"), {markers.SCOPE})
        self.assertEqual(e("Kingdom Book 1"), set())
        self.assertEqual(e("The Box"), set())

    def test_the_conflict_reaches_the_note_and_nothing_converts(self):
        key = f"{AUTHOR}/{AUTHOR} - {KINGDOM}"
        note, detail, status, converted = pipeline_run(key, [product("B0SYNK0009", f"{KINGDOM} (Abridged)")])
        self.assertIn("abridged", note)
        self.assertTrue(detail["match"]["edition_conflict"])
        self.assertEqual(converted, [])
        self.assertEqual(status, "review")


if __name__ == "__main__":
    unittest.main()

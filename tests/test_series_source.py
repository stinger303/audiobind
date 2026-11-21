"""Q-B12: one series name, everywhere.

Audnex ``seriesPrimary`` (through overrides.apply) is what the tag writer puts in the file. The
detail blob records that value once, and the Match column, the panel and the series shelf all read
it through overrides.series_of - so none of them can name a series the file does not carry.

The fixture is the measured disagreement: the Audible search says ``Discworld #4``, Audnex says
``Discworld - Death #1``.
"""

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from audiobind import catalog, encode, pipeline, series, web
from audiobind import overrides as overrides_mod
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.matching import Match
from audiobind.state import State
from audiobind.units import BookUnit, Part

ASIN = "B0MORT0001"
PATH = "Terry Pratchett/Mort"

#: the Audible search result: series[0] is the main Discworld numbering
PRODUCT = Product(asin=ASIN, title="Mort", authors=["Terry Pratchett"], hours=9.6,
                  series="Discworld", sequence="4")
#: the Audnex record for the same ASIN: its primary series is the Death sub-series
AUDNEX = {
    "asin": ASIN,
    "title": "Mort",
    "authors": [{"name": "Terry Pratchett"}],
    "narrators": [{"name": "Nigel Planer"}],
    "seriesPrimary": {"asin": "B0DEATH", "name": "Discworld - Death", "position": "1"},
    "seriesSecondary": {"asin": "B0DISC", "name": "Discworld", "position": "4"},
    "runtimeLengthMin": 576,
    "releaseDate": "2005-01-01",
}
NO_SERIES = {k: v for k, v in AUDNEX.items() if not k.startswith("series")}


def unit() -> BookUnit:
    book = BookUnit(path=PATH, name="Mort", parent="Terry Pratchett",
                    parts=[Part(name="Mort", files=["/src/mort.mp3"])],
                    author_guess="Terry Pratchett", title_guess="Mort")
    book.rel_path = PATH
    return book


def audit(overrides=None) -> pipeline.Audit:
    match = Match(product=PRODUCT, score=0.95, margin=0.2, confidence="HIGH")
    return pipeline.Audit(unit=unit(), hours=9.6, match=match, confidence="HIGH", action="convert",
                          overrides=dict(overrides or {}))


class SeriesCase(unittest.TestCase):
    def setUp(self):
        catalog.cache_clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name) / "config")
        self.saved_root = CONFIG.output_root
        CONFIG.output_root = pathlib.Path(self.tmp.name) / "output"
        self.saved_dry = CONTROL._dry_run

    def tearDown(self):
        CONTROL._dry_run = self.saved_dry
        CONFIG.output_root = self.saved_root
        self.state.close()
        self.tmp.cleanup()
        catalog.cache_clear()

    def convert_and_record(self, book: dict, overrides=None) -> tuple[dict, dict]:
        """Run convert_unit as far as a dry run goes (it fetches the Audnex record exactly as a
        real conversion does), record the outcome, and return (stored detail, tag map) - the tag
        map being what encode.write_ffmetadata writes from the same inputs a real encode uses."""
        current = audit(overrides)
        if overrides:
            self.state.set_overrides(PATH, overrides)
        CONTROL.set_dry_run(True)
        with patch.object(catalog, "book", return_value=book):
            self.assertIsNone(pipeline.convert_unit(current, log=lambda *_: None))
        pipeline.record(self.state, current, "ready", note="dry run - nothing written")
        detail = json.loads(self.state.get(PATH).detail)
        tagged = overrides_mod.apply(book, current.overrides)
        tags = encode.write_ffmetadata(pathlib.Path(self.tmp.name) / "ffmeta.txt", tagged, current.match, [])
        return detail, tags

    def live_overrides(self) -> dict:
        return self.state.get_overrides(PATH)


class TestPanelEqualsTagMap(SeriesCase):
    def test_the_discworld_case_panel_and_file_say_the_same_series(self):
        detail, tags = self.convert_and_record(AUDNEX)
        self.assertEqual(tags["series"], "Discworld - Death #1")
        self.assertEqual(web.series_label(detail, self.live_overrides()), tags["series"])
        self.assertEqual(detail["match"]["series"], "Discworld - Death")
        self.assertEqual(detail["match"]["sequence"], "1")
        self.assertEqual(detail["match"]["series_source"], "audnex")
        # the Audible search value is not what the file carries, and the panel no longer says it
        self.assertNotIn("Discworld #4", web.series_label(detail, {}))
        self.assertEqual(web.series_source_note(detail, {}), "")

    def test_the_match_column_on_the_page_shows_the_file_s_series(self):
        detail, tags = self.convert_and_record(AUDNEX)
        row = self.state.get(PATH)
        row.status = "converted"
        self.state.save(row)
        page = web.render(self.state, "")
        self.assertIn(f'<span class="ab-match-lead">{tags["series"]}</span>', page)
        self.assertNotIn("Discworld #4", page)
        # the series boxes' placeholders are the value used when nothing is typed
        self.assertIn('placeholder="Discworld - Death"', page)

    def test_a_held_book_reads_audnex_from_the_cache_without_a_request(self):
        current = audit()
        with patch.object(catalog, "_get_json", side_effect=AssertionError("no request allowed")):
            catalog.cache_put(catalog._book_url(ASIN), AUDNEX)
            pipeline.record(self.state, current, "held")
        match = json.loads(self.state.get(PATH).detail)["match"]
        self.assertEqual((match["series"], match["sequence"], match["series_source"]),
                         ("Discworld - Death", "1", "audnex"))

    def test_a_re_check_without_audnex_at_hand_keeps_the_audnex_series(self):
        self.convert_and_record(AUDNEX)
        catalog.cache_clear()
        with patch.object(catalog, "_get_json", side_effect=AssertionError("no request allowed")):
            pipeline.record(self.state, audit(), "review")  # nothing fetched, nothing cached
        match = json.loads(self.state.get(PATH).detail)["match"]
        self.assertEqual((match["series"], match["series_source"]), ("Discworld - Death", "audnex"))


class TestOverrideBeatsBoth(SeriesCase):
    def test_series_and_index_override_beat_audnex_and_audible(self):
        detail, tags = self.convert_and_record(AUDNEX, {"series": "Death", "series_index": "1.5"})
        self.assertEqual(tags["series"], "Death #1.5")
        self.assertEqual(web.series_label(detail, self.live_overrides()), "Death #1.5")
        self.assertEqual(overrides_mod.series_of(detail["match"], self.live_overrides())[2], "override")
        self.assertEqual(series.unit_series(self.state.series_rows()[0]), ("Death", "1.5"))

    def test_a_series_override_alone_keeps_the_audnex_position_as_the_tags_do(self):
        detail, tags = self.convert_and_record(AUDNEX, {"series": "Death"})
        self.assertEqual(tags["series"], "Death #1")
        self.assertEqual(web.series_label(detail, self.live_overrides()), tags["series"])
        self.assertEqual(series.unit_series(self.state.series_rows()[0]), ("Death", "1"))

    def test_an_override_set_after_the_record_still_wins_and_clearing_it_hands_back(self):
        detail, _tags = self.convert_and_record(AUDNEX)
        self.state.set_overrides(PATH, {"series_index": "9"})
        self.assertEqual(web.series_label(detail, self.live_overrides()), "Discworld - Death #9")
        self.state.set_overrides(PATH, {})
        self.assertEqual(web.series_label(detail, self.live_overrides()), "Discworld - Death #1")


class TestAudnexAbsentFallsBack(SeriesCase):
    def test_no_audnex_series_keeps_the_audible_value_and_says_so(self):
        detail, tags = self.convert_and_record(NO_SERIES)
        match = detail["match"]
        self.assertEqual((match["series"], match["sequence"]), ("Discworld", "4"))
        self.assertEqual(match["series_source"], "catalog")
        self.assertTrue(match["audnex_checked"])
        self.assertEqual(web.series_label(detail, {}), "Discworld #4")
        # the file carries no series here, and the panel says so in plain words
        self.assertEqual(tags["series"], "")
        note = web.series_source_note(detail, {})
        self.assertIn("comes from the Audible search", note)
        self.assertIn("Audnex lists no series", note)

    def test_audnex_never_asked_falls_back_and_says_it_was_not_checked(self):
        with patch.object(catalog, "_get_json", side_effect=AssertionError("no request allowed")):
            pipeline.record(self.state, audit(), "review")
        detail = json.loads(self.state.get(PATH).detail)
        self.assertEqual(detail["match"]["series_source"], "catalog")
        self.assertFalse(detail["match"]["audnex_checked"])
        self.assertIn("hasn't been checked", web.series_source_note(detail, {}))

    def test_the_note_is_on_the_panel_only_for_a_fallback(self):
        self.convert_and_record(NO_SERIES)
        self.assertIn("ab-series-note", web.render(self.state, ""))
        self.convert_and_record(AUDNEX)
        self.assertNotIn("ab-series-note", web.render(self.state, ""))

    def test_a_row_recorded_before_q_b12_reads_as_it_always_did(self):
        detail = {"match": {"series": "Discworld", "sequence": "4"}}
        self.assertEqual(web.series_label(detail, {}), "Discworld #4")
        self.assertEqual(web.series_source_note(detail, {}), "")


class TestShelfGroupsOnThePanelValue(SeriesCase):
    def test_shelf_and_panel_name_the_same_series(self):
        detail, tags = self.convert_and_record(AUDNEX)
        row = self.state.series_rows()[0]
        name, sequence = series.unit_series(row)
        self.assertEqual(f"{name} #{sequence}", web.series_label(detail, self.live_overrides()))
        self.assertEqual(f"{name} #{sequence}", tags["series"])
        groups = series.group_rows(self.state.series_rows())
        self.assertEqual([g["name"] for g in groups.values()], ["Discworld - Death"])


if __name__ == "__main__":
    unittest.main()

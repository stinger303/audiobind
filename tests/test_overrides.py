"""Operator metadata overrides: stored, applied to tags and metadata.json, shown as custom."""

import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from audiobind import encode, overrides, web
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.matching import Match
from audiobind.state import State, UnitState

BOOK = {
    "asin": "B005EHN61O",
    "title": "Moneyball",
    "subtitle": "The Art of Winning an Unfair Game",
    "authors": [{"name": "Michael Lewis"}],
    "narrators": [{"name": "Scott Brick"}],
    "seriesPrimary": {"name": "Baseball", "position": "2"},
    "releaseDate": "2011-03-01",
    "publisherName": "Norton",
    "language": "english",
    "description": "<p>Baseball.</p>",
}


class TestNormalise(unittest.TestCase):
    def test_unknown_fields_are_dropped(self):
        self.assertEqual(overrides.normalise({"title": "T", "evil": "x"}), {"title": "T"})

    def test_empty_means_no_override(self):
        self.assertEqual(overrides.normalise({"title": "", "series": "   "}), {})

    def test_whitespace_is_trimmed(self):
        self.assertEqual(overrides.normalise({"title": "  Moneyball  "}), {"title": "Moneyball"})

    def test_authors_split_on_semicolons(self):
        self.assertEqual(overrides.normalise({"authors": "A; B ;C"})["authors"], ["A", "B", "C"])

    def test_authors_accept_a_list(self):
        self.assertEqual(overrides.normalise({"authors": ["A", " B "]})["authors"], ["A", "B"])

    def test_absurd_lengths_are_capped(self):
        value = overrides.normalise({"title": "x" * 5000})["title"]
        self.assertEqual(len(value), overrides.MAX_LENGTH)

    def test_garbage_input_is_not_an_error(self):
        self.assertEqual(overrides.normalise(None), {})
        self.assertEqual(overrides.normalise("nope"), {})

    def test_custom_fields_lists_what_was_set(self):
        self.assertEqual(overrides.custom_fields({"title": "T", "series": "S"}), {"title", "series"})
        self.assertEqual(overrides.custom_fields({}), set())

    def test_describe_round_trips_for_the_form(self):
        described = overrides.describe({"authors": ["A", "B"], "series_index": "3"})
        self.assertEqual(described["authors"], "A;B")
        self.assertEqual(described["series_index"], "3")
        self.assertEqual(described["title"], "")


class TestApply(unittest.TestCase):
    def test_nothing_set_leaves_the_payload_alone(self):
        self.assertEqual(overrides.apply(BOOK, {}), BOOK)

    def test_the_original_payload_is_not_mutated(self):
        overrides.apply(BOOK, {"title": "Other"})
        self.assertEqual(BOOK["title"], "Moneyball")

    def test_title_author_narrator_series_are_replaced(self):
        merged = overrides.apply(
            BOOK,
            {
                "title": "Moneyball: Revised",
                "authors": "M. Lewis; Co Author",
                "narrator": "New Narrator",
                "series": "Sabermetrics",
                "series_index": "7",
            },
        )
        self.assertEqual(merged["title"], "Moneyball: Revised")
        self.assertEqual([a["name"] for a in merged["authors"]], ["M. Lewis", "Co Author"])
        self.assertEqual([n["name"] for n in merged["narrators"]], ["New Narrator"])
        self.assertEqual(merged["seriesPrimary"]["name"], "Sabermetrics")
        self.assertEqual(merged["seriesPrimary"]["position"], "7")

    def test_series_index_alone_updates_the_existing_series(self):
        merged = overrides.apply(BOOK, {"series_index": "9"})
        self.assertEqual(merged["seriesPrimary"], {"name": "Baseball", "position": "9"})

    def test_a_series_index_with_no_series_at_all_is_ignored(self):
        merged = overrides.apply({"title": "T"}, {"series_index": "9"})
        self.assertNotIn("seriesPrimary", merged)

    def test_unset_fields_keep_the_catalog_value(self):
        merged = overrides.apply(BOOK, {"title": "New"})
        self.assertEqual([a["name"] for a in merged["authors"]], ["Michael Lewis"])


class TestOverridesReachTheWrittenFiles(unittest.TestCase):
    """The whole point: a correction must end up in the tags and in metadata.json."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.dir.name)
        self.match = Match(
            product=Product(
                asin="B005EHN61O", title="Moneyball", authors=["Michael Lewis"], narrators=["Scott Brick"]
            )
        )
        self.custom = {
            "title": "Moneyball (Corrected)",
            "authors": "Fixed Author",
            "narrator": "Fixed Narrator",
            "series": "Fixed Series",
            "series_index": "4",
        }

    def tearDown(self):
        self.dir.cleanup()

    def test_ffmetadata_tags_use_the_overrides(self):
        book = overrides.apply(BOOK, self.custom)
        tags = encode.write_ffmetadata(self.tmp / "meta.ff", book, self.match, [])
        self.assertEqual(tags["title"], "Moneyball (Corrected)")
        self.assertEqual(tags["album"], "Moneyball (Corrected)")
        self.assertEqual(tags["artist"], "Fixed Author")
        self.assertEqual(tags["narrator"], "Fixed Narrator")
        self.assertEqual(tags["composer"], "Fixed Narrator")
        self.assertEqual(tags["series"], "Fixed Series #4")
        written = (self.tmp / "meta.ff").read_text(encoding="utf-8")
        self.assertIn("Moneyball (Corrected)", written)
        self.assertIn("Fixed Author", written)

    def test_metadata_json_uses_the_overrides(self):
        book = overrides.apply(BOOK, self.custom)
        encode.write_abs_metadata(self.tmp / "metadata.json", book, self.match, [], 3600.0)
        data = json.loads((self.tmp / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Moneyball (Corrected)")
        self.assertEqual(data["authors"], ["Fixed Author"])
        self.assertEqual(data["narrators"], ["Fixed Narrator"])
        self.assertEqual(data["series"], ["Fixed Series #4"])

    def test_without_overrides_the_catalog_still_wins(self):
        encode.write_abs_metadata(self.tmp / "metadata.json", BOOK, self.match, [], 3600.0)
        data = json.loads((self.tmp / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Moneyball")
        self.assertEqual(data["authors"], ["Michael Lewis"])
        self.assertEqual(data["series"], ["Baseball #2"])


class TestStateStorage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_round_trip(self):
        self.state.set_overrides("A/B", {"title": "T"})
        self.assertEqual(self.state.get_overrides("A/B"), {"title": "T"})

    def test_no_overrides_is_an_empty_dict(self):
        self.assertEqual(self.state.get_overrides("nothing/here"), {})

    def test_overrides_survive_a_rescan_of_the_unit(self):
        """save() must not wipe a correction, exactly like the ASIN override."""
        self.state.set_overrides("A/B", {"title": "T"})
        self.state.save(UnitState(path="A/B", status="converted", title="catalog title"))
        self.assertEqual(self.state.get_overrides("A/B"), {"title": "T"})

    def test_saving_empty_overrides_clears_them(self):
        self.state.set_overrides("A/B", {"title": "T"})
        self.state.set_overrides("A/B", {})
        self.assertEqual(self.state.get_overrides("A/B"), {})

    def test_corrupt_json_is_not_fatal(self):
        self.state.save(UnitState(path="A/B", status="ready"))
        with self.state._lock:
            self.state.connection.execute("UPDATE units SET overrides = '{bad' WHERE path = ?", ("A/B",))
        self.assertEqual(self.state.get_overrides("A/B"), {})


class TestEditorUI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(
            UnitState(
                path="Lewis/Moneyball",
                status="converted",
                title="Moneyball",
                author="Michael Lewis",
                asin="B005EHN61O",
                detail=json.dumps({"match": {"title": "Moneyball", "authors": ["Michael Lewis"]}}),
            )
        )

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_panel_has_an_input_per_editable_field(self):
        page = web.render(self.state)
        for name in overrides.FIELDS:
            self.assertIn(f'data-field="{name}"', page)
        self.assertIn('class="ab-edit"', page)
        self.assertIn("abSaveMeta(this)", page)

    def test_catalog_values_are_offered_as_placeholders_not_values(self):
        page = web.render(self.state)
        self.assertIn('placeholder="Moneyball"', page)
        self.assertIn('data-field="title" value=""', page)

    def test_lock_chips_follow_the_locked_fields(self):
        page = web.render(self.state)
        self.assertIn('class="ab-lock ab-lock-off" data-field="title"', page)
        self.assertIn("\U0001f513 Unlocked", page)
        self.assertNotIn("\U0001f512 Locked by you", page)
        self.assertIn('locked by you" hidden>locked</span>', page)  # row badge, hidden
        self.state.set_overrides("Lewis/Moneyball", {"title": "Corrected"})
        self.state.lock_fields("Lewis/Moneyball", {"title"})
        page = web.render(self.state)
        self.assertIn('class="ab-lock ab-lock-on" data-field="title"', page)
        self.assertIn("\U0001f512 Locked by you", page)
        self.assertIn('class="ab-lock ab-lock-off" data-field="narrator"', page)
        self.assertIn('data-field="title" value="Corrected"', page)
        self.assertIn('locked by you">locked</span>', page)  # row badge, shown

    def test_endpoint_saves_and_clears_overrides(self):
        CONFIG.web_port = 0  # kernel picks a free port: no contention
        web.Handler.state = self.state
        server = web.serve(self.state)
        try:

            def post(body):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{CONFIG.web_port}/api/unit",
                    method="POST",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read())

            result = post(
                {
                    "action": "set_meta",
                    "path": "Lewis/Moneyball",
                    "fields": {"title": "Corrected", "series_index": "3"},
                }
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["custom"], ["series_index", "title"])
            self.assertEqual(
                self.state.get_overrides("Lewis/Moneyball"), {"title": "Corrected", "series_index": "3"}
            )

            result = post(
                {"action": "set_meta", "path": "Lewis/Moneyball", "fields": {"title": "", "series_index": ""}}
            )
            self.assertEqual(result["custom"], [])
            self.assertEqual(self.state.get_overrides("Lewis/Moneyball"), {})

            with self.assertRaises(urllib.error.HTTPError) as caught:
                post({"action": "set_meta", "path": "nope/nope", "fields": {"title": "x"}})
            caught.exception.close()
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

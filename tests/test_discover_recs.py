"""0.17.0-dev1: Discover > For you - suggestions computed from the owned library.

Nothing here touches the network: the catalog is faked at ``catalog.query``, and the
background refresher is stubbed or joined wherever it would run.
"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import catalog, discover, series, web
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def prod(asin, title, authors=(), narrators=(), hours=10.0, language="english", subtitle=""):
    return Product(asin=asin, title=title, subtitle=subtitle, authors=list(authors), narrators=list(narrators),
                   hours=hours, language=language, image=f"https://m.media-amazon.com/images/I/{asin}.jpg")


class Catalog:
    """A fake catalog.query: answers per (kind, name) and records every call."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, title="", author="", narrator="", keywords="", limit=20):
        key = ("author", author) if author else ("narrator", narrator) if narrator else ("genre", keywords)
        self.calls.append(key)
        answer = self.answers.get(key, [])
        return None if answer is None else list(answer)


class RecsCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        self.state = State(self.root / "state")
        self.out = self.root / "output"
        self.out.mkdir()
        self.saved_out = CONFIG.output_root
        CONFIG.output_root = self.out
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()
        self.n = 0

    def tearDown(self):
        self.schedule.stop()
        discover.RECS_REFRESHER.join(5)
        CONFIG.output_root = self.saved_out
        self.state.close()
        self.dir.cleanup()

    def own(self, title, author, narrators=(), genres=(), asin="", language="english", overrides=None):
        """One book in the library; with genres it is published, metadata.json and all."""
        self.n += 1
        output = ""
        if genres:
            folder = self.out / author / title
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "metadata.json").write_text(json.dumps({"title": title, "genres": list(genres)}))
            output = str(folder / f"{title}.m4b")
        detail = {"match": {"asin": asin, "title": title, "authors": [author], "narrators": list(narrators),
                            "language": language}}
        path = f"{author}/{title}"
        self.state.save(UnitState(path=path, status="converted", asin=asin, title=title, author=author,
                                  detail=json.dumps(detail), output=output))
        if overrides:
            self.state.set_overrides(path, overrides)

    def refresh(self, fake):
        with mock.patch.object(catalog, "query", fake):
            return discover.refresh_recommendations(self.state)


class TestOwnedSignals(RecsCase):
    def test_top_authors_narrators_and_genres_by_owned_count(self):
        for i in range(3):
            self.own(f"Reacher {i}", "Lee Child", ["Dick Hill"], ["Thriller", "Suspense"])
        for i in range(2):
            self.own(f"Bosch {i}", "Michael Connelly", ["Titus Welliver"], ["Thriller"])
        self.own("Hail Mary", "Andy Weir", ["Ray Porter"], ["Science Fiction"])
        signals = discover.owned_signals(self.state)
        self.assertEqual(signals["authors"][:2], [("Lee Child", 3), ("Michael Connelly", 2)])
        self.assertEqual(signals["narrators"][0], ("Dick Hill", 3))
        self.assertEqual(signals["genres"], [("Thriller", 5), ("Suspense", 3)])
        self.assertEqual(signals["languages"], ["english"])

    def test_a_narrator_override_beats_the_catalog_match(self):
        self.own("Book", "Author", ["Catalog Reader"], overrides={"narrator": "Real Reader; Second Voice"})
        names = [name for name, _ in discover.owned_signals(self.state)["narrators"]]
        self.assertEqual(sorted(names), ["Real Reader", "Second Voice"])

    def test_metadata_outside_the_output_root_is_never_read(self):
        stray = self.root / "elsewhere" / "Book"
        stray.mkdir(parents=True)
        (stray / "metadata.json").write_text(json.dumps({"genres": ["Smuggled"]}))
        self.state.save(UnitState(path="x/Book", status="converted", title="Book", author="A",
                                  output=str(stray / "Book.m4b")))
        self.assertEqual(discover.owned_signals(self.state)["genres"], [])

    def test_caps(self):
        for i in range(8):
            self.own(f"Book {i}", f"Author {i}", [f"Narrator {i}"], [f"Genre {i}"])
        signals = discover.owned_signals(self.state)
        self.assertEqual(len(signals["authors"]), discover.TOP_AUTHORS)
        self.assertEqual(len(signals["narrators"]), discover.TOP_NARRATORS)
        self.assertEqual(len(signals["genres"]), discover.TOP_GENRES)


class TestCompute(RecsCase):
    def test_a_refresh_never_exceeds_the_call_cap(self):
        for i in range(8):
            self.own(f"Book {i}", f"Author {i}", [f"Narrator {i}"], [f"Genre {i}"])
        fake = Catalog()
        result = self.refresh(fake)
        self.assertEqual(len(fake.calls), discover.MAX_CALLS)
        self.assertEqual(result["calls"], discover.MAX_CALLS)
        kinds = [k for k, _ in fake.calls]
        self.assertEqual(kinds.count("author"), 5)
        self.assertEqual(kinds.count("narrator"), 3)
        self.assertEqual(kinds.count("genre"), 2)

    def test_suggestions_come_from_owned_metadata_and_skip_what_you_have_or_want(self):
        self.own("Killing Floor", "Lee Child", ["Dick Hill"], ["Thriller"], asin="B0OWNED001")
        self.own("Die Trying", "Lee Child", ["Dick Hill"], ["Thriller"])
        self.state.wishlist_add({"asin": "B0WANTED01", "title": "Tripwire"})
        fake = Catalog({
            ("author", "Lee Child"): [
                prod("B0OWNED001", "Killing Floor", ["Lee Child"]),                 # owned by ASIN
                prod("B0OTHERED1", "Die Trying", ["Lee Child"]),                    # owned by title + author
                prod("B0WANTED01", "Tripwire", ["Lee Child"]),                      # already wanted
                prod("B0SUMMARY1", "Summary of Killing Floor", ["Lee Child"]),      # knockoff
                prod("B0GERMAN01", "Größenwahn", ["Lee Child"], language="german"),  # not your language
                prod("B0IMPOSTR1", "Copycat", ["Lee Childs Fan Club"]),             # not by Lee Child
                prod("B0RUNNING1", "Running Blind", ["Lee Child"]),
                prod("B0RUNNING2", "Running Blind", ["Lee Child"]),                 # second edition
            ],
            ("narrator", "Dick Hill"): [
                prod("B0RUNNING1", "Running Blind", ["Lee Child"], ["Dick Hill"]),  # already suggested
                prod("B0NARRATE1", "Bourne", ["Robert Ludlum"], ["Dick Hill"]),
                prod("B0NOTHIM01", "Other", ["Someone"], ["Scott Brick"]),          # not read by him
            ],
            ("genre", "Thriller"): [prod("B0GENRE001", "Gone Girl", ["Gillian Flynn"])],
        })
        before = self.state.wishlist_count()
        result = self.refresh(fake)
        sections = {s["kind"]: s for s in result["sections"]}
        author_items = [i["asin"] for i in sections["author"]["groups"][0]["items"]]
        self.assertEqual(author_items, ["B0RUNNING1"])
        self.assertEqual(sections["author"]["groups"][0]["seed"], "Lee Child")
        self.assertEqual(sections["author"]["groups"][0]["owned"], 2)
        self.assertEqual([i["asin"] for i in sections["narrator"]["groups"][0]["items"]], ["B0NARRATE1"])
        self.assertEqual([i["asin"] for i in sections["genre"]["groups"][0]["items"]], ["B0GENRE001"])
        # nothing is ever added on its own
        self.assertEqual(self.state.wishlist_count(), before)
        # suggestions are remembered for covers and Get this
        self.assertIsNotNone(self.state.catalog_seen_image("B0NARRATE1"))

    def test_per_seed_cap(self):
        self.own("Owned", "Prolific")
        fake = Catalog({("author", "Prolific"): [prod(f"B0PROLIF{i:02d}", f"Book {i}", ["Prolific"])
                                                 for i in range(20)]})
        result = self.refresh(fake)
        self.assertEqual(len(result["sections"][0]["groups"][0]["items"]), discover.PER_SEED)

    def test_a_failed_query_is_recorded_not_hidden(self):
        self.own("Owned", "Author A", ["Reader R"])
        fake = Catalog({("author", "Author A"): None,
                        ("narrator", "Reader R"): [prod("B0NARRATE1", "X", ["Z"], ["Reader R"])]})
        result = self.refresh(fake)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["sections"][0]["groups"][0]["ok"])
        self.assertTrue(result["sections"][1]["groups"][0]["ok"])

    def test_an_all_failed_refresh_keeps_the_last_good_result(self):
        self.own("Owned", "Author A")
        self.refresh(Catalog({("author", "Author A"): [prod("B0GOOD0001", "Good", ["Author A"])]}))
        kept = self.refresh(Catalog({("author", "Author A"): None}))
        self.assertEqual(kept["last_error"], "unreachable")
        stored = discover.recommendations(self.state)
        self.assertEqual(stored["sections"][0]["groups"][0]["items"][0]["asin"], "B0GOOD0001")
        self.assertEqual(stored["last_error"], "unreachable")


class TestCaching(RecsCase):
    def age(self, seconds):
        self.state.connection.execute("UPDATE discover_cache SET fetched_at = fetched_at - ?", (seconds,))

    def test_no_catalog_call_while_the_cache_is_fresh(self):
        self.own("Owned", "Author A")
        self.refresh(Catalog())
        fake = Catalog()
        with mock.patch.object(catalog, "query", fake), \
                mock.patch.object(discover.RECS_REFRESHER, "schedule", return_value=True) as schedule:
            self.assertFalse(discover.request_refresh(self.state))
            schedule.assert_not_called()
            discover.recommendations(self.state)
            web.render_discover(self.state, "for_you")
        self.assertEqual(fake.calls, [])

    def test_a_week_old_cache_or_none_at_all_is_refreshed(self):
        with mock.patch.object(discover.RECS_REFRESHER, "schedule", return_value=True) as schedule:
            self.assertTrue(discover.request_refresh(self.state))  # nothing stored yet
            self.own("Owned", "Author A")
            self.refresh(Catalog())
            self.age(discover.RECS_TTL_SECONDS - 60)
            self.assertFalse(discover.request_refresh(self.state))
            self.age(120)
            self.assertTrue(discover.request_refresh(self.state))
        self.assertEqual(schedule.call_count, 2)

    def test_refresh_button_forces_even_when_fresh(self):
        self.own("Owned", "Author A")
        self.refresh(Catalog())
        with mock.patch.object(discover.RECS_REFRESHER, "schedule", return_value=True) as schedule:
            self.assertTrue(discover.request_refresh(self.state, force=True))
        schedule.assert_called_once()

    def test_background_refresh_stores_the_result(self):
        self.own("Owned", "Author A")
        fake = Catalog({("author", "Author A"): [prod("B0BGROUND1", "Later", ["Author A"])]})
        with mock.patch.object(catalog, "query", fake):
            self.assertTrue(discover.request_refresh(self.state))
            discover.RECS_REFRESHER.join(5)
        self.assertFalse(discover.RECS_REFRESHER.busy)
        self.assertEqual(discover.recommendations(self.state)["sections"][0]["groups"][0]["items"][0]["asin"],
                         "B0BGROUND1")

    def test_stored_suggestions_are_checked_again_on_every_read(self):
        self.own("Owned", "Author A")
        self.refresh(Catalog({("author", "Author A"): [prod("B0FIRST001", "First", ["Author A"]),
                                                       prod("B0SECOND01", "Second", ["Author A"]),
                                                       prod("B0THIRD001", "Third", ["Author A"])]}))
        self.state.wishlist_add({"asin": "B0FIRST001", "title": "First"})
        self.own("Second", "Author A")  # got it since the refresh
        items = discover.recommendations(self.state)["sections"][0]["groups"][0]["items"]
        self.assertEqual([i["asin"] for i in items], ["B0THIRD001"])


class TestForYouPage(RecsCase):
    def render(self):
        with mock.patch.object(catalog, "query", side_effect=AssertionError("render must not query")):
            return web.render_discover(self.state, "for_you")

    def test_sections_and_get_this(self):
        self.own("Killing Floor", "Lee Child", ["Dick Hill"])
        self.own("Die Trying", "Lee Child", ["Dick Hill"])
        self.refresh(Catalog({("author", "Lee Child"): [prod("B0RUNNING1", "Running Blind", ["Lee Child"])],
                              ("narrator", "Dick Hill"): []}))
        html = self.render()
        self.assertIn('class="chip on" href="/discover/for-you" aria-current="page">For you</a>', html)
        self.assertIn("<h3>More by authors you own</h3>", html)
        self.assertIn("<h4>Lee Child<span>you own 2</span></h4>", html)
        self.assertIn("abWant(this,'for_you')\">Get this</button>", html)
        self.assertIn("<h4>Dick Hill<span>reads 2 of yours</span></h4>", html)
        self.assertIn("Nothing new &#8212;", html)
        self.assertIn("No genre data yet.", html)
        self.assertIn("Updated <b>just now</b> &#183; 2 catalog requests (at most 10 per refresh)", html)
        self.assertIn('id="ab-recs" data-fetched="', html)
        self.assertIn('onclick="abRecsRefresh(true)">Refresh</button>', html)
        self.assertNotIn("https://", html)

    def test_before_the_first_refresh(self):
        html = self.render()
        self.assertIn("Press <b>Refresh</b> to build suggestions", html)
        with mock.patch.object(type(discover.RECS_REFRESHER), "busy", new_callable=mock.PropertyMock,
                               return_value=True):
            self.assertIn("Building your suggestions", self.render())

    def test_an_empty_library_says_so(self):
        self.refresh(Catalog())
        self.assertIn("Your library has nothing to go on yet.", self.render())

    def test_failures_are_shown(self):
        self.own("Owned", "Author A")
        self.refresh(Catalog({("author", "Author A"): None}))
        html = self.render()
        self.assertIn("could not be reached for this one", html)
        self.assertIn("the last refresh could not reach the catalog", html)


class TestRecsEndpoints(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()

    def tearDown(self):
        discover.RECS_REFRESHER.join(5)
        self.schedule.stop()
        super().tearDown()

    def test_get_reads_the_cache_only(self):
        with mock.patch.object(catalog, "query", side_effect=AssertionError("GET must not query")):
            status, body = self.request("/api/discover/recommendations", headers={})
            self.assertEqual(status, 200)
            self.assertIsNone(body["fetched_at"])
            self.assertTrue(body["stale"])
            status, html = self.request("/discover/for-you", raw=True, headers={})
            self.assertEqual(status, 200)
            self.assertIn(b'id="ab-recs"', html)

    def test_post_refresh_runs_in_the_background(self):
        self.state.save(UnitState(path="A/Owned", status="converted", title="Owned", author="Author A"))
        fake = Catalog({("author", "Author A"): [prod("B0POSTED01", "Posted", ["Author A"])]})
        with mock.patch.object(catalog, "query", fake):
            status, body = self.request("/api/discover/recommendations", {"action": "refresh"})
            self.assertEqual(status, 200)
            self.assertTrue(body["queued"])
            self.assertTrue(body["refreshing"])
            discover.RECS_REFRESHER.join(5)
            # fresh now: a second page-load refresh is a no-op
            status, body = self.request("/api/discover/recommendations", {"action": "refresh"})
            self.assertFalse(body["queued"])
        self.assertEqual(fake.calls, [("author", "Author A")])
        status, body = self.request("/api/discover/recommendations", headers={})
        self.assertEqual(body["sections"][0]["groups"][0]["items"][0]["asin"], "B0POSTED01")
        self.assertEqual(self.state.wishlist_count(), 0)

    def test_post_refusals(self):
        status, _ = self.request("/api/discover/recommendations", {"action": "nope"})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/discover/recommendations", b'{"action":"refresh"}',
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


if __name__ == "__main__":
    unittest.main()

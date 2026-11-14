"""0.17.0-dev1: Discover > Search - catalog search with the already-in-library marker.

Nothing here touches the network: the catalog is faked at ``catalog._get_json`` or
``catalog.query``, and the series refresher is stubbed wherever a page is rendered.
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

from audiobind import catalog, covers, discover, series, web
from audiobind.catalog import Product
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def product(asin, title, authors=("Andy Weir",), narrators=("Ray Porter",), hours=16.2, series_name="",
            sequence="", year="2021", subtitle=""):
    return Product(asin=asin, title=title, subtitle=subtitle, authors=list(authors), narrators=list(narrators),
                   series=series_name, sequence=sequence, hours=hours, language="english", year=year,
                   image=f"https://m.media-amazon.com/images/I/{asin}.jpg")


RESULTS = [
    product("B08G9PRS1K", "Project Hail Mary"),
    product("B00B5HZGUG", "The Martian", hours=10.9, year="2013"),
    product("B0751LWHQC", "Artemis", narrators=("Rosario Dawson",), hours=8.9, year="2017"),
    product("B0SUMMARY1", "Summary of Project Hail Mary", authors=("Quick Reads",), hours=0.5),
    product("B0BRACE001", 'Dune {Saga} "Quoted" <b>', authors=("Frank Herbert",), series_name="Dune",
            sequence="1", year="1965"),
]


def row_html(html, asin):
    """One result row's markup, up to the next row or the end of the panel."""
    match = re.search(r'<div class="ab-disc-row" data-asin="%s">.*?(?=<div class="ab-disc-row"|</section>)' % asin,
                      html, re.S)
    return match.group(0) if match else ""


def owned(state, path, title, author, asin=""):
    detail = {"match": {"asin": asin, "title": title, "authors": [author]}}
    state.save(UnitState(path=path, status="converted", asin=asin, title=title, author=author,
                         detail=json.dumps(detail), output=""))


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()

    def tearDown(self):
        self.schedule.stop()
        self.state.close()
        self.dir.cleanup()


class TestCatalogQuery(unittest.TestCase):
    def test_query_goes_through_the_throttled_memoised_helper(self):
        payload = {"products": [{"asin": "B08G9PRS1K", "title": "Project Hail Mary",
                                 "authors": [{"name": "Andy Weir"}], "runtime_length_min": 970}]}
        with mock.patch.object(catalog, "_get_json", return_value=payload) as get:
            found = catalog.query(title="hail mary", author="weir", limit=20)
        url = get.call_args[0][0]
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith(catalog.AUDIBLE))
        self.assertEqual(params["title"], ["hail mary"])
        self.assertEqual(params["author"], ["weir"])
        self.assertEqual(params["num_results"], ["20"])
        self.assertEqual([p.asin for p in found], ["B08G9PRS1K"])
        self.assertEqual(found[0].hours, 16.17)

    def test_failure_is_none_and_an_empty_query_asks_nothing(self):
        with mock.patch.object(catalog, "_get_json", return_value=None) as get:
            self.assertIsNone(catalog.query(narrator="Ray Porter"))
            self.assertEqual(get.call_count, 1)
            self.assertEqual(catalog.query(), [])
            self.assertEqual(get.call_count, 1)

    def test_limit_is_capped(self):
        with mock.patch.object(catalog, "_get_json", return_value={"products": []}) as get:
            catalog.query(keywords="space", limit=500)
        self.assertIn("num_results=50", get.call_args[0][0])


class TestSearchMarks(StateCase):
    def search(self, **kw):
        with mock.patch.object(catalog, "query", return_value=list(RESULTS)) as query:
            result = discover.search(self.state, **kw)
        return result, query

    def test_marks_books_already_in_the_library(self):
        owned(self.state, "Weir/Project Hail Mary", "Project Hail Mary", "Andy Weir", asin="B08G9PRS1K")
        # another edition of The Martian: no ASIN in common, same title and author
        owned(self.state, "Weir/The Martian", "The Martian", "Andy Weir", asin="B0OTHEREDN")
        # "I have this" on the Series page counts too
        self.state.set_owned_mark("B0751LWHQC")
        result, query = self.search(title="weir")
        query.assert_called_once_with(title="weir", author="", limit=discover.SEARCH_LIMIT)
        flags = {r["asin"]: r["in_library"] for r in result["results"]}
        self.assertEqual(flags, {"B08G9PRS1K": True, "B00B5HZGUG": True, "B0751LWHQC": True,
                                 "B0SUMMARY1": False, "B0BRACE001": False})

    def test_wishlisted_and_knockoff_flags(self):
        self.state.wishlist_add({"asin": "B00B5HZGUG", "title": "The Martian"})
        result, _ = self.search(title="weir")
        rows = {r["asin"]: r for r in result["results"]}
        self.assertTrue(rows["B00B5HZGUG"]["wishlisted"])
        self.assertFalse(rows["B08G9PRS1K"]["wishlisted"])
        self.assertTrue(rows["B0SUMMARY1"]["knockoff"])
        self.assertFalse(rows["B08G9PRS1K"]["knockoff"])

    def test_results_are_remembered_for_covers_and_get_this(self):
        self.search(title="weir")
        self.assertEqual(self.state.catalog_seen_image("B08G9PRS1K"), "https://m.media-amazon.com/images/I/B08G9PRS1K.jpg")
        self.assertIsNone(self.state.catalog_seen_image("B0NEVERSEEN"))
        # a "Get this" click takes the catalog's metadata, not what the browser sends
        row, error = discover.wishlist_item(self.state, {"asin": "B0751LWHQC", "title": "Forged", "origin": "search"})
        self.assertEqual(error, "")
        self.assertEqual((row["title"], row["authors"], row["hours"]), ("Artemis", ["Andy Weir"], 8.9))

    def test_empty_query_never_calls_the_catalog(self):
        result, query = self.search()
        query.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])

    def test_unreachable_catalog_is_said_plainly(self):
        with mock.patch.object(catalog, "query", return_value=None):
            result = discover.search(self.state, title="weir")
        self.assertFalse(result["ok"])
        self.assertIn("could not be reached", result["error"])


class TestSearchPage(StateCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(CONFIG, "mam_id", "x")  # these tests are about the MAM surfaces
        p.start()
        self.addCleanup(p.stop)

    def render(self, **kw):
        with mock.patch.object(catalog, "query", return_value=list(RESULTS)) as query:
            html = web.render_discover(self.state, "search", **kw)
        return html, query

    def test_page_renders_results_with_the_owned_marker(self):
        owned(self.state, "Weir/Project Hail Mary", "Project Hail Mary", "Andy Weir", asin="B08G9PRS1K")
        self.state.wishlist_add({"asin": "B0751LWHQC", "title": "Artemis"})
        html, _ = self.render(title="weir")
        self.assertIn("5 results for &#8220;weir&#8221;", html)
        self.assertIn("1 already in your library", html)
        row = row_html(html, "B08G9PRS1K")
        self.assertIn("In your library", row)
        self.assertNotIn("Get this", row)
        martian = row_html(html, "B00B5HZGUG")
        self.assertIn("Get this", martian)
        self.assertIn("abWant(this,'search')", martian)
        artemis = row_html(html, "B0751LWHQC")
        self.assertIn("On your wishlist", artemis)
        # cover, authors, narrators, runtime, series, year
        self.assertIn('/cover?asin=B00B5HZGUG', html)
        self.assertIn("By Andy Weir &#183; Narrated by Ray Porter", html)
        self.assertIn("10.9 hrs &#183; 2013 &#183; ASIN B00B5HZGUG", html)
        self.assertIn("Dune #1 &#183; 1965", html)
        self.assertIn('class="ab-ko"', row_html(html, "B0SUMMARY1"))

    def test_titles_are_escaped_and_braces_survive(self):
        html, _ = self.render(title='dune "x"')
        self.assertIn("Dune {Saga} &quot;Quoted&quot; &lt;b&gt;", html)
        self.assertNotIn("<b>Dune", html)
        self.assertIn('value="dune &quot;x&quot;"', html)

    def test_no_query_no_catalog_call(self):
        html, query = self.render()
        query.assert_not_called()
        self.assertIn('name="title"', html)
        self.assertIn('name="author"', html)
        self.assertNotIn('class="ab-disc-row"', html)

    def test_failure_and_empty_states(self):
        with mock.patch.object(catalog, "query", return_value=None):
            html = web.render_discover(self.state, "search", title="x")
        self.assertIn("could not be reached", html)
        with mock.patch.object(catalog, "query", return_value=[]):
            html = web.render_discover(self.state, "search", title="zzz")
        self.assertIn("Nothing in the Audible catalog for &#8220;zzz&#8221;", html)

    def test_no_external_urls_and_discover_tab_active(self):
        html, _ = self.render(title="weir")
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn('class="ab-rail-item ab-active" href="/discover" aria-current="page"', html)

    def test_rail_has_discover_under_series_on_every_page(self):
        for page in (web.render(self.state), web.render_series(self.state), web.render_settings(self.state),
                     self.render()[0]):
            lib, ser = page.index("</span> Library"), page.index("</span> Series")
            disc, sett = page.index("</span> Discover"), page.index("</span> Settings")
            self.assertLess(lib, ser)
            self.assertLess(ser, disc)
            self.assertLess(disc, sett)

    def test_rail_count_is_the_wishlist_size(self):
        self.assertIn('id="ab-rail-wanted" title="books on your wishlist" hidden>0</span>', web.render(self.state))
        self.state.wishlist_add({"asin": "B000000001", "title": "x"})
        self.assertIn('id="ab-rail-wanted" title="books on your wishlist">1</span>', web.render(self.state))


class TestWishlistTab(StateCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(CONFIG, "mam_id", "x")  # these tests are about the MAM surfaces
        p.start()
        self.addCleanup(p.stop)

    def test_lists_rows_with_remove(self):
        self.state.wishlist_add({"asin": "B000000001", "title": "Wanted <One>", "authors": ["A. Writer"],
                                 "origin": "search", "hours": 7.5, "series": "Saga", "sequence": "2"})
        html = web.render_discover(self.state, "wishlist")
        self.assertIn("1 book wanted", html)
        self.assertIn("Wanted &lt;One&gt;", html)
        self.assertIn("By A. Writer", html)
        self.assertIn("7.5 hrs &#183; Saga #2 &#183; ASIN B000000001 &#183; added just now from Search", html)
        self.assertIn("WANTED", html)
        self.assertIn('onclick="abUnwant(this)">Remove</button>', html)

    def test_empty(self):
        self.assertIn("Your wishlist is empty.", web.render_discover(self.state, "wishlist"))


class TestAssets(unittest.TestCase):
    def test_discover_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            js = pathlib.Path(tmp) / "discover.js"
            js.write_text(web.DISCOVER_SCRIPT, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_external_assets_in_the_discover_templates(self):
        for marker in ("http://", "https://", "//cdn"):
            self.assertNotIn(marker, web.DISCOVER_PAGE + web.DISCOVER_SCRIPT)


class TestSearchEndpoints(LiveServerCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(CONFIG, "mam_id", "x")  # these tests are about the MAM surfaces
        p.start()
        self.addCleanup(p.stop)
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()
        self.query = mock.patch.object(catalog, "query", return_value=list(RESULTS))
        self.fake = self.query.start()
        saved_state_dir = CONFIG.state_dir
        CONFIG.state_dir = self.root / "state"
        self.addCleanup(setattr, CONFIG, "state_dir", saved_state_dir)
        owned(self.state, "Weir/Project Hail Mary", "Project Hail Mary", "Andy Weir", asin="B08G9PRS1K")

    def tearDown(self):
        self.query.stop()
        self.schedule.stop()
        super().tearDown()

    def test_search_page_is_served(self):
        status, html = self.request("/discover?title=hail+mary&author=weir", raw=True, headers={})
        self.assertEqual(status, 200)
        self.assertIn(b"In your library", html)
        self.fake.assert_called_once_with(title="hail mary", author="weir", limit=discover.SEARCH_LIMIT)

    def test_search_api(self):
        status, body = self.request("/api/discover/search?title=weir", headers={})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 5)
        first = body["results"][0]
        self.assertEqual(first["asin"], "B08G9PRS1K")
        self.assertTrue(first["in_library"])
        for key in ("title", "authors", "narrators", "hours", "series", "sequence", "year", "wishlisted"):
            self.assertIn(key, first)

    def test_search_api_reports_an_unreachable_catalog(self):
        self.fake.return_value = None
        status, body = self.request("/api/discover/search?title=weir", headers={})
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])

    def test_get_this_from_a_search_result(self):
        self.request("/api/discover/search?title=weir", headers={})
        status, body = self.request("/api/wishlist", {"asin": "B00B5HZGUG", "origin": "search"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["item"]["title"], "The Martian")
        self.assertEqual(body["item"]["origin"], "search")

    def test_wishlist_tab_is_served(self):
        self.state.wishlist_add({"asin": "B000000001", "title": "Wanted One"})
        status, html = self.request("/discover/wishlist", raw=True, headers={})
        self.assertEqual(status, 200)
        self.assertIn(b"Wanted One", html)

    def test_cover_proxy_allows_asins_a_search_showed_only(self):
        self.request("/api/discover/search?title=weir", headers={})
        with mock.patch.object(catalog, "download", return_value=False) as download:
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0751LWHQC"))
            self.assertEqual(download.call_args[0][0], "https://m.media-amazon.com/images/I/B0751LWHQC.jpg")
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0NEVERSEEN"))
            self.assertEqual(download.call_count, 1)


if __name__ == "__main__":
    unittest.main()

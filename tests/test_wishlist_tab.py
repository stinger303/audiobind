"""0.18.0-dev10: Wishlist is its own rail tab, Discover books is one screen, and a book that
reaches the library leaves the wishlist. No network."""

import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import series, watcher, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def want(state, asin="B000000001", title="Project Hail Mary", authors=("Andy Weir",)):
    state.wishlist_add({"asin": asin, "title": title, "authors": list(authors), "origin": "search"})


def convert(state, title="Project Hail Mary", author="Andy Weir", asin="B000000001"):
    state.save(UnitState(path=f"{author}/{title}", status="converted", title=title, author=author,
                         asin=asin, output="/output/x.m4b"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(pathlib.Path(self.tmp.name))
        self.addCleanup(self.state.close)
        p = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        watcher.WATCHER._paused = None

    def mam(self, on=True):
        p = mock.patch.object(CONFIG, "mam_id", "x" if on else "")
        p.start()
        self.addCleanup(p.stop)


class TestRailAndDiscover(Base):
    def test_rail_has_wishlist_below_discover_with_mam(self):
        self.mam()
        for page in (web.render(self.state), web.render_series(self.state), web.render_settings(self.state),
                     web.render_discover(self.state, "search"), web.render_discover(self.state, "wishlist")):
            disc, wish = page.index("</span> Discover books"), page.index("</span> Wishlist")
            self.assertLess(disc, wish)
            self.assertLess(wish, page.index("</span> Settings"))
            self.assertIn('href="/wishlist"', page)

    def test_no_wishlist_rail_item_without_mam(self):
        self.mam(False)
        for page in (web.render(self.state), web.render_discover(self.state, "search"),
                     web.render_discover(self.state, "wishlist")):
            self.assertNotIn('href="/wishlist"', page)
            self.assertNotIn("</span> Wishlist", page)
            self.assertNotIn('id="ab-rail-wanted"', page)
            self.assertIn("</span> Discover books", page)

    def test_discover_has_no_chips(self):
        self.mam()
        html = web.render_discover(self.state, "search")
        self.assertNotIn("For you</a>", html)
        self.assertNotIn('class="chip', html)
        self.assertNotIn("/discover/for-you", html)
        self.assertNotIn("/discover/wishlist", html)
        self.assertIn("<title>Audiobind &#183; Discover books</title>", html)
        self.assertIn("<h1>Discover books</h1>", html)

    def test_search_form_and_recommendations_on_one_render(self):
        self.mam()
        html = web.render_discover(self.state, "search")
        self.assertIn('role="search"', html)
        self.assertIn('aria-label="Title"', html)
        self.assertIn('id="ab-recs"', html)
        self.assertLess(html.index('role="search"'), html.index('id="ab-recs"'))
        self.assertIn("abRecsRefresh", html)

    def test_a_search_replaces_the_recommendations(self):
        self.mam()
        result = {"ok": True, "title": "dune", "author": "", "results": []}
        html = web.render_discover(self.state, "search", "dune", "", result=result)
        self.assertIn('role="search"', html)
        self.assertIn("Nothing in the Audible catalog", html)
        self.assertNotIn('id="ab-recs"', html)

    def test_wishlist_page_renders_the_wishlist_body(self):
        self.mam()
        want(self.state)
        html = web.render_discover(self.state, "wishlist")
        self.assertIn("<h1>Wishlist</h1>", html)
        self.assertIn("1 book wanted", html)
        self.assertIn("abUnwant(this)", html)
        self.assertNotIn('role="search"', html)

    def test_rail_badge_is_on_wishlist_and_drops_after_a_removal(self):
        self.mam()
        want(self.state)
        self.assertIn('id="ab-rail-wanted" title="books on your wishlist">1</span>', web.render(self.state))
        convert(self.state)
        watcher.resolve_library(self.state)
        self.assertIn('id="ab-rail-wanted" title="books on your wishlist" hidden>0</span>',
                      web.render(self.state))


class TestRoutes(LiveServerCase):
    def test_wishlist_and_discover_render(self):
        with mock.patch.object(CONFIG, "mam_id", "x"):
            want(self.state)
            status, html = self.request("/wishlist", raw=True, headers={})
            self.assertEqual(status, 200)
            self.assertIn(b"<h1>Wishlist</h1>", html)
            self.assertIn(b"Project Hail Mary", html)
            status, html = self.request("/discover", raw=True, headers={})
            self.assertEqual(status, 200)
            self.assertIn(b"<h1>Discover books</h1>", html)

    def test_wishlist_falls_back_to_the_library_page_without_mam(self):
        with mock.patch.object(CONFIG, "mam_id", ""):
            status, html = self.request("/wishlist", raw=True, headers={})
            self.assertEqual(status, 200)
            self.assertNotIn(b"<h1>Wishlist</h1>", html)
            self.assertNotIn(b'href="/wishlist"', html)


class TestAcquiredBooksLeave(Base):
    def test_a_resolved_row_is_gone_and_the_removal_is_idempotent(self):
        want(self.state)
        convert(self.state)
        with self.assertLogs("audiobind", "INFO") as logs:
            self.assertEqual(watcher.resolve_library(self.state), ["B000000001"])
        self.assertIn("wishlist: Project Hail Mary is in the library - removed from the wishlist",
                      "\n".join(logs.output))
        self.assertEqual(self.state.wishlist_all(), [])
        self.assertEqual(self.state.wishlist_count(), 0)
        self.assertEqual(watcher.resolve_library(self.state), [], "a second resolve does nothing")

    def test_a_downloading_row_leaves_too(self):
        want(self.state)
        self.state.wishlist_set_status("B000000001", "downloading", note="sent")
        convert(self.state)
        watcher.resolve_library(self.state)
        self.assertEqual(self.state.wishlist_all(), [])

    def test_rows_that_do_not_match_are_untouched(self):
        want(self.state, "B000000001")
        want(self.state, "B000000002", "The Martian")
        want(self.state, "B000000003", "Artemis")
        self.state.wishlist_set_status("B000000002", "not_found", note="x")
        self.state.wishlist_set_status("B000000003", "error", note="x")
        convert(self.state, "Something Else", "Nobody", "B0ELSE0000")
        self.assertEqual(watcher.resolve_library(self.state), [])
        self.assertEqual({r["asin"]: r["status"] for r in self.state.wishlist_all()},
                         {"B000000001": "wanted", "B000000002": "not_found", "B000000003": "error"})

    def test_only_the_matching_row_is_removed(self):
        want(self.state, "B000000001")
        want(self.state, "B000000002", "The Martian")
        convert(self.state)
        watcher.resolve_library(self.state)
        self.assertEqual([r["asin"] for r in self.state.wishlist_all()], ["B000000002"])
        self.assertEqual(self.state.wishlist_all()[0]["status"], "wanted")

    def test_a_leftover_in_library_row_is_cleaned_up(self):
        want(self.state)
        self.state.wishlist_set_status("B000000001", "in_library", note="old")
        watcher.resolve_library(self.state)
        self.assertEqual(self.state.wishlist_all(), [])


if __name__ == "__main__":
    unittest.main()

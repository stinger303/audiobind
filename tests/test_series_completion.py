"""0.17.0-dev1: series completion - owned X of Y, the missing list, Want and Want all missing.

Nothing here touches the network: series listings are stored straight into the cache, and
the background refresher is stubbed wherever a page is rendered.
"""

import re
import unittest
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import discover, series, web

try:
    from test_series import FIVE, StateCase, book, entry, store
except ImportError:
    from tests.test_series import FIVE, StateCase, book, entry, store

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def card(html, slug):
    return re.search(rf'<article class="ab-series-card" id="ab-series-{slug}".*?</article>', html, re.S).group(0)


def panel(html, slug):
    return re.search(rf'<section class="ab-series-panel" id="ab-series-missing-{slug}".*?</section>', html, re.S).group(0)


class TestOwnedOfTotal(StateCase):
    def test_owned_vs_total_math_and_missing_list(self):
        self.owned(1, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual((view["have"], view["total"]), (2, 5))
        self.assertEqual([(m["n"], m["asin"], m["title"], m["hours"]) for m in view["missing"]],
                         [(2, "B0000000J2", "Book 2", 10.0), (4, "B0000000J4", "Book 4", 10.0),
                          (5, "B0000000J5", "Book 5", 10.0)])
        self.assertEqual(view["source"], series.SOURCE_CATALOG)

    def test_an_owned_book_beyond_the_listing_still_counts(self):
        self.owned(1, 2, 7)  # the listing stops at 5; book 7 is yours anyway
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual((view["have"], view["total"]), (3, 6))

    def test_card_names_the_source_of_the_total(self):
        self.owned(1, 2, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        html = card(web.render_series(self.state), "jack-reacher")
        self.assertIn('of 5 owned</span>', html)
        self.assertIn('<div class="ab-series-src">total from the Audible catalog &#183; checked just now</div>', html)

    def test_a_kept_listing_after_a_failed_check_says_so(self):
        self.owned(1, 2, 3)
        key = store(self.state, "Jack Reacher", "Lee Child", FIVE)
        previous = self.state.series_cache_all()[key]
        with mock.patch.object(series, "FETCH", return_value=None):
            series.refresh_one(self.state, {"key": key, "name": "Jack Reacher", "author": "Lee Child",
                                            "previous": previous})
        html = card(web.render_series(self.state), "jack-reacher")
        self.assertIn("the last check failed, so this is the earlier listing", html)
        self.assertIn("of 5 owned", html)


class TestUnknownTotals(StateCase):
    def render_card(self):
        return card(web.render_series(self.state), "jack-reacher")

    def test_never_fetched_is_fetching_and_unknown(self):
        self.owned(1, 2)
        html = self.render_card()
        self.assertIn(">total unknown</span>", html)
        self.assertIn("fetching series size&hellip;", html)
        self.assertNotIn(" of ", html)
        self.assertEqual(series.aggregate(self.state)["series"][0]["source"], series.SOURCE_PENDING)

    def test_not_found_says_audible_has_no_listing(self):
        self.owned(1, 2)
        self.state.series_cache_put(series.group_key("Jack Reacher", "Lee Child"), False,
                                    {"reason": "not_found"}, [])
        html = self.render_card()
        self.assertIn(">total unknown</span>", html)
        self.assertIn("Audible has no listing for this series", html)
        self.assertNotIn(" of ", html)
        self.assertNotIn("MISSING", html)
        # and no missing panel, no Want all: there is nothing honest to count as missing
        page = web.render_series(self.state)
        self.assertNotIn('onclick="abSeriesWantAll(this)"', page)
        self.assertNotIn('<section class="ab-series-panel"', page)

    def test_unknown_in_the_detail_panel_hook(self):
        self.owned(1, 2)
        self.state.series_cache_put(series.group_key("Jack Reacher", "Lee Child"), False,
                                    {"reason": "not_found"}, [])
        self.assertIn("total unknown &#183; Audible has no listing for this series", web.render(self.state))

    def test_api_reports_the_source(self):
        self.owned(1, 2)
        self.state.series_cache_put(series.group_key("Jack Reacher", "Lee Child"), False,
                                    {"reason": "unreachable"}, [])
        public = series.public(series.aggregate(self.state)["series"][0])
        self.assertEqual(public["source"], "unreachable")
        self.assertIsNone(public["total"])


class TestWantButtons(StateCase):
    def setUp(self):
        super().setUp()
        self.owned(1, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)

    def test_each_missing_row_has_want_and_the_panel_has_want_all(self):
        html = panel(web.render_series(self.state), "jack-reacher")
        self.assertEqual(html.count('onclick="abSeriesWant(this)">Want</button>'), 3)
        self.assertIn('data-series="jack-reacher"', html)
        self.assertIn('onclick="abSeriesWantAll(this)">Want all 3 missing</button>', html)
        self.assertIn("You own 2 of 5 &#183; total from the Audible catalog", html)
        # the row: title, sequence and runtime
        row = re.search(r'<div class="ab-series-row" data-asin="B0000000J4".*?</div></div>.*?</div>', html, re.S).group(0)
        self.assertIn('<span class="ab-series-seq">4</span>', row)
        self.assertIn(">Book 4</div>", row)
        self.assertIn("10.0 hrs", row)

    def test_a_wishlisted_entry_shows_the_marker_and_want_all_counts_the_rest(self):
        self.state.wishlist_add({"asin": "B0000000J2", "title": "Book 2"})
        html = panel(web.render_series(self.state), "jack-reacher")
        self.assertEqual(html.count('onclick="abSeriesWant(this)">Want</button>'), 2)
        self.assertIn('<span class="ab-series-pill ab-series-wanted">On your wishlist</span>', html)
        self.assertIn(">Want the other 2 missing</button>", html)

    def test_all_wishlisted(self):
        for n in (2, 4, 5):
            self.state.wishlist_add({"asin": f"B0000000J{n}", "title": f"Book {n}"})
        html = panel(web.render_series(self.state), "jack-reacher")
        self.assertIn("All 3 on your wishlist", html)
        self.assertNotIn("abSeriesWantAll", html)
        self.assertNotIn("abSeriesWant(this)", html)

    def test_rendering_adds_nothing(self):
        web.render_series(self.state)
        web.render(self.state)
        self.assertEqual(self.state.wishlist_count(), 0)


class TestWantMissing(StateCase):
    def setUp(self):
        super().setUp()
        self.owned(1, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)

    def test_want_all_adds_n_rows_with_series_metadata(self):
        result, status, error = discover.want_missing(self.state, "jack-reacher")
        self.assertEqual((status, error), (200, ""))
        self.assertEqual(result["added"], ["B0000000J2", "B0000000J4", "B0000000J5"])
        self.assertEqual(self.state.wishlist_count(), 3)
        row = self.state.wishlist_get("B0000000J4")
        self.assertEqual((row["title"], row["series"], row["sequence"], row["origin"], row["status"]),
                         ("Book 4", "Jack Reacher", "4", "series", "wanted"))
        self.assertEqual(row["authors"], ["Lee Child"])

    def test_want_all_is_idempotent_and_skips_what_is_already_wanted(self):
        self.state.wishlist_add({"asin": "B0000000J2", "title": "Book 2"})
        result, _, _ = discover.want_missing(self.state, "jack-reacher")
        self.assertEqual(result["added"], ["B0000000J4", "B0000000J5"])
        self.assertEqual(result["already"], ["B0000000J2"])
        again, _, _ = discover.want_missing(self.state, "jack-reacher")
        self.assertEqual(again["added"], [])
        self.assertEqual(self.state.wishlist_count(), 3)

    def test_marked_and_owned_books_are_never_added(self):
        self.state.set_owned_mark("B0000000J5")
        result, _, _ = discover.want_missing(self.state, "jack-reacher")
        self.assertEqual(result["added"], ["B0000000J2", "B0000000J4"])
        self.assertIsNone(self.state.wishlist_get("B0000000J1"))

    def test_the_servers_missing_list_wins_over_a_stale_page(self):
        self.state.save(book("Lee Child/Jack Reacher/4", "Book 4", seq="4"))  # got it since the page drew
        result, _, _ = discover.want_missing(self.state, "jack-reacher")
        self.assertEqual(result["added"], ["B0000000J2", "B0000000J5"])

    def test_unknown_series_and_unknown_size_are_refused(self):
        self.assertEqual(discover.want_missing(self.state, "no-such-series")[1], 404)
        self.owned(1, 2, name="Lincoln Lawyer", author="Michael Connelly")  # never fetched
        result, status, error = discover.want_missing(self.state, "lincoln-lawyer")
        self.assertIsNone(result)
        self.assertEqual(status, 409)
        self.assertIn("unknown", error)
        self.assertEqual(self.state.wishlist_count(), 0)

    def test_one_transaction(self):
        with mock.patch.object(self.state, "wishlist_add", side_effect=AssertionError("one row at a time")):
            discover.want_missing(self.state, "jack-reacher")
        self.assertEqual(self.state.wishlist_count(), 3)


class TestWantEndpoints(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()
        for n in (1, 3):
            self.state.save(book(f"Lee Child/Jack Reacher/{n}", f"Book {n}", seq=str(n)))
        store(self.state, "Jack Reacher", "Lee Child", FIVE)

    def tearDown(self):
        self.schedule.stop()
        super().tearDown()

    def test_want_one_missing_entry(self):
        status, body = self.request("/api/wishlist", {"asin": "B0000000J4", "origin": "series"})
        self.assertEqual(status, 200)
        self.assertTrue(body["added"])
        self.assertEqual(body["item"]["series"], "Jack Reacher")
        self.assertEqual(body["item"]["sequence"], "4")

    def test_want_all_missing(self):
        status, body = self.request("/api/wishlist", {"series": "jack-reacher", "missing": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["added"]), 3)
        self.assertEqual(body["count"], 3)
        status, listed = self.request("/api/wishlist", headers={})
        self.assertEqual({r["asin"] for r in listed["items"]}, {"B0000000J2", "B0000000J4", "B0000000J5"})
        status, page = self.request("/series", raw=True, headers={})
        self.assertIn(b"All 3 on your wishlist", page)

    def test_want_all_refusals(self):
        status, _ = self.request("/api/wishlist", {"series": "jack-reacher"})
        self.assertEqual(status, 400)
        status, body = self.request("/api/wishlist", {"series": "nope", "missing": "all"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(self.state.wishlist_count(), 0)


class TestWantAllNeedsSignIn(LiveServerCase):
    signed_in = False

    def test_want_all_is_not_exempt(self):
        self.state.create_credentials("owner", auth_mod.hash_password("owner-pass-123", iterations=1000))
        self.state.save(book("Lee Child/Jack Reacher/1", "Book 1", seq="1"))
        self.state.save(book("Lee Child/Jack Reacher/3", "Book 3", seq="3"))
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        with mock.patch.object(series.REFRESHER, "schedule", return_value=False):
            status, _ = self.request("/api/wishlist", {"series": "jack-reacher", "missing": "all"})
        self.assertEqual(status, 401)
        self.assertEqual(self.state.wishlist_count(), 0)


if __name__ == "__main__":
    unittest.main()

"""0.17.0-dev1: the wishlist - the hand-off point between discovery and the Phase 3 watcher.

Nothing here touches the network: series listings are stored straight into the state DB.
"""

import http.client
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from audiobind import auth as auth_mod
from audiobind import catalog, discover, series, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase, seed_account
except ImportError:
    from tests.live import LiveServerCase, seed_account


def item(asin, title="A Book", authors=("Someone",), **extra):
    return {"asin": asin, "title": title, "authors": list(authors), **extra}


def store_series(state, name="Jack Reacher", author="Lee Child", owned=(1, 2), total=5):
    """Two owned books plus a stored five-book listing: #3-#5 are missing."""
    for n in owned:
        detail = {"match": {"asin": "", "title": f"Book {n}", "authors": [author],
                            "series": name, "sequence": str(n)}}
        state.save(UnitState(path=f"{author}/{name}/{n}", status="converted", title=f"Book {n}",
                             author=author, detail=json.dumps(detail)))
    entries = [{"asin": f"B0000000J{n}", "title": f"Book {n}", "sequence": str(n), "hours": 10.5,
                "image": f"https://m.media-amazon.com/images/I/J{n}.jpg"} for n in range(1, total + 1)]
    state.series_cache_put(series.group_key(name, author), True,
                           {"series_asin": "B0SERIES01", "title": name, "entries": entries,
                            "fetched_at": time.time()},
                           [(e["asin"], e["image"]) for e in entries])
    return entries


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


class TestWishlistState(StateCase):
    def test_add_then_list(self):
        self.assertTrue(self.state.wishlist_add(item("B000000001", "First", origin="search")))
        rows = self.state.wishlist_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["asin"], "B000000001")
        self.assertEqual(row["title"], "First")
        self.assertEqual(row["authors"], ["Someone"])
        self.assertEqual(row["status"], "wanted")
        self.assertEqual(row["origin"], "search")
        self.assertGreater(row["added_at"], 0)

    def test_re_add_is_idempotent_and_keeps_the_first_row(self):
        self.assertTrue(self.state.wishlist_add(item("B000000001", "First")))
        first = self.state.wishlist_get("B000000001")
        time.sleep(0.01)
        self.assertFalse(self.state.wishlist_add(item("B000000001", "Renamed")))
        self.assertEqual(self.state.wishlist_count(), 1)
        self.assertEqual(self.state.wishlist_get("B000000001"), first)

    def test_remove(self):
        self.state.wishlist_add(item("B000000001"))
        self.state.wishlist_add(item("B000000002"))
        self.assertTrue(self.state.wishlist_remove("B000000001"))
        self.assertFalse(self.state.wishlist_remove("B000000001"))
        self.assertEqual(self.state.wishlist_asins(), {"B000000002"})

    def test_list_is_newest_first(self):
        self.state.wishlist_add(item("B000000001"))
        time.sleep(0.01)
        self.state.wishlist_add(item("B000000002"))
        self.assertEqual([r["asin"] for r in self.state.wishlist_all()], ["B000000002", "B000000001"])

    def test_add_many_counts_only_new_rows(self):
        self.state.wishlist_add(item("B000000001"))
        added = self.state.wishlist_add_many([item("B000000001"), item("B000000002"), item("B000000003")])
        self.assertEqual(added, 2)
        self.assertEqual(self.state.wishlist_count(), 3)

    def test_survives_a_reopen(self):
        self.state.wishlist_add(item("B000000001", "Kept"))
        self.state.close()
        self.state = State(pathlib.Path(self.dir.name))
        self.assertEqual(self.state.wishlist_get("B000000001")["title"], "Kept")


class TestWishlistItem(StateCase):
    def test_bad_asin_is_refused(self):
        row, error = discover.wishlist_item(self.state, {"asin": "../etc", "title": "x"})
        self.assertIsNone(row)
        self.assertIn("not an ASIN", error)

    def test_unknown_asin_needs_a_title(self):
        row, error = discover.wishlist_item(self.state, {"asin": "B000000009"})
        self.assertIsNone(row)
        self.assertIn("send a title", error)
        row, error = discover.wishlist_item(self.state, {"asin": "b000000009", "title": "  Spaced   out ",
                                                         "authors": "Solo"})
        self.assertEqual(error, "")
        self.assertEqual(row["asin"], "B000000009")
        self.assertEqual(row["title"], "Spaced out")
        self.assertEqual(row["authors"], ["Solo"])
        self.assertEqual(row["origin"], "api")

    def test_series_listing_metadata_beats_what_the_client_posts(self):
        store_series(self.state)
        row, _ = discover.wishlist_item(self.state, {"asin": "B0000000J4", "title": "Forged",
                                                     "origin": "series"})
        self.assertEqual(row["title"], "Book 4")
        self.assertEqual(row["authors"], ["Lee Child"])
        self.assertEqual(row["series"], "Jack Reacher")
        self.assertEqual(row["sequence"], "4")
        self.assertEqual(row["hours"], 10.5)
        self.assertEqual(row["origin"], "series")

    def test_unknown_origin_falls_back_to_api(self):
        row, _ = discover.wishlist_item(self.state, {"asin": "B000000009", "title": "T", "origin": "<x>"})
        self.assertEqual(row["origin"], "api")


class TestWishlistApi(LiveServerCase):
    def test_add_list_re_add_remove(self):
        status, body = self.request("/api/wishlist", {"asin": "B000000001", "title": "First",
                                                      "authors": ["A. Writer"], "origin": "search"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["added"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["item"]["status"], "wanted")

        status, body = self.request("/api/wishlist", {"asin": "B000000001", "title": "First"})
        self.assertEqual(status, 200)
        self.assertFalse(body["added"], "a second add is a no-op, not an error")
        self.assertEqual(body["count"], 1)

        status, listing = self.request("/api/wishlist", headers={})
        self.assertEqual(status, 200)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0]["authors"], ["A. Writer"])

        status, body = self.request("/api/wishlist/remove", {"asin": "B000000001"})
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])
        self.assertEqual(body["count"], 0)
        status, body = self.request("/api/wishlist/remove", {"asin": "B000000001"})
        self.assertEqual(status, 404)

    def test_refusals(self):
        status, body = self.request("/api/wishlist", {"asin": "nope", "title": "x"})
        self.assertEqual(status, 400)
        status, body = self.request("/api/wishlist", {"asin": "B000000001"})
        self.assertEqual(status, 400)
        self.assertIn("title", body["error"])
        status, _ = self.request("/api/wishlist/remove", {"asin": ""})
        self.assertEqual(status, 400)
        self.assertEqual(self.state.wishlist_count(), 0)

    def test_post_has_the_cross_site_guard(self):
        status, _ = self.request("/api/wishlist", b'{"asin":"B000000001","title":"x"}',
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.request("/api/wishlist", {"asin": "B000000001", "title": "x"},
                                 headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.state.wishlist_count(), 0)


class TestDiscoveryNeedsSignIn(LiveServerCase):
    """Every new route sits behind the existing sign-in; none is exempt."""

    signed_in = False
    PAGES = ("/discover", "/discover?title=dune", "/discover/wishlist", "/discover/for-you")
    API_GETS = ("/api/wishlist", "/api/discover/search?title=dune", "/api/discover/recommendations")
    POSTS = ("/api/wishlist", "/api/wishlist/remove", "/api/discover/recommendations")

    def setUp(self):
        super().setUp()
        self.state.create_credentials("owner", auth_mod.hash_password("owner-pass-123", iterations=1000))

    def raw(self, method, path, body=None, cookie=""):
        """(status, Location) without following a redirect."""
        conn = http.client.HTTPConnection("127.0.0.1", CONFIG.web_port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status, response.getheader("Location")

    def test_no_new_route_is_exempt(self):
        for route in self.PAGES + self.API_GETS + self.POSTS:
            bare = route.split("?", 1)[0]
            self.assertNotIn(bare, web.Handler.AUTH_EXEMPT)
            self.assertNotIn(bare, web.Handler.SETUP_EXEMPT)

    def test_signed_out_requests_are_refused(self):
        with mock.patch.object(catalog, "query") as query:
            for route in self.PAGES:
                self.assertEqual(self.raw("GET", route), (303, "/login"), route)
            for route in self.API_GETS:
                self.assertEqual(self.raw("GET", route)[0], 401, route)
            for route in self.POSTS:
                self.assertEqual(self.raw("POST", route, {"asin": "B000000001", "title": "x"})[0], 401, route)
        query.assert_not_called()
        self.assertEqual(self.state.wishlist_count(), 0)

    def test_signed_in_requests_go_through(self):
        cookie = seed_account(self.state)
        with mock.patch.object(catalog, "query", return_value=[]):
            for route in self.PAGES + self.API_GETS:
                self.assertEqual(self.raw("GET", route, cookie=cookie)[0], 200, route)


if __name__ == "__main__":
    unittest.main()

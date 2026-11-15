"""0.18.0-dev12: a wishlist row can carry its own search title (the near-miss fix)."""

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from audiobind import mam, watcher, web
from audiobind.config import CONFIG
from audiobind.state import State

try:
    from test_watcher import WatcherCase, TORRENT, answer, entry
    from live import LiveServerCase
except ImportError:
    from tests.test_watcher import WatcherCase, TORRENT, answer, entry
    from tests.live import LiveServerCase

ASIN = "B000000001"


class TestWatcherUsesOverride(WatcherCase):
    def test_the_search_call_gets_the_override(self):
        self.want(title="Missing")
        self.state.wishlist_set_search_title(ASIN, "The Missing")
        client = self.mam(answer())
        with mock.patch.object(client, "search", wraps=client.search) as search:
            self.cycle()
        self.assertEqual(search.call_args.args[0], "The Missing")

    def test_no_override_searches_the_catalog_title(self):
        self.want(title="Project Hail Mary")
        client = self.mam(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        with mock.patch.object(client, "search", wraps=client.search) as search:
            self.cycle()
        self.assertEqual(search.call_args.args[0], "Project Hail Mary")
        self.assertEqual(self.row()["status"], "downloading")

    def test_override_clears_the_threshold_where_the_catalog_title_missed(self):
        torrents = answer(entry(1001, "The Missing"))
        self.want(title="Missing")
        self.mam(torrents)
        self.cycle()
        row = self.row()
        self.assertEqual(row["status"], "wanted")
        self.assertIn("not found on MAM", row["note"])
        self.state.wishlist_set_search_title(ASIN, "The Missing")
        self.mam(torrents, (200, {}, TORRENT))
        self.cycle()
        self.assertEqual(self.row()["status"], "downloading")
        self.assertEqual(self.row()["title"], "Missing")

    def test_select_candidate_scores_the_override(self):
        row = {"title": "Missing", "authors": []}
        found = [t for t in self._torrents("The Missing")]
        self.assertIsNone(mam.select_candidate(row, found)[0])
        self.assertIsNotNone(mam.select_candidate({**row, "search_title": "The Missing"}, found)[0])

    def _torrents(self, name):
        client = self.mam(answer(entry(7, name)))
        return client.search("x", "")


class TestSetSearchTitle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.wishlist_add({"asin": ASIN, "title": "Missing", "authors": ["A. Writer"]})
        self.state.wishlist_set_status(ASIN, "not_found", note="closest was 'The Missing' (0.85)", checked=True)

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_save_resets_the_row_and_keeps_the_display_title(self):
        row = self.state.wishlist_set_search_title(ASIN, "  The   Missing ")
        self.assertEqual((row["status"], row["note"], row["misses"], row["checked_at"]), ("wanted", "", 0, 0))
        self.assertEqual((row["title"], row["search_title"]), ("Missing", "The Missing"))
        self.assertEqual(self.state.wishlist_due(5)[0]["search_title"], "The Missing")

    def test_empty_and_whitespace_clear_the_override(self):
        for blank in ("", "   \t "):
            self.state.wishlist_set_search_title(ASIN, "The Missing")
            self.state.wishlist_set_status(ASIN, "not_found", note="x")
            row = self.state.wishlist_set_search_title(ASIN, blank)
            self.assertEqual(row["search_title"], "")
            self.assertEqual((row["status"], row["note"], row["title"]), ("wanted", "", "Missing"))

    def test_unknown_row(self):
        self.assertIsNone(self.state.wishlist_set_search_title("B0NOTHERE1", "x"))


class TestMigration(unittest.TestCase):
    def test_old_database_gains_the_column_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as d:
            State(pathlib.Path(d)).close()
            db = next(pathlib.Path(d).glob("*.db"))
            con = sqlite3.connect(db)
            con.execute("ALTER TABLE wishlist DROP COLUMN search_title")
            con.execute("INSERT INTO wishlist (asin, title, added_at) VALUES ('B000000009', 'Old', 1)")
            con.commit()
            con.close()
            state = State(pathlib.Path(d))
            try:
                cols = {r[1] for r in state.connection.execute("PRAGMA table_info(wishlist)")}
                self.assertIn("search_title", cols)
                row = state.wishlist_get("B000000009")
                self.assertEqual((row["title"], row["search_title"], row["status"]), ("Old", "", "wanted"))
                self.assertEqual(len(state.wishlist_due(5)), 1)
            finally:
                state.close()


class TestPage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        p = mock.patch.object(CONFIG, "mam_id", "x")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_action_everywhere_marker_only_when_set(self):
        self.state.wishlist_add({"asin": ASIN, "title": "Missing"})
        self.state.wishlist_add({"asin": "B000000002", "title": "Plain"})
        self.state.wishlist_set_search_title(ASIN, "The <Missing>")
        html = web.render_discover(self.state, "wishlist")
        self.assertEqual(html.count('onclick="abEditTitle(this)">Edit title</button>'), 2)
        self.assertEqual(html.count('class="ab-wl-search">searching for'), 1)
        self.assertIn("searching for &#8220;The &lt;Missing&gt;&#8221;", html)
        self.assertIn(">Missing</div>", html, "the display title does not change")


class TestApi(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.state.wishlist_add({"asin": ASIN, "title": "Missing"})
        self.state.wishlist_set_status(ASIN, "not_found", note="x")

    def test_save_and_clear(self):
        status, body = self.request("/api/wishlist/title", {"asin": ASIN, "title": "The Missing"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["item"]["search_title"], body["item"]["status"], body["item"]["title"]),
                         ("The Missing", "wanted", "Missing"))
        status, body = self.request("/api/wishlist/title", {"asin": ASIN, "title": "  "})
        self.assertEqual((status, body["item"]["search_title"]), (200, ""))

    def test_errors(self):
        self.assertEqual(self.request("/api/wishlist/title", {"asin": "B0MISSING1", "title": "x"})[0], 404)
        self.assertEqual(self.request("/api/wishlist/title", {"asin": "../x", "title": "x"})[0], 400)
        self.assertEqual(self.request("/api/wishlist/title", {"asin": ASIN, "title": 5})[0], 400)


if __name__ == "__main__":
    unittest.main()

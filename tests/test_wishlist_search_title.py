"""0.18.0-dev12: a wishlist row can carry its own search title (the near-miss fix)."""

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from audiobind import tracker, watcher, web
from audiobind.config import CONFIG
from audiobind.state import State
from tests.tracker_cfg import tracker_cfg

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
        client = self.fake_tracker(answer())
        with mock.patch.object(client, "search", wraps=client.search) as search:
            self.cycle()
        self.assertEqual(search.call_args.args[0], "The Missing")

    def test_no_override_searches_the_catalog_title(self):
        self.want(title="Project Hail Mary")
        client = self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        with mock.patch.object(client, "search", wraps=client.search) as search:
            self.cycle()
        self.assertEqual(search.call_args.args[0], "Project Hail Mary")
        self.assertEqual(self.row()["status"], "downloading")

    def test_override_clears_the_threshold_where_the_catalog_title_missed(self):
        torrents = answer(entry(1001, "The Missing"))
        self.want(title="Missing")
        self.fake_tracker(torrents)
        self.cycle()
        row = self.row()
        self.assertEqual(row["status"], "wanted")
        self.assertIn("not found on the tracker", row["note"])
        self.state.wishlist_set_search_title(ASIN, "The Missing")
        self.fake_tracker(torrents, (200, {}, TORRENT))
        self.cycle()
        self.assertEqual(self.row()["status"], "downloading")
        self.assertEqual(self.row()["title"], "Missing")

    def test_select_candidate_scores_the_override(self):
        row = {"title": "Missing", "authors": []}
        found = [t for t in self._torrents("The Missing")]
        self.assertIsNone(tracker.select_candidate(row, found)[0])
        self.assertIsNotNone(tracker.select_candidate({**row, "search_title": "The Missing"}, found)[0])

    def _torrents(self, name):
        client = self.fake_tracker(answer(entry(7, name)))
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

    def _force(self, status, **columns):
        """Put the row in ``status`` directly (the transition table forbids some of these)."""
        sets = ", ".join(["status = ?", "note = ?", "misses = ?", "checked_at = ?"])
        self.state.connection.execute(
            f"UPDATE wishlist SET {sets} WHERE asin = ?",
            (status, columns.get("note", "kept note"), columns.get("misses", 3), columns.get("checked_at", 12345.0), ASIN),
        )

    def test_searchable_statuses_still_reset_to_wanted(self):
        for status in ("wanted", "not_found", "error"):
            with self.subTest(status=status):
                self._force(status)
                row = self.state.wishlist_set_search_title(ASIN, f"Title for {status}")
                self.assertEqual((row["status"], row["note"], row["misses"], row["checked_at"]), ("wanted", "", 0, 0))
                self.assertEqual(row["search_title"], f"Title for {status}")

    def test_rows_past_searching_save_the_title_and_keep_everything_else(self):
        for status in ("downloading", "downloaded", "in_library"):
            with self.subTest(status=status):
                self._force(status)
                row = self.state.wishlist_set_search_title(ASIN, f"Title for {status}")
                self.assertEqual(
                    (row["status"], row["note"], row["misses"], row["checked_at"]), (status, "kept note", 3, 12345.0)
                )
                self.assertEqual(row["search_title"], f"Title for {status}")
                self.assertEqual(row["title"], "Missing")
                self.assertEqual(self.state.wishlist_due(5), [], "the watcher must not pick it up again")

    def test_clearing_the_title_on_a_row_past_searching_keeps_its_status(self):
        self._force("downloaded")
        self.state.wishlist_set_search_title(ASIN, "Something")
        row = self.state.wishlist_set_search_title(ASIN, "  ")
        self.assertEqual((row["status"], row["search_title"]), ("downloaded", ""))


class TestRequeue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.wishlist_add({"asin": ASIN, "title": "Missing", "authors": ["A. Writer"]})

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def _force(self, status):
        self.state.connection.execute(
            "UPDATE wishlist SET status = ?, misses = 4, checked_at = 999, tracker_tid = '77', "
            "torrent_hash = 'abc' WHERE asin = ?", (status, ASIN))

    def test_past_searching_rows_go_back_to_wanted_and_are_due(self):
        for status in ("downloading", "downloaded", "in_library"):
            with self.subTest(status=status):
                self._force(status)
                self.assertTrue(self.state.wishlist_requeue(ASIN))
                row = self.state.wishlist_get(ASIN)
                self.assertEqual((row["status"], row["misses"], row["checked_at"]), ("wanted", 0, 0))
                self.assertIn("re-queued", row["note"])
                self.assertEqual([r["asin"] for r in self.state.wishlist_due(5)], [ASIN])
                self.assertEqual((row["tracker_tid"], row["torrent_hash"]), ("77", "abc"),
                                 "the existing torrent's record is not touched")

    def test_searchable_and_unknown_rows_are_refused(self):
        for status in ("wanted", "not_found", "error"):
            with self.subTest(status=status):
                self._force(status)
                self.assertFalse(self.state.wishlist_requeue(ASIN))
                self.assertEqual(self.state.wishlist_get(ASIN)["status"], status)
        self.assertFalse(self.state.wishlist_requeue("B0NOTHERE1"))


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
        p = tracker_cfg("x")
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

    def test_requeue_button_only_on_rows_past_searching(self):
        for asin, status in (("B000000001", "downloading"), ("B000000002", "downloaded"),
                             ("B000000003", "wanted"), ("B000000004", "not_found")):
            self.state.wishlist_add({"asin": asin, "title": f"Book {status}"})
            self.state.connection.execute("UPDATE wishlist SET status = ? WHERE asin = ?", (status, asin))
        html = web.render_discover(self.state, "wishlist")
        self.assertEqual(html.count('onclick="abRequeue(this)">Re-queue</button>'), 2)
        self.assertIn('aria-label="Re-queue: Book downloading"', html)
        self.assertNotIn('aria-label="Re-queue: Book wanted"', html)
        self.assertNotIn('aria-label="Re-queue: Book not_found"', html)


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

    def test_title_on_a_downloading_book_keeps_its_status(self):
        self.state.connection.execute("UPDATE wishlist SET status = 'downloading' WHERE asin = ?", (ASIN,))
        status, body = self.request("/api/wishlist/title", {"asin": ASIN, "title": "The Missing"})
        self.assertEqual((status, body["item"]["status"], body["item"]["search_title"]),
                         (200, "downloading", "The Missing"))

    def test_requeue_endpoint(self):
        self.assertEqual(self.request("/api/wishlist/requeue", {"asin": ASIN})[0], 409, "not_found is Retry's job")
        self.state.connection.execute("UPDATE wishlist SET status = 'downloaded' WHERE asin = ?", (ASIN,))
        status, body = self.request("/api/wishlist/requeue", {"asin": ASIN})
        self.assertEqual((status, body["item"]["status"]), (200, "wanted"))
        self.assertEqual(self.request("/api/wishlist/requeue", {"asin": "B0MISSING1"})[0], 404)
        self.assertEqual(self.request("/api/wishlist/requeue", {"asin": "../x"})[0], 400)

    def test_errors(self):
        self.assertEqual(self.request("/api/wishlist/title", {"asin": "B0MISSING1", "title": "x"})[0], 404)
        self.assertEqual(self.request("/api/wishlist/title", {"asin": "../x", "title": "x"})[0], 400)
        self.assertEqual(self.request("/api/wishlist/title", {"asin": ASIN, "title": 5})[0], 400)


if __name__ == "__main__":
    unittest.main()

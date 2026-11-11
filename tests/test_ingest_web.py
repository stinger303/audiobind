"""0.16.0 ingest in the web UI: badge, source path, delete refusal, shelf, API fields."""

import re
import unittest
from unittest import mock

from audiobind import ingest, web
from audiobind.advice import explain
from audiobind.config import CONFIG
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

try:
    from test_ingest_discovery import touch, tree_snapshot
except ImportError:
    from tests.test_ingest_discovery import touch, tree_snapshot

TORRENT = "Andy Weir - Project Hail Mary (2021) [MAM]"
KEY = "ingest:" + TORRENT
REFUSAL = "delete is for library sources; remove the torrent in qBittorrent"


class IngestWebCase(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.ingest_root = self.root / "ingest"
        self.ingest_root.mkdir()
        self.saved_ingest = (CONFIG.ingest_dir, ingest._DISABLED_AT_START)
        CONFIG.ingest_dir = str(self.ingest_root)
        ingest._DISABLED_AT_START = ""
        touch(str(self.ingest_root / TORRENT / "01.mp3"))
        touch(str(self.lib / "Author" / "Book" / "01.mp3"))
        self.state.save(UnitState(path=KEY, status="review", confidence="LOW", title="Project Hail Mary",
                                  author="Andy Weir", note="", hours=16.2))
        self.state.save(UnitState(path="Author/Book", status="review", confidence="LOW", title="Library Book",
                                  author="Someone", hours=8.0))

    def tearDown(self):
        CONFIG.ingest_dir, ingest._DISABLED_AT_START = self.saved_ingest
        super().tearDown()

    def row_html(self, html, key):
        """The table row and its detail panel for one book."""
        attr = web._attr(key)
        start = html.index(f'<tr class="urow" onclick="toggleRow(this)" data-path="{attr}"')
        end = html.index("</td></tr>", html.index(f'<tr class="drow" data-path="{attr}"', start))
        return html[start:end]


class TestIngestRows(IngestWebCase):
    def test_ingest_rows_carry_the_badge_and_library_rows_do_not(self):
        html = web.render(self.state)
        ingest_row = self.row_html(html, KEY)
        library_row = self.row_html(html, "Author/Book")
        self.assertIn('class="ab-ingest-badge"', ingest_row)
        self.assertIn("&#8675; ingest", ingest_row)
        self.assertNotIn("ab-ingest-badge", library_row)
        # same table, same columns
        self.assertEqual(ingest_row.count("<td"), library_row.count("<td"))

    def test_badge_class_is_new_and_styled(self):
        self.assertIn(".ab-ingest-badge{", web.STYLES)
        self.assertEqual(web.STYLES.count(".ab-ingest-badge{"), 1)

    def test_detail_panel_shows_the_container_source_dir(self):
        html = web.render(self.state)
        expected = web._html(str(self.ingest_root / TORRENT))
        self.assertIn(f"Ingest source (read-only, still seeding): <span class=\"dim\"><code>{expected}</code>",
                      self.row_html(html, KEY))
        # the library panel is unchanged: its rel path, no ingest wording
        library_row = self.row_html(html, "Author/Book")
        self.assertIn('<span class="dim"><code>Author/Book</code></span>', library_row)
        self.assertNotIn("Ingest source", library_row)

    def test_delete_controls_are_marked_for_ingest_rows_only(self):
        html = web.render(self.state)
        ingest_row = self.row_html(html, KEY)
        library_row = self.row_html(html, "Author/Book")
        self.assertEqual(ingest_row.count('data-source="ingest"'), 2)  # kebab + panel Delete
        self.assertIn("remove the torrent in qBittorrent", ingest_row)
        self.assertNotIn("data-source", library_row)
        self.assertIn("Permanently delete this book's source folder", library_row)

    def test_search_for_ingest_narrows_to_ingest_rows(self):
        body = re.search(r'<tbody id="ab-body">(.*?)</tbody>', web.render(self.state, query="ingest"), re.S)
        self.assertIn("Project Hail Mary", body.group(1))
        self.assertNotIn("Library Book", body.group(1))

    def test_unmatched_ingest_row_is_on_the_shelf_with_the_usual_wording(self):
        html = web.render(self.state)
        shelf = html[html.index('id="ab-shelf"'):html.index("</section>", html.index('id="ab-shelf"'))]
        card_start = shelf.index(f'data-path="{web._attr(KEY)}"')
        card = shelf[card_start:shelf.index('<div class="ab-pactions">', card_start)]
        why, _todo = explain("review", "LOW", "", {}, tier=str(CONFIG.auto_convert))
        self.assertIn(web._html(why), card)
        self.assertIn("ab-ingest-badge", card)
        actions = shelf[card_start:]
        for label in ("Retry now", "Convert anyway", "Edit ASIN"):
            self.assertIn(label, actions)

    def test_no_badge_anywhere_when_there_are_no_ingest_rows(self):
        self.state.delete(KEY)
        CONFIG.ingest_dir = ""
        html = web.render(self.state)
        self.assertNotIn('class="ab-ingest-badge"', html)
        self.assertNotIn('data-source="ingest"', html)


class TestIngestDelete(IngestWebCase):
    def test_delete_on_an_ingest_row_is_refused_and_nothing_is_touched(self):
        before = tree_snapshot(self.ingest_root)
        with mock.patch.object(web.shutil, "rmtree", side_effect=AssertionError("rmtree called")):
            status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 409)
        self.assertIn(REFUSAL, body["error"])
        self.assertEqual(body["source"], "ingest")
        self.assertIsNotNone(self.state.get(KEY))
        self.assertEqual(tree_snapshot(self.ingest_root), before)

    def test_refused_even_without_confirmation_and_with_ingest_off(self):
        status, body = self.post({"action": "delete", "path": KEY})
        self.assertEqual(status, 409)
        self.assertIn(REFUSAL, body["error"])
        CONFIG.ingest_dir = ""
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 409)
        self.assertTrue((self.ingest_root / TORRENT / "01.mp3").exists())

    def test_a_published_ingest_book_keeps_its_output_too(self):
        out = self.out / "Andy Weir" / "Project Hail Mary" / "Project Hail Mary.m4b"
        out.parent.mkdir(parents=True)
        out.write_text("m4b")
        self.state.save(UnitState(path=KEY, status="converted", output=str(out)))
        status, _body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 409)
        self.assertTrue(out.exists())

    def test_library_delete_still_works(self):
        status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.state.get("Author/Book"))
        self.assertTrue((self.ingest_root / TORRENT / "01.mp3").exists())


class TestIngestApi(IngestWebCase):
    def test_units_endpoint_includes_source(self):
        status, body = self.request("/api/units")
        self.assertEqual(status, 200)
        sources = {u["path"]: u["source"] for u in body["units"]}
        self.assertEqual(sources, {KEY: "ingest", "Author/Book": "library"})

    def test_state_endpoint_reports_the_ingest_dir(self):
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["ingest_dir"], str(self.ingest_root))
        self.assertTrue(body["ingest_enabled"])
        self.assertIn("counts", body)  # additive: the existing fields are all still there

    def test_state_endpoint_with_ingest_off(self):
        CONFIG.ingest_dir = ""
        _status, body = self.request("/api/state")
        self.assertEqual(body["ingest_dir"], "")
        self.assertFalse(body["ingest_enabled"])


class TestPageScript(unittest.TestCase):
    def test_ingest_delete_skips_the_destructive_modal(self):
        start = web.SCRIPT.index("function abOpenDeleteModal")
        body = web.SCRIPT[start:web.SCRIPT.index("abDeleteTrigger = trigger;", start)]
        self.assertIn("dataset.source === 'ingest'", body)
        self.assertNotIn("confirm: true", body)


if __name__ == "__main__":
    unittest.main()

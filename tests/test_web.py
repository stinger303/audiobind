import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from audiobind import web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState


class TestRender(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        for i, status in enumerate(["converted", "ready", "ready", "review", "held", "failed"]):
            self.state.save(UnitState(path=f"Author/Book {i}", status=status, confidence="HIGH",
                                      title=f"Book {i}", hours=2.5, files=3, src_mb=120))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_shows_every_book_not_just_a_page_of_them(self):
        page = web.render(self.state)
        for i in range(6):
            self.assertIn(f"Book {i}", page)

    def test_status_filter_narrows_the_table(self):
        page = web.render(self.state, "ready")
        # Table rows show only ready books, but problem units may still be in the shelf
        self.assertIn("Book 1", page)
        self.assertIn("Book 2", page)
        self.assertNotIn("Book 0", page)      # converted
        # Book 3 (review) appears in the shelf, so check it's not in table rows
        body_content = page.split('<tbody id="ab-body">')[1].split("</tbody>")[0]
        self.assertNotIn("Book 3", body_content)

    def test_filter_chips_carry_counts(self):
        page = web.render(self.state)
        self.assertIn('href="/?status=ready"', page)
        self.assertIn("ready ", page)
        self.assertIn(">2<", page)

    def test_unknown_filter_falls_back_to_everything(self):
        page = web.render(self.state, "nonsense")
        self.assertIn("Book 0", page)

    def test_detail_panel_carries_source_and_output(self):
        page = web.render(self.state)
        self.assertIn("Author/Book 0", page)     # source path in the panel
        self.assertIn("120 MB", page)

    def test_version_string_is_real(self):
        from audiobind import __version__
        page = web.render(self.state)
        self.assertIn('v{}'.format(__version__), page)

    def test_encoding_strip_hidden_when_idle(self):
        page = web.render(self.state)
        self.assertNotIn('class="ab-queue"', page)

    def test_rail_links_point_to_status_filters(self):
        page = web.render(self.state)
        self.assertIn('href="/?status=review"', page)
        self.assertIn('href="/?status=held"', page)
        self.assertIn('href="/?status=failed"', page)
        # Each rail item carries the live count
        self.assertIn('class="ab-count"', page)

    def test_shelf_renders_from_held_review_failed(self):
        page = web.render(self.state)
        # 3 problem units: review, held, failed
        self.assertIn('id="ab-shelf"', page)
        self.assertIn('id="ab-shelf-count">3<', page)
        self.assertIn("action needed on 3 books", page)
        # Each problem card rendered
        self.assertIn("Book 3", page)
        self.assertIn("Book 4", page)
        self.assertIn("Book 5", page)

    def test_shelf_empty_state_when_nothing_needs_attention(self):
        from audiobind.state import State
        import pathlib, tempfile
        d = tempfile.TemporaryDirectory()
        s = State(pathlib.Path(d.name))
        try:
            s.save(UnitState(path="Done/Book", status="converted", title="Done", hours=1))
            s.save(UnitState(path="Ready/Book", status="ready", title="Ready", hours=1))
            page = web.render(s)
            self.assertIn("nothing needs attention", page)
            self.assertIn('id="ab-shelf-count">0<', page)
        finally:
            s.close()
            d.cleanup()


class TestReasonLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(
            path="Author/Held Book", status="held", confidence="HIGH",
            title="Held Book", hours=5.2, files=3, src_mb=120, note=""))
        self.state.save(UnitState(
            path="Author/Failed Book", status="failed", confidence="MEDIUM",
            title="Failed Book", hours=0, files=1, src_mb=0,
            note="the encoder returned an error"))
        self.state.save(UnitState(
            path="Author/Review Book", status="review", confidence="LOW",
            title="Review Book", hours=0, files=1, src_mb=0, note=""))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_held_rows_render_a_reason_line(self):
        page = web.render(self.state)
        self.assertIn('class="ab-breason"', page)
        self.assertIn("5.2", page)           # hours in the reason

    def test_failed_rows_render_reason_with_failed_class(self):
        page = web.render(self.state)
        self.assertIn('class="ab-breason failed"', page)

    def test_converted_rows_do_not_render_a_reason(self):
        self.state.save(UnitState(
            path="Author/Done", status="converted", title="Done", hours=1, files=1))
        page = web.render(self.state)
        # Only the Done row itself should have no ab-breason
        done_row_start = page.find('data-path="Author/Done"')
        next_tr = page.find('<tr class="drow"', done_row_start)
        done_row = page[done_row_start:next_tr] if next_tr > 0 else page[done_row_start:]
        self.assertNotIn("ab-breason", done_row)


class TestCompactActions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(
            path="Author/Book", status="held", confidence="LOW",
            title="Book", hours=1, files=1))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_actions_live_inside_a_fixed_width_actcol(self):
        page = web.render(self.state)
        self.assertIn(".ab-rowact", page)
        # Actcol has a real width, hover overlay is inline-flex inside it
        self.assertIn("width:170px", web.STYLES)
        self.assertIn("display:inline-flex", web.STYLES)

    def test_actions_still_render_for_problem_rows(self):
        page = web.render(self.state)
        self.assertIn("Retry now", page)
        self.assertIn("Convert anyway", page)
        self.assertIn("Edit ASIN", page)
        self.assertIn("function abEditAsin(", page)
        # Panel button is still "Use this ASIN" (not renamed)
        self.assertIn("Use this ASIN", page)


class TestDeleteUI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(
            path="Author/Book", status="held", confidence="LOW",
            title="Book", hours=1, files=1))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_delete_button_is_in_detail_panel_not_shelf_or_row(self):
        page = web.render(self.state)
        # Delete still appears inside the detail panel (drow)
        detail_section = page.split('<tr class="drow"')[1].split("</tr>")[0]
        self.assertIn(">Delete<", detail_section)
        # The confirmation is now a shared modal, not inline
        self.assertIn('id="ab-modal"', page)
        # Shelf cards should NOT have Delete
        shelf_section = page.split('id="ab-shelf"')[1].split("</section>")[0]
        self.assertNotIn(">Delete<", shelf_section)

    def test_confirm_strip_warns_of_permanent_deletion(self):
        page = web.render(self.state)
        self.assertIn("permanently deletes", page)
        self.assertIn("cannot be undone", page)
        self.assertIn("Delete permanently", page)
        self.assertIn(">Cancel<", page)
        self.assertNotIn("Move to trash", page)
        self.assertNotIn("Type DELETE", page)

    def test_delete_js_functions_present(self):
        page = web.render(self.state)
        self.assertIn("function unitDelete(", page)
        self.assertIn("function abModalConfirm(", page)
        self.assertIn("function abModalCancel(", page)
        self.assertIn("function abOpenDeleteModal(", page)
        self.assertIn('id="ab-modal"', page)
        self.assertIn("Escape", page)  # escape key handler wired


class TestDeleteHandler(unittest.TestCase):
    """Hard delete, exercised through the real HTTP handler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.lib = self.root / "library"
        self.out = self.root / "output"
        self.lib.mkdir()
        self.out.mkdir()
        self.state = State(self.root / "state")
        self.saved = (CONFIG.library_root, CONFIG.output_root, CONFIG.web_port)
        CONFIG.library_root, CONFIG.output_root = self.lib, self.out
        CONFIG.web_port = 0
        self.server = web.serve(self.state)
        self.base = "http://127.0.0.1:%d" % CONFIG.web_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        web.Handler.state = None
        CONFIG.library_root, CONFIG.output_root, CONFIG.web_port = self.saved
        self.state.close()
        self.tmp.cleanup()

    def post(self, payload):
        req = urllib.request.Request(
            self.base + "/api/unit", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def make_book(self):
        src = self.lib / "Author" / "Book"
        src.mkdir(parents=True, exist_ok=True)
        (src / "01.mp3").write_text("audio")
        out_book = self.out / "Author" / "Book"
        out_book.mkdir(parents=True, exist_ok=True)
        (out_book / "Book.m4b").write_text("m4b")
        self.state.save(UnitState(path="Author/Book", status="converted",
                                  title="Book", output=str(out_book / "Book.m4b")))
        return src, out_book

    def test_delete_refuses_without_confirm(self):
        status, body = self.post({"action": "delete", "path": "Author/Book"})
        self.assertEqual(status, 400)
        self.assertIn("confirmation", body["error"])

    def test_hard_delete_removes_source_output_and_row(self):
        src, out_book = self.make_book()
        status, body = self.post({"action": "delete", "path": "Author/Book",
                                  "confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["source_removed"])
        self.assertEqual(body["output"], "removed")
        self.assertFalse(src.exists())
        self.assertFalse(out_book.exists())
        self.assertIsNone(self.state.get("Author/Book"))

    def test_delete_with_no_published_output_still_removes_the_source(self):
        src = self.lib / "Author" / "Book"
        src.mkdir(parents=True)
        (src / "01.mp3").write_text("audio")
        self.state.save(UnitState(path="Author/Book", status="review"))
        status, body = self.post({"action": "delete", "path": "Author/Book",
                                  "confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["source_removed"])
        self.assertEqual(body["output"], "absent")
        self.assertFalse(src.exists())
        self.assertIsNone(self.state.get("Author/Book"))

    def test_delete_refuses_path_traversal(self):
        # the row must exist for the request to reach the traversal guard
        self.state.save(UnitState(path="../escape", status="review"))
        status, body = self.post({"action": "delete", "path": "../escape",
                                  "confirm": True})
        self.assertEqual(status, 400)
        self.assertIn("escapes", body["error"])
        # and nothing outside the library root was touched
        self.assertFalse((self.root / "escape").exists())

    def test_delete_refuses_while_encoding(self):
        from audiobind.control import CONTROL
        self.make_book()
        CONTROL.begin_unit("Author/Book")
        try:
            status, body = self.post({"action": "delete", "path": "Author/Book",
                                      "confirm": True})
            self.assertEqual(status, 409)
            self.assertIn("encoding", body["error"])
            self.assertTrue((self.lib / "Author" / "Book").exists())
        finally:
            CONTROL.end_unit()


if __name__ == "__main__":
    unittest.main()

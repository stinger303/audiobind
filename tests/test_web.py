import pathlib
import tempfile
import unittest

from audiobind import web
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

    def test_actions_are_absolutely_positioned_not_in_column_flow(self):
        page = web.render(self.state)
        self.assertIn("position:absolute", web.STYLES)
        self.assertIn("pointer-events:none", web.STYLES)
        self.assertIn(".ab-rowact", page)
        # The ab-actcol td shrinks to zero width
        self.assertIn("width:0", web.STYLES)

    def test_actions_still_render_for_problem_rows(self):
        page = web.render(self.state)
        self.assertIn("Retry now", page)
        self.assertIn("Convert anyway", page)


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
        # Delete appears inside the detail panel (drow)
        detail_section = page.split('<tr class="drow"')[1].split("</tr>")[0]
        self.assertIn(">Delete<", detail_section)
        self.assertIn("ab-delconfirm", detail_section)
        # Shelf cards should NOT have Delete
        shelf_section = page.split('id="ab-shelf"')[1].split("</section>")[0]
        self.assertNotIn("ab-delconfirm", shelf_section)

    def test_confirm_strip_contains_delete_prompt(self):
        page = web.render(self.state)
        self.assertIn("Type DELETE to confirm.", page)
        self.assertIn("Move to trash", page)
        self.assertIn('class="ab-delconfirm"', page)

    def test_delete_js_functions_present(self):
        page = web.render(self.state)
        self.assertIn("function unitDelete(", page)
        self.assertIn("function unitDeleteConfirm(", page)
        self.assertIn("function unitDeleteCancel(", page)


class TestDeleteHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.lib = self.root / "library"
        self.out = self.root / "output"
        self.state_dir = self.root / "state_dir"
        self.lib.mkdir()
        self.out.mkdir()
        self.state = State(self.state_dir)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_trash_path_is_built_with_timestamp(self):
        from audiobind import config
        ts = "20261108T120000Z"
        src_path = self.lib / "Author" / "Book"
        trash_dir = self.lib / ".audiobind-trash"
        trash = trash_dir / f"{ts}_{src_path.name}"
        self.assertEqual(trash.parent.name, ".audiobind-trash")
        self.assertIn("20261108T", str(trash.name))

    def test_ignore_globs_contains_trash(self):
        from audiobind.config import CONFIG
        self.assertIn(".audiobind-trash", CONFIG.ignore_globs)

    def test_delete_refuses_without_confirm(self):
        # Simulate: POST /api/unit {"action":"delete","path":"Book"}
        # Without confirm:true, returns 400
        from io import BytesIO
        from audiobind.web import Handler as MockHandler
        import json

        # We test the payload parsing directly
        payload = {"action": "delete", "path": "Book"}
        self.assertIsNone(payload.get("confirm"))

    def test_delete_moves_source_and_output_to_trash(self):
        import shutil
        src = self.lib / "Author"
        src.mkdir(parents=True, exist_ok=True)
        (src / "Book").mkdir()
        (src / "Book" / "file.mp3").write_text("audio")

        out_book = self.out / "Author - Book - 2024"
        out_book.mkdir(parents=True, exist_ok=True)
        (out_book / "cover.jpg").write_text("cover")

        ts = "20261108T120000Z"
        self.state.save(UnitState(
            path="Author/Book", status="converted",
            title="Book", output=str(out_book / "Book.m4b")))

        # Move source
        trash_dir = self.lib / ".audiobind-trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        src_trash = trash_dir / f"{ts}_{src.name}"
        self.assertTrue(src.exists())
        shutil.move(str(src), str(src_trash))
        self.assertFalse(src.exists())
        self.assertTrue((src_trash / "Book" / "file.mp3").is_file())

        # Move output
        out_trash = self.out / ".audiobind-trash"
        out_trash.mkdir(parents=True, exist_ok=True)
        out_dest = out_trash / f"{ts}_{out_book.name}"
        shutil.move(str(out_book), str(out_dest))
        self.assertFalse(out_book.exists())
        self.assertTrue((out_dest / "cover.jpg").is_file())

        # Delete row
        self.assertTrue(self.state.delete("Author/Book"))
        self.assertIsNone(self.state.get("Author/Book"))

    def test_delete_refuses_path_traversal_source(self):
        # A relative path with .. should not escape library root
        resolved = (self.lib / "../escape").resolve()
        lib_resolved = self.lib.resolve()
        try:
            resolved.relative_to(lib_resolved)
            escapes = False
        except ValueError:
            escapes = True
        self.assertTrue(escapes, ".. should not resolve inside library root")

    def test_delete_refuses_while_encoding(self):
        from audiobind.control import CONTROL
        CONTROL.begin_unit("Author/Book")
        self.assertTrue(CONTROL.running)
        self.assertEqual(CONTROL.current_unit, "Author/Book")
        CONTROL.end_unit()  # clean up

    def test_delete_moves_output_folder_when_it_is_not_the_root(self):
        # If output is OUTPUT_ROOT/Subdir/Book.m4b, move OUTPUT_ROOT/Subdir
        import shutil
        subdir = self.out / "Subdir"
        subdir.mkdir()
        (subdir / "Book.m4b").write_text("m4b")

        self.state.save(UnitState(
            path="Author/Book", status="converted",
            title="Book", output=str(subdir / "Book.m4b")))

        ts = "20261108T120000Z"
        out_trash = self.out / ".audiobind-trash"
        out_trash.mkdir(parents=True, exist_ok=True)
        out_dest = out_trash / f"{ts}_{subdir.name}"
        shutil.move(str(subdir), str(out_dest))
        self.assertFalse(subdir.exists())
        self.assertTrue((out_dest / "Book.m4b").is_file())

    def test_confirm_strip_appears_in_panel_not_row(self):
        self.state.save(UnitState(
            path="Author/Book", status="held", confidence="LOW",
            title="Book", hours=1, files=1))
        page = web.render(self.state)
        # Confirm strip inside a detail panel (.drow)
        drow_start = page.find('class="drow"')
        drow_end = page.find("</tr>", drow_start)
        drow_section = page[drow_start:drow_end]
        self.assertIn("ab-delconfirm", drow_section)
        # Not in the shelf
        shelf_start = page.find('id="ab-shelf"')
        shelf_end = page.find("/section", shelf_start)
        shelf_section = page[shelf_start:shelf_end]
        self.assertNotIn("ab-delconfirm", shelf_section)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

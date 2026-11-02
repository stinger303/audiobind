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
        self.assertIn("Book 1", page)
        self.assertIn("Book 2", page)
        self.assertNotIn("Book 0", page)      # converted
        self.assertNotIn("Book 3", page)      # review

    def test_filter_chips_carry_counts(self):
        page = web.render(self.state)
        self.assertIn('href="/?status=ready"', page)
        self.assertIn("ready <b>2</b>", page)

    def test_unknown_filter_falls_back_to_everything(self):
        page = web.render(self.state, "nonsense")
        self.assertIn("Book 0", page)

    def test_detail_panel_carries_source_and_output(self):
        page = web.render(self.state)
        self.assertIn("Author/Book 0", page)     # source path in the panel
        self.assertIn("120 MB", page)


if __name__ == "__main__":
    unittest.main()

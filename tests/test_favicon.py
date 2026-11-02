import pathlib
import unittest

from audiobind import web


class TestIcon(unittest.TestCase):
    def test_icon_file_is_packaged_next_to_the_package(self):
        self.assertTrue(web.ICON_PATH.exists(), f"missing icon at {web.ICON_PATH}")
        self.assertIn("<svg", web.ICON_PATH.read_text(encoding="utf-8"))

    def test_page_declares_the_favicon(self):
        import tempfile
        from audiobind.state import State
        with tempfile.TemporaryDirectory() as tmp:
            page = web.render(State(pathlib.Path(tmp)))
        self.assertIn('rel="icon"', page)
        self.assertIn("/favicon.svg", page)


if __name__ == "__main__":
    unittest.main()

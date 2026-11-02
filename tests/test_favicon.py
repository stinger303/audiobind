import pathlib
import tempfile
import unittest

from audiobind import web
from audiobind.state import State


class TestIcon(unittest.TestCase):
    def test_icon_file_is_packaged_next_to_the_package(self):
        self.assertTrue(web.ICON_PATH.exists(), f"missing icon at {web.ICON_PATH}")
        self.assertIn("<svg", web.ICON_PATH.read_text(encoding="utf-8"))

    def test_page_declares_the_favicon(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            state = State(pathlib.Path(tmp))
            try:
                page = web.render(state)
            finally:
                state.close()
        self.assertIn('rel="icon"', page)
        self.assertIn("/favicon.svg", page)


if __name__ == "__main__":
    unittest.main()

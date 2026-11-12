"""v0.14.4: braces in user data must never reach a str.format() template.

One ``{`` in a folder name, a catalog title, an override or an ffmpeg error used to raise
KeyError/IndexError inside render(), which blanked the library page for everyone.
"""

import json
import pathlib
import tempfile
import unittest
import urllib.request

from audiobind import web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import sign_in_urllib, sign_out_urllib
except ImportError:
    from tests.live import sign_in_urllib, sign_out_urllib


def brace_unit(path="Frank Herbert/Dune {Unabridged}", **kw):
    detail = {
        "action": "review",
        "hours": 21.0,
        "match": {
            "asin": "B002V1OF70",
            "title": "Dune {x} {0} {dp}",
            "authors": ["Frank {Herbert}"],
            "narrators": ["Scott {Brick"],
            "hours": 21.0,
            "confidence": "LOW",
            "image": "",
            "asin_pin_failed": "",
        },
        "source": {"parts": 1, "files": 2, "mb": 500, "hours": 21.0, "codecs": ["mp3"], "channels": []},
        "output": None,
    }
    kw.setdefault("status", "review")
    kw.setdefault("confidence", "LOW")
    kw.setdefault("title", "Dune {x}")
    kw.setdefault("author", "Frank {Herbert}")
    kw.setdefault("note", "ffmpeg said {stream:0} } {")
    kw.setdefault("detail", json.dumps(detail))
    kw.setdefault(
        "alternates",
        json.dumps([{"asin": "B00ALT0001", "title": "Dune {alt}", "authors": ["{Someone}"], "score": 0.8}]),
    )
    return UnitState(path=path, **kw)


class TestBracesRender(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_brace_in_folder_path_renders(self):
        self.state.save(brace_unit())
        page = web.render(self.state)
        self.assertIn('data-path="Frank Herbert/Dune {Unabridged}"', page)
        self.assertIn('class="drow"', page)

    def test_brace_in_titles_and_notes_renders_literally(self):
        self.state.save(brace_unit())
        page = web.render(self.state)
        self.assertIn("Dune {x} {0} {dp}", page)  # catalog title, not substituted
        self.assertIn("ffmpeg said {stream:0} } {", page)
        self.assertIn("Dune {alt}", page)

    def test_brace_in_metadata_override_renders(self):
        self.state.save(brace_unit(path="Author/Plain"))
        self.state.set_overrides("Author/Plain", {"title": "Book {1}", "series": "{series}", "series_index": "{"})
        page = web.render(self.state)
        self.assertIn('value="Book {1}"', page)
        self.assertIn('value="{series}"', page)
        self.assertIn('data-path="Author/Plain"', page)

    def test_delete_button_keeps_path_and_title(self):
        self.state.save(brace_unit())
        page = web.render(self.state)
        self.assertIn('data-path="Frank Herbert/Dune {Unabridged}" data-title="Dune {x}"', page)


class TestBracesOverHttp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.saved_port = CONFIG.web_port
        CONFIG.web_port = 0
        self.server = web.serve(self.state)
        self.base = "http://127.0.0.1:%d" % CONFIG.web_port
        sign_in_urllib(self.state)

    def tearDown(self):
        sign_out_urllib()
        self.server.shutdown()
        self.server.server_close()
        web.Handler.state = None
        CONFIG.web_port = self.saved_port
        self.state.close()
        self.dir.cleanup()

    def test_library_page_serves_200_with_the_row(self):
        self.state.save(brace_unit())
        self.state.save(brace_unit(path="Author/Other"))
        self.state.set_overrides("Author/Other", {"title": "Book {1}"})
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode()
        self.assertIn('class="urow"', body)
        self.assertIn("Dune {Unabridged}", body)
        self.assertIn("Book {1}", body)


if __name__ == "__main__":
    unittest.main()

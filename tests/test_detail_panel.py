"""The v0.12.3 review detail panel: plain-English copy, meter, re-open banner."""

import json
import pathlib
import tempfile
import unittest

from audiobind import web
from audiobind.state import State, UnitState


def review_unit(**kw):
    kw.setdefault("path", "Michael J. Sullivan/Heir of Novron")
    kw.setdefault("status", "review")
    kw.setdefault("confidence", "MEDIUM")
    detail = {
        "reopen": kw.pop("reopen", False),
        "action": "review",
        "hours": 31.79,
        "match": {
            "asin": "B008UCYCSQ",
            "title": "Heir of Novron",
            "authors": ["Michael J. Sullivan"],
            "hours": 31.5,
            "confidence": "MEDIUM",
            "image": "",
            "asin_pin_failed": "",
        },
        "source": {"parts": 1, "files": 32, "mb": 900, "hours": 31.79, "codecs": [], "channels": []},
        "output": None,
    }
    kw.setdefault("detail", json.dumps(detail))
    kw.setdefault("asin", "B008UCYCSQ")
    return UnitState(**kw)


class TestDetailPanelCopy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(review_unit())

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def page(self):
        return web.render(self.state)

    def test_section_headers_are_plain_english(self):
        page = self.page()
        for header in (
            "Why it's waiting for you",
            "Your options",
            "Your source files",
            "What you'll get",
            "The Audible match",
        ):
            self.assertIn(header, page)

    def test_source_files_read_like_sentences(self):
        page = self.page()
        self.assertIn("32 audio files in 1 part", page)
        self.assertIn("31.79 hours of audio", page)
        self.assertIn("Audio format not detected in the files", page)

    def test_jargon_fields_are_gone(self):
        page = self.page()
        self.assertNotIn("codec not recorded", page)
        self.assertNotIn("planned action:", page)
        self.assertNotIn("Why it is review", page)

    def test_the_match_block_names_the_book_author_and_links_the_asin(self):
        page = self.page()
        self.assertIn("Audible match: <b>Heir of Novron</b>", page)
        self.assertIn("Author: <b>Michael J. Sullivan</b>", page)
        self.assertIn("Match confidence: <b>MEDIUM</b>", page)
        self.assertIn("audible.com/pd?asin=B008UCYCSQ", page)

    def test_the_match_block_names_the_book_and_links_the_asin(self):
        page = self.page()
        self.assertIn("Audible match: <b>Heir of Novron</b>", page)
        self.assertIn("Match confidence: <b>MEDIUM</b>", page)
        self.assertIn("audible.com/pd?asin=B008UCYCSQ", page)

    def test_a_confidence_meter_marks_the_current_tier(self):
        page = self.page()
        self.assertIn('class="ab-meter"', page)
        self.assertIn('<span class="ab-on">MEDIUM</span>', page)
        self.assertIn('<span class="ab-off">LOW</span>', page)
        self.assertIn("How sure we are this is the right book", page)


class TestReopenBanner(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def page(self):
        return web.render(self.state)

    def test_a_recheck_that_still_fails_reopens_the_panel(self):
        self.state.save(review_unit(reopen=True))
        page = self.page()
        self.assertIn("data-reopen", page)
        self.assertIn("Re-checked &mdash; still not confident enough", page)
        self.assertIn("below your high auto-convert setting", page)

    def test_a_plain_review_row_does_not_reopen(self):
        self.state.save(review_unit(reopen=False))
        page = self.page()
        self.assertNotIn(' data-reopen="1"', page)
        self.assertNotIn("Re-checked &mdash;", page)

    def test_a_held_recheck_gets_the_edition_banner(self):
        u = review_unit(reopen=True, status="held")
        self.state.save(u)
        page = self.page()
        self.assertIn("Re-checked &mdash; still not a runtime match", page)


if __name__ == "__main__":
    unittest.main()

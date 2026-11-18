"""The rail's Library item goes to the library and is only marked active on it.

Before this, Library pointed at "/" - which renders the attention view whenever anything needs
review - and its ab-active was hardcoded, so Library and Needs your review were the same page and
the rail always claimed you were on Library.
"""

import pathlib
import re
import tempfile
import unittest

from audiobind import web
from audiobind.state import State, UnitState

REVIEW = UnitState(path="Child/Persuader", status="review", confidence="MEDIUM", title="Persuader", hours=13.1)
DONE = UnitState(path="Child/61 Hours", status="converted", confidence="HIGH", title="61 Hours", hours=13.18)


def render_with(units, status=""):
    with tempfile.TemporaryDirectory() as tmp:
        state = State(pathlib.Path(tmp))
        try:
            for u in units:
                state.save(u)
            return web.render(state, status)
        finally:
            state.close()


def rail(page):
    return page.split('<aside class="ab-rail">')[1].split("</aside>")[0]


def rail_link(page, label):
    """The opening <a ...> tag of the rail item whose text is `label`."""
    m = re.search(r'(<a class="ab-rail-item[^"]*"[^>]*>)\s*<span class="ab-glyph">[^<]*</span> ' + re.escape(label), rail(page))
    assert m, label
    return m.group(1)


def is_active(tag):
    return " ab-active" in tag and 'aria-current="page"' in tag


def is_inactive(tag):
    return " ab-active" not in tag and "aria-current" not in tag


class TestRailNav(unittest.TestCase):
    def setUp(self):
        self.default = render_with([REVIEW, DONE])
        self.all = render_with([REVIEW, DONE], "all")
        self.attention = render_with([REVIEW, DONE], "attention")

    def test_default_is_still_the_attention_view(self):
        self.assertIn('<a class="chip on ab-alert" href="/?status=attention">', self.default)
        self.assertIn('<a class="chip" href="/?status=all">', self.default)

    def test_library_points_at_the_library(self):
        for page in (self.default, self.all, self.attention):
            self.assertIn('href="/?status=all"', rail_link(page, "Library"))
            self.assertIn('<a href="/?status=all" title="Back to the library">Audiobind</a>', rail(page))
            self.assertNotIn('href="/"', rail(page))

    def test_library_active_only_on_all_view(self):
        self.assertTrue(is_active(rail_link(self.all, "Library")))
        self.assertTrue(is_inactive(rail_link(self.attention, "Library")))
        self.assertTrue(is_inactive(rail_link(self.default, "Library")))

    def test_needs_review_active_only_on_attention_view(self):
        self.assertTrue(is_active(rail_link(self.attention, "Needs your review")))
        self.assertTrue(is_active(rail_link(self.default, "Needs your review")))
        self.assertTrue(is_inactive(rail_link(self.all, "Needs your review")))

    def test_status_filter_view_marks_neither(self):
        page = render_with([REVIEW, DONE], "converted")
        self.assertTrue(is_inactive(rail_link(page, "Library")))
        self.assertTrue(is_inactive(rail_link(page, "Needs your review")))

    def test_nothing_to_review_default_is_library(self):
        page = render_with([DONE])
        self.assertTrue(is_active(rail_link(page, "Library")))
        self.assertTrue(is_inactive(rail_link(page, "Needs your review")))

    def test_other_pages_mark_neither(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                state.save(REVIEW)
                for page in (web.render_settings(state), web.render_series(state)):
                    self.assertIn('href="/?status=all"', rail_link(page, "Library"))
                    self.assertTrue(is_inactive(rail_link(page, "Library")))
                    self.assertTrue(is_inactive(rail_link(page, "Needs your review")))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()

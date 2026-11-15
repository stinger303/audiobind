"""0.21.0-dev2: UI quick wins - calm the page, one primary per view."""

import pathlib
import re
import tempfile
import unittest
from unittest import mock

from audiobind import web
from audiobind.control import CONTROL
from audiobind.state import State, UnitState


class QuickWins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_rail_alert_only_when_the_count_is_above_zero(self):
        page = web.render(self.state)
        for name in ("review", "held", "failed"):
            self.assertIn(f'class="ab-rail-item" href="/?status={name}"', page)
        self.state.save(UnitState(path="A/1", status="failed", title="T"))
        page = web.render(self.state)
        self.assertIn('class="ab-rail-item ab-alert" href="/?status=failed"', page)
        self.assertIn('class="ab-rail-item" href="/?status=held"', page)

    def test_series_item_is_never_an_alarm(self):
        self.assertNotIn("ab-alert", web._series_rail_item(5))

    def test_pause_and_stop_only_while_encoding(self):
        idle = web.render(self.state)
        self.assertNotIn("Pause after this book", idle)
        self.assertNotIn("Stop now", idle)
        with mock.patch.object(CONTROL, "current_unit", "Some/Book"), \
                mock.patch.object(CONTROL, "current_title", "Some Book"):
            running = web.render(self.state)
        self.assertIn("Pause after this book", running)
        self.assertIn("Stop now", running)
        self.assertIn('id="ab-qtitle">Some Book<', running)  # the catalog title, not the raw key
        self.assertIn('id="ab-qprog"', running)

    def test_paused_still_offers_resume(self):
        with mock.patch.object(CONTROL, "_paused", True, create=True), \
                mock.patch.object(type(CONTROL), "paused", new_callable=mock.PropertyMock, return_value=True):
            self.assertIn(">Resume</button>", web.render(self.state))

    def test_labels_and_buttons(self):
        page = web.render(self.state)
        self.assertIn(">Scan for changes</button>", page)
        self.assertIn("Re-match all&hellip;</button>", page)
        self.assertNotIn(">Scan now<", page)
        self.assertIn(">Library</a></h1>", page)
        scan = re.search(r'<button class="([^"]*)"[^>]*id="ab-scanbtn"', page)
        self.assertNotIn("primary", scan.group(1))

    def test_styles(self):
        self.assertIn("--ab-text4:#7d828b", web.STYLES)
        self.assertIn(".ab-st-hold .ab-st-dot{background:var(--ab-amber)}", web.STYLES)
        self.assertIn("tr.drow,tr.urow{scroll-margin-top:40px}", web.STYLES)
        self.assertRegex(web.STYLES, r"\.ab-breason\{[^}]*white-space:nowrap;overflow:hidden;text-overflow:ellipsis")
        self.assertNotIn("button.dry{", web.STYLES)  # only .dry.on is tinted

    def test_no_tofu_glyphs(self):
        page = web.render(self.state)
        self.assertNotIn("&#x23F8;", page)
        self.assertIn("&#8214;", page)
        self.assertNotIn("ud83d", page)
        self.assertNotIn("\U0001f512", web.lock_chip("title", True))

    def test_reason_carries_the_full_sentence_in_its_title(self):
        self.state.save(UnitState(path="A/1", status="held", title="T", note="x"))
        page = web.render(self.state)
        self.assertRegex(page, r'<span class="ab-breason" title="[^"]+">')

    def test_shelf_starts_collapsed_unless_opened(self):
        self.assertIn("let collapsed = true;", web.render(self.state))


if __name__ == "__main__":
    unittest.main()

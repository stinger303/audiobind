"""UI pass A1: confirmations before destructive actions, feedback on every control, form and
accessibility semantics. These pin the rendered markup/script; there is no browser in CI."""

import re
import tempfile
import unittest
from pathlib import Path

from audiobind import web
from audiobind.state import State


def js(name):
    return getattr(web, name)


def body_of(src, name):
    """The text of one top-level ``function name(`` in a script, up to the next top-level function."""
    start = src.index(f"function {name}(")
    nxt = re.search(r"\n(?:function |let |const |document\.|window\.|//)", src[start + 10:])
    return src[start : start + 10 + nxt.start()] if nxt else src[start:]


class TestConfirmations(unittest.TestCase):
    def test_convert_anyway_names_the_book_and_confirms(self):
        fn = body_of(js("SCRIPT"), "abUnitAction")
        self.assertIn("action === 'force'", fn)
        self.assertIn("confirm(", fn)
        self.assertIn("abBookTitle(", fn)
        # the confirm comes before anything is sent
        self.assertLess(fn.index("confirm("), fn.index("fetch("))

    def test_rematch_all_confirms(self):
        fn = body_of(js("SCRIPT"), "act")
        self.assertIn("'recheck'", fn)
        self.assertIn("confirm(", fn)

    def test_stop_says_it_also_pauses(self):
        fn = body_of(js("SCRIPT"), "stop_now")
        self.assertIn("stay paused until you press Resume", fn)

    def test_reset_defaults_confirms_first(self):
        fn = body_of(js("SETTINGS_SCRIPT"), "abResetSettings")
        self.assertLess(fn.index("confirm("), fn.index("fetch("))

    def test_no_stray_apostrophes_in_confirm_strings(self):
        # an unescaped ' inside a '...' JS string breaks the whole script block
        for name in ("SCRIPT", "SETTINGS_SCRIPT"):
            for line in js(name).splitlines():
                if "confirm(" in line or re.match(r"\s+'[A-Z][^']*'\s*\+?\s*$", line):
                    self.assertEqual((line.count("'") - line.count("\\'")) % 2, 0, line)


class TestControlFeedback(unittest.TestCase):
    def test_controls_share_one_helper_that_reports_failure(self):
        src = js("SCRIPT")
        fn = body_of(src, "abControl")
        self.assertIn("working", fn)
        self.assertIn("out.ok", fn)
        self.assertIn("abToast(", fn)
        for name in ("act", "stop_now", "dry", "abWatcherDry"):
            self.assertIn("abControl(", body_of(src, name))
            self.assertNotIn("location.reload()", body_of(src, name))

    def test_use_this_edition_reports_a_refusal(self):
        fn = body_of(js("SCRIPT"), "abUseEdition")
        self.assertIn("out.ok", fn)
        self.assertIn("abToast(", fn)


class TestModalSemantics(unittest.TestCase):
    def test_every_modal_is_modal_and_described(self):
        for tpl in (web.PAGE, web.SETTINGS_PAGE):
            self.assertIn('role="dialog" aria-modal="true"', tpl)
            self.assertIn('aria-describedby="ab-modal-warning"', tpl)
            self.assertIn('id="ab-modal-warning"', tpl)

    def test_tab_stays_inside_an_open_modal(self):
        nav = web.NAV_SCRIPT
        self.assertIn("e.key !== 'Tab'", nav)
        self.assertIn("getElementById('ab-modal')", nav)
        self.assertIn("button:not([disabled])", nav)


class TestFormsAndLabels(unittest.TestCase):
    def render_settings(self, tab=None):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            try:
                return web.render_settings(state, tab) if tab else web.render_settings(state)
            finally:
                state.close()

    def test_filter_box_has_an_accessible_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            try:
                page = web.render(state)
            finally:
                state.close()
        self.assertRegex(page, r'<input id="ab-search"[^>]*aria-label="Filter books"')

    def test_settings_controls_get_named_from_their_label(self):
        src = web.SETTINGS_SCRIPT
        self.assertIn("'.ab-setting-label'", src)
        self.assertIn("setAttribute('aria-label', name)", src)
        self.assertIn("aria-describedby", src)

    def test_feedback_spans_are_live_regions(self):
        page = self.render_settings()
        for fb in re.findall(r'<span class="ab-fb"[^>]*>', page):
            self.assertIn('role="status"', fb, fb)
        self.assertTrue(re.findall(r'<span class="ab-fb"[^>]*>', page))

    def test_url_boxes_get_the_url_keyboard_and_no_capital(self):
        page = self.render_settings("watcher")
        box = re.search(r'<input type="text"[^>]*inputmode="url"[^>]*>', page)
        if box is None:  # the watcher tab only exists when the watcher is configured
            self.skipTest("no watcher fields on this tab in this configuration")
        self.assertIn('autocapitalize="none"', box.group(0))

    def test_account_submit_cannot_double_fire(self):
        fn = body_of(web.ACCOUNT_SCRIPT, "abAccountSubmit")
        self.assertIn("submit.disabled = true", fn)
        self.assertIn(".finally(", fn)

    def test_asin_box_is_named_and_enter_submits(self):
        src = js("SCRIPT")
        self.assertIn("function abEnterClicks(", src)
        self.assertIn("button[data-enter]", src)
        fn = body_of(src, "abUnitAction")
        self.assertIn("Enter the Audible ASIN first", fn)  # empty box: say so, do not just move focus


if __name__ == "__main__":
    unittest.main()

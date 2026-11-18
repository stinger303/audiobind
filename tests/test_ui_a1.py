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


if __name__ == "__main__":
    unittest.main()

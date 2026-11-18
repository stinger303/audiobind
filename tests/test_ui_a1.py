"""UI pass A1: confirmations before destructive actions, feedback on every control, form and
accessibility semantics. These pin the rendered markup/script; there is no browser in CI."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestSessionExpiry(unittest.TestCase):
    def test_every_signed_in_page_carries_the_401_redirect(self):
        for tpl in (web.PAGE, web.SETTINGS_PAGE, web.SERIES_PAGE, web.DISCOVER_PAGE):
            self.assertIn("{nav_script}", tpl)
        # sign-in and first-run setup are plain form posts: a wrong password must show its message,
        # never bounce the visitor back to the form they are already on
        for tpl in (web.LOGIN_PAGE, web.SETUP_PAGE):
            self.assertNotIn("{nav_script}", tpl)
            self.assertNotIn("fetch(", tpl)
        nav = web.NAV_SCRIPT
        self.assertIn("r.status === 401", nav)
        self.assertIn("location.href = '/login'", nav)
        # installed before anything else in the script block calls fetch
        self.assertLess(nav.index("window.fetch = "), nav.index("abNavMq"))


class TestFocusIsVisible(unittest.TestCase):
    def test_no_control_hides_its_focus_without_a_replacement(self):
        css = web.STYLES
        self.assertIn("input[type=range]:focus-visible{outline:2px solid", css)  # the slider had outline:none
        self.assertIn(".ab-search:focus{outline:2px solid", css)
        self.assertIn(".ab-rail-item:focus-visible{outline:2px solid", css)
        self.assertNotRegex(css, r"\.ab-search:focus\{outline:none")
        self.assertNotRegex(css, r"\.ab-rail-item:focus-visible\{outline:none")


class TestWishlistButtonsNameTheBook(unittest.TestCase):
    ROW = {"asin": "B005EHN61O", "title": 'Dune "Deluxe" <1>', "status": "not_found", "added_at": 1.0,
           "authors": ["Frank Herbert"]}

    def test_every_row_button_says_which_book(self):
        html = web._wishlist_row_html(self.ROW)
        for verb in ("Retry", "Edit title", "Remove from wishlist"):
            self.assertIn(f'aria-label="{verb}: Dune &quot;Deluxe&quot; &lt;1&gt;"', html)

    def test_get_this_names_the_book_and_drops_the_name_when_the_words_change(self):
        item = {"asin": "B005EHN61O", "title": "Dune"}
        with mock.patch.object(web.watcher_mod, "configured", return_value=True):
            self.assertIn('aria-label="Get this: Dune"', web._disc_row(item, "search"))
        fn = body_of(js("DISCOVER_SCRIPT"), "abWant")
        self.assertIn("removeAttribute('aria-label')", fn)  # 'On your wishlist' must not be spoken as 'Get this: ...'
        self.assertEqual(fn.count("speak()"), 2)  # and a refused or failed request puts it back


class TestWishlistConfirmsWhenTheTorrentIsAlreadyRunning(unittest.TestCase):
    def test_remove_confirms_for_downloading_and_downloaded_before_sending(self):
        script = js("DISCOVER_SCRIPT")
        self.assertIn("row.dataset.status === 'downloading' || row.dataset.status === 'downloaded'", script)
        fn = body_of(script, "abUnwant")
        self.assertIn("abWishlistInFlight(row)", fn)
        self.assertIn("does not stop or delete the torrent", fn)
        self.assertLess(fn.index("confirm("), fn.index("abPost("))

    def test_saving_a_title_confirms_because_it_requeues_the_book(self):
        fn = body_of(js("DISCOVER_SCRIPT"), "abEditTitle")
        self.assertIn("abWishlistInFlight(row)", fn)
        self.assertIn("second copy to qBittorrent", fn)
        self.assertLess(fn.index("confirm("), fn.index("/api/wishlist/title"))
        # the server really does reset the status; if that ever stops, this warning is wrong
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            try:
                state.wishlist_add({"asin": "B005EHN61O", "title": "Dune"})
                state.connection.execute("UPDATE wishlist SET status='downloading' WHERE asin='B005EHN61O'")
                row = state.wishlist_set_search_title("B005EHN61O", "Dune Frank Herbert")
            finally:
                state.close()
        self.assertEqual(row["status"], "wanted")


class TestPhoneInputs(unittest.TestCase):
    def test_phone_inputs_are_16px_so_ios_does_not_zoom(self):
        phone = web.STYLES.split("@media(max-width:640px){", 1)[1]
        self.assertRegex(phone[:400], r"select, textarea\{font-size:16px\}")

    def test_placeholder_and_focus_use_readable_colours(self):
        self.assertIn(".ab-in::placeholder{color:var(--ab-text4)", web.STYLES)
        self.assertNotIn("#5d6479", web.STYLES)
        self.assertRegex(web.STYLES, r"\.ab-in:focus\{outline:2px solid var\(--ab-accent-bright\)")
        self.assertNotIn("outline:none;border-color:#2b5ea8", web.STYLES)


if __name__ == "__main__":
    unittest.main()

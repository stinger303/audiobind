"""The two ways into the Set up downloading wizard: the Library page's one-time card and the Watcher tab's button."""

import json
import re
import unittest
import urllib.error
import urllib.request
from unittest import mock

from audiobind import wizard, web, watcher
from audiobind.control import CONTROL

try:
    from test_wizard_shell import WizardCase, seed_account
except ImportError:
    from tests.test_wizard_shell import WizardCase, seed_account

CARD = 'id="ab-wizcard"'


class EntryCase(WizardCase):
    def post(self, path, payload, headers=None):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode()
            finally:
                exc.close()

    def tracker_configured(self):
        patcher = mock.patch.object(watcher, "missing_settings", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLibraryCard(EntryCase):
    def test_a_fresh_install_shows_the_card_with_both_buttons(self):
        _status, page = self.get("/")
        self.assertIn(CARD, page)
        self.assertIn("Do you also want Audiobind to fetch books for you?", page)
        self.assertRegex(page, r'<a class="btn ab-save" id="ab-wizcard-yes" href="/setup/downloading">'
                               r"Set up downloading</a>")
        self.assertIn(">No, just my library</button>", page)
        self.assertIn('aria-labelledby="ab-wizcard-head"', page)

    def test_the_first_sentence_is_true_whether_or_not_a_scan_is_running(self):
        with mock.patch.object(type(CONTROL), "scanning", new_callable=mock.PropertyMock, return_value=True):
            self.assertIn("Your library is being scanned.", self.get("/")[1])
        with mock.patch.object(type(CONTROL), "scanning", new_callable=mock.PropertyMock, return_value=False):
            page = self.get("/")[1]
        self.assertNotIn("is being scanned", page)
        self.assertIn("Your library is set up.", page)

    def test_it_is_only_on_the_plain_library_view(self):
        for path in ("/?status=all", "/"):
            self.assertIn(CARD, self.get(path)[1], path)
        for path in ("/?status=review", "/?status=attention", "/?q=dune", "/settings", "/discover"):
            self.assertNotIn(CARD, self.get(path)[1], path)

    def test_no_is_stored_and_the_card_never_returns(self):
        status, body = self.post(wizard.API, {"action": "dismiss"})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertTrue(wizard.read(self.state)["dismissed"])
        for path in ("/", "/?status=all"):
            self.assertNotIn(CARD, self.get(path)[1])
        # and it survives a restart: only the settings store is read
        from audiobind.state import State

        self.state.close()
        self.state = State(self.root / "state")
        web.Handler.state = self.state
        self.assertNotIn(CARD, self.get("/")[1])

    def test_it_is_not_shown_while_a_tracker_is_already_set_up(self):
        self.tracker_configured()
        self.assertNotIn(CARD, self.get("/")[1])
        self.assertFalse(wizard.read(self.state)["dismissed"], "hidden, not answered")

    def test_the_answer_endpoint_refuses_what_it_does_not_know(self):
        self.assertEqual(self.post(wizard.API, {"action": "frobnicate"})[0], 400)
        self.assertEqual(self.post(wizard.API, {})[0], 400)
        self.assertFalse(wizard.read(self.state)["dismissed"])

    def test_the_answer_endpoint_is_post_only_and_guarded_like_the_others(self):
        self.assertEqual(self.get(wizard.API)[0], 405)
        status, _body = self.post(wizard.API, {"action": "dismiss"}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _body = self.post(wizard.API, {"action": "dismiss"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(wizard.read(self.state)["dismissed"])

    def test_the_script_posts_the_answer_and_removes_the_card(self):
        self.assertIn("function abWizardDismiss()", web.SCRIPT)
        self.assertIn("'/api/setup/downloading'", web.SCRIPT)


class TestSignedOutAnswer(EntryCase):
    signed_in = False

    def test_a_signed_out_answer_is_refused_and_stores_nothing(self):
        seed_account(self.state)
        status, _body = self.post(wizard.API, {"action": "dismiss"})
        self.assertEqual(status, 401)
        self.assertFalse(wizard.read(self.state)["dismissed"])


class TestWatcherTabButton(EntryCase):
    def test_the_button_is_at_the_top_while_the_watcher_is_not_configured(self):
        page = web.render_settings(self.state, "watcher")
        self.assertIn('id="ab-wiz-open" href="/setup/downloading">Set up downloading</a>', page)
        self.assertLess(page.index('id="ab-wiz-open"'), page.index('data-setting="tracker_url"'),
                        "above the fields")

    def test_no_button_once_the_watcher_is_configured(self):
        self.tracker_configured()
        self.assertNotIn('id="ab-wiz-open"', web.render_settings(self.state, "watcher"))

    def test_the_button_is_a_link_not_a_submit_so_the_form_is_not_saved_by_it(self):
        page = web.render_settings(self.state, "watcher")
        tag = re.search(r'<a [^>]*id="ab-wiz-open"[^>]*>', page).group(0)
        self.assertNotIn("onclick", tag)


if __name__ == "__main__":
    unittest.main()

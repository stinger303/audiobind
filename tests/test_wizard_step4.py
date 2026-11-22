"""Set up downloading, step 4 (your tracker) and the end state. The boxes in the order a person fills them,
saved under the Watcher tab's own keys through its own checks; Test sends exactly ONE search with the
configured request and says what came back in plain words, never the cookie, the address or the raw answer;
the politeness limits hold; the shipped copy never names a tracker site; the end state sums up and links
on."""

import codecs
import html
import json
import re
import urllib.error
import urllib.request
from unittest import mock

from audiobind import config, egress, tracker, watcher, web, wizard
from audiobind.config import CONFIG, SETTING_KEYS

try:
    from live import seed_account
    from test_wizard_step3 import OTHER, SEEN, Step3Case
except ImportError:
    from tests.live import seed_account
    from tests.test_wizard_step3 import OTHER, SEEN, Step3Case

COOKIE = "SESSION-VALUE-never-shown-4711"
SEARCH_BODY = '{"q": "{{title}}", "cat": "{{category}}"}'
#: the site this app is most often pointed at, kept out of this file's text on purpose: the rule under test is
#: that its name is never shipped, and a test file that spelled it out would ship it itself
SITE = codecs.decode("zlnabanzbhfr", "rot13")
SITE_SHORT = codecs.decode("ZNZ", "rot13")

TRACKER_KEYS = ("tracker_url", "tracker_session", "tracker_cookie_name", "tracker_search_path",
                "tracker_search_body", "tracker_search_category", "tracker_download_path",
                "tracker_freeleech_param", "tracker_user_agent")


def results(n, first="Dune", author="Frank Herbert"):
    rows = [{"id": i + 1, "title": first if i == 0 else f"Other book {i}", "author_info": {"1": author}}
            for i in range(n)]
    return json.dumps({"data": rows}).encode()


class FakeTracker:
    """The tracker's network: records every request and answers (status, body) in turn."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers) or [(200, results(12))], []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers)})
        status, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return status, {}, body


class Step4Case(Step3Case):
    def setUp(self):
        super().setUp()
        for key in TRACKER_KEYS:
            patcher = mock.patch.object(CONFIG, key, "")
            patcher.start()
            self.addCleanup(patcher.stop)
        for key, value in (("tracker_search_method", "POST"), ("tracker_search_encoding", "json"),
                           ("tracker_download_param", "tid")):
            patcher = mock.patch.object(CONFIG, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        config._EFFECTIVE.clear()

    def configure(self, **extra):
        settings = {"tracker_url": "https://tracker.example", "tracker_session": COOKIE,
                    "tracker_cookie_name": "sid", "tracker_search_path": "/api/search.json",
                    "tracker_search_body": SEARCH_BODY, "tracker_search_category": "13",
                    "tracker_download_path": "/download.php", **extra}
        self.save(settings)

    def page4(self):
        return self.get(wizard.PATH + "?step=4")[1]

    def ask(self, fake, query="Dune"):
        """POST step 4's Test with the tracker's network replaced by ``fake`` (and no spacing wait)."""
        real = watcher.make_tracker

        def factory(route):
            client = real(route)
            client.transport = fake
            client.limiter = tracker.RateLimiter(spacing=0.0)
            return client

        with mock.patch.object(watcher, "make_tracker", factory):
            status, body = self.post(wizard.API, {"action": "tracker_test", "query": query})
        return status, json.loads(body), body

    @staticmethod
    def text(page):
        """The guide's visible words: block ends become spaces, other tags vanish, entities are decoded."""
        body = re.search(r'<div class="ab-wiz">.*</div>', page, re.S).group(0)
        body = re.sub(r"</(li|p|h2|div|b|label|legend|option|button|a)>", " ", body)
        return " ".join(html.unescape(re.sub(r"<[^>]+>", "", body)).split())


class TestTheStep(Step4Case):
    def test_step_four_is_built_and_no_step_says_not_built(self):
        self.assertEqual(wizard.BUILT, (1, 2, 3, 4))
        for step in ("1", "2", "3", "4", "done"):
            with self.subTest(step=step):
                self.assertNotIn("is not built yet", self.get(f"{wizard.PATH}?step={step}")[1])
        self.assertIn("Step 4 of 4: Your tracker", self.page4())

    def test_the_boxes_come_in_the_order_a_person_fills_them(self):
        page = self.page4()
        order = ["ab-wiz-turl", "ab-wiz-tsession", "ab-wiz-tcookie", "ab-wiz-tpath", "ab-wiz-tbody",
                 "ab-wiz-tmethod", "ab-wiz-tenc", "ab-wiz-tcat", "ab-wiz-tdl", "ab-wiz-test"]
        where = [page.index(f'id="{element}"') for element in order]
        self.assertEqual(where, sorted(where))
        for element in order[:-1]:
            self.assertIn(f'for="{element}"', page, "every box has a label")

    def test_the_search_choices_hold_their_usual_defaults(self):
        page = self.page4()
        self.assertRegex(page, r'id="ab-wiz-tmethod"[^>]*>.*?<option value="POST" selected>')
        self.assertRegex(page, r'id="ab-wiz-tenc"[^>]*>.*?<option value="json" selected>')

    def test_every_box_saves_under_a_watcher_tab_key(self):
        keys = set(re.findall(r'data-key="([a-z_]+)"', self.page4()))
        self.assertEqual(keys, {key for key, _label in wizard.TRACKER_FIELDS})
        self.assertTrue(keys <= set(SETTING_KEYS))
        self.assertIn("fetch('/api/settings'", web.WIZARD_SCRIPT.split("function abWizTrackerSave")[1])

    def test_every_box_the_watcher_needs_is_on_the_step(self):
        from audiobind.config import WATCHER_REQUIRED

        self.assertTrue({attr for _var, attr in WATCHER_REQUIRED} <= set(wizard.TRACKER_LABELS))
        self.assertEqual(len(wizard.tracker_missing()), len(WATCHER_REQUIRED),
                         "nothing set: all of them named")

    def test_a_bad_value_is_refused_by_the_settings_pages_own_checks(self):
        status, body = self.post("/api/settings", {"settings": {"tracker_url": "not a url"}})
        self.assertEqual(status, 400)
        self.assertIn("Tracker URL:", body)

    def test_with_no_cookie_the_step_explains_it_without_asking_for_a_password(self):
        page = self.page4()
        text = self.text(page)
        self.assertIn("What is a session cookie?", text)
        self.assertIn("preferences page", text)
        self.assertIn("Audiobind never asks for your tracker password.", text)
        for phrase in ("log in", "login", "sign in", "user name", "username", "your password here"):
            self.assertNotIn(phrase, text.lower())
        self.assertNotRegex(page, r'id="ab-wiz-tsession"[^>]*type="text"', "the cookie box is a password box")

    def test_a_saved_cookie_is_never_served_and_the_explanation_steps_aside(self):
        self.configure()
        page = self.page4()
        self.assertNotIn(COOKIE, page)
        self.assertNotIn("What is a session cookie?", page)
        self.assertIn('placeholder="set - leave blank to keep it"', page)
        self.assertIn('id="ab-wiz-tclear"', page)

    def test_continue_saves_and_reaches_the_end(self):
        page = self.page4()
        self.assertIn("abWizardTrackerContinue()", page)
        self.assertIn("'/setup/downloading?step=done'", web.WIZARD_SCRIPT)
        self.assertIn(f'id="ab-wiz-back" href="{wizard.PATH}?step=3"', page)


class TestTheTest(Step4Case):
    def test_one_search_with_the_configured_request_and_a_plain_result(self):
        self.configure()
        fake = FakeTracker((200, results(12)))
        status, answer, _raw = self.ask(fake)
        self.assertEqual(status, 200, answer)
        self.assertEqual(len(fake.calls), 1, "a test is one request")
        call = fake.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://tracker.example/api/search.json"))
        self.assertEqual(json.loads(call["data"]), {"q": "Dune", "cat": "13"})
        self.assertEqual(call["headers"]["Cookie"], f"sid={COOKIE}")
        self.assertEqual(answer["message"],
                         "Your tracker answered: 12 results, first: Dune by Frank Herbert.")

    def test_an_empty_title_searches_for_the_default_one(self):
        self.configure()
        fake = FakeTracker((200, results(1, first="Dune")))
        _status, answer, _raw = self.ask(fake, query="")
        self.assertEqual(json.loads(fake.calls[0]["data"])["q"], watcher.DEFAULT_TEST_QUERY)
        self.assertEqual(answer["message"], "Your tracker answered: 1 result, first: Dune by Frank Herbert.")

    def test_no_results_is_still_a_working_search(self):
        self.configure()
        _status, answer, _raw = self.ask(FakeTracker((200, b'{"error": "Nothing returned, out of 0"}')))
        self.assertTrue(answer["ok"])
        self.assertIn("found nothing for “Dune”", answer["message"])

    def test_the_answer_never_carries_the_cookie_the_address_or_raw_text(self):
        self.configure(tracker_search_method="GET")
        answers = [(200, b'{"error": "RAW-ANSWER-TEXT from the site"}'), (200, b"<html>RAW-PAGE-TEXT</html>"),
                   (403, b"RAW-FORBIDDEN-TEXT"), (200, results(3))]
        for status_code, body in answers:
            with self.subTest(status=status_code, body=body[:20]):
                watcher.WATCHER._client = None  # a fresh client: no backoff carried from the answer before
                fake = FakeTracker((status_code, body))
                _status, _answer, raw = self.ask(fake)
                self.assertEqual(len(fake.calls), 1)
                self.assertIn("?", fake.calls[0]["url"], "GET: the search goes in the query string")
                for secret in (COOKIE, "RAW-", "tracker.example", "?q=", "api/search.json"):
                    self.assertNotIn(secret, raw)

    def test_a_refusal_is_said_plainly_and_the_backoff_holds_the_next_test(self):
        self.configure()
        fake = FakeTracker((403, b"no"))
        status, answer, _raw = self.ask(fake)
        self.assertEqual(status, 502)
        self.assertIn("Your tracker turned the request away (it answered 403).", answer["message"])
        self.assertIn("The session cookie is wrong, has expired, or was created for a different address",
                      answer["message"])
        self.assertIn("Audiobind now waits", answer["message"])
        _status, again, _raw = self.ask(fake)
        self.assertEqual(len(fake.calls), 1, "the backoff holds: the second Test sends nothing")
        self.assertIn("Nothing was sent", again["message"])
        self.assertFalse(again["sent"])

    def test_the_search_keeps_the_trackers_spacing(self):
        self.configure()
        fake = FakeTracker((200, results(2)))
        real = watcher.make_tracker
        waits = []

        def factory(route):
            client = real(route)
            client.transport = fake
            client.limiter.wait = lambda: waits.append(True)
            return client

        with mock.patch.object(watcher, "make_tracker", factory):
            self.post(wizard.API, {"action": "tracker_test", "query": "Dune"})
        self.assertEqual(waits, [True], "the request waited its turn on the watcher's own limiter")

    def test_missing_boxes_are_named_and_nothing_is_sent(self):
        self.save({"tracker_url": "https://tracker.example"})
        fake = FakeTracker()
        status, answer, _raw = self.ask(fake)
        self.assertEqual(status, 502)
        self.assertTrue(answer["message"].startswith("Fill in these first: Session cookie, Cookie name"),
                        answer["message"])
        self.assertEqual(fake.calls, [])

    def test_with_the_guard_off_the_test_keeps_the_address_step_three_found(self):
        self.configure()
        self.check_again([SEEN])
        with mock.patch.object(egress, "probe") as probe:
            self.ask(FakeTracker())
        probe.assert_not_called()
        self.assertEqual(self.state.get_meta(watcher.META_EGRESS)["ip"], SEEN)

    def test_the_wrong_address_sends_nothing(self):
        self.configure()
        self.save({"expected_egress": OTHER})
        fake = FakeTracker()
        with mock.patch.object(egress, "probe", return_value=SEEN):
            _status, answer, _raw = self.ask(fake)
        self.assertEqual(fake.calls, [])
        self.assertIn(f"Your tracker would see {SEEN}, but your session is for {OTHER}", answer["message"])

    def test_the_test_is_signed_in_only(self):
        req = urllib.request.Request(self.base + wizard.API, data=b'{"action":"tracker_test"}', method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": "nothing=here"})
        with mock.patch.object(watcher, "make_tracker") as make:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
        self.assertIn(caught.exception.code, (401, 403))
        caught.exception.close()
        make.assert_not_called()


class TestNoSiteName(Step4Case):
    def pages(self):
        for step in ("1", "2", "3", "4", "done"):
            yield step, self.get(f"{wizard.PATH}?step={step}")[1]

    def assert_unnamed(self, text, where):
        self.assertNotIn(SITE, text.lower(), where)
        self.assertNotRegex(text, rf"\b{SITE_SHORT}\b", where)

    def test_no_rendered_page_of_the_guide_names_a_tracker_site(self):
        self.configure()
        for step, page in self.pages():
            self.assert_unnamed(page, f"step {step}")

    def test_pointing_at_that_site_changes_no_copy(self):
        # the person's own address is theirs to see in its box; nothing the app writes may name the site
        self.configure(tracker_url=f"https://www.{SITE}.net")
        self.check_again([SEEN])
        for step, page in self.pages():
            own_box = re.sub(r'(id="ab-wiz-turl"[^>]*?)(data-initial|value)="[^"]*"', r"\1", page)
            own_box = re.sub(r'(id="ab-wiz-turl"[^>]*?)(data-initial|value)="[^"]*"', r"\1", own_box)
            self.assert_unnamed(own_box, f"step {step}")

    def test_the_module_and_the_script_name_no_tracker_site(self):
        with open(wizard.__file__, encoding="utf-8") as handle:
            self.assert_unnamed(handle.read(), "wizard.py")
        self.assert_unnamed(web.WIZARD_SCRIPT, "WIZARD_SCRIPT")
        self.assert_unnamed(web.WIZARD_SESSION_WHAT, "the session cookie explanation")

    def test_the_test_answers_name_no_tracker_site(self):
        self.configure(tracker_url=f"https://www.{SITE}.net")
        for status_code, body in ((200, results(2)), (403, b""), (200, b"<html>")):
            watcher.WATCHER._client = None
            _status, _answer, raw = self.ask(FakeTracker((status_code, body)))
            self.assert_unnamed(raw, f"answer to {status_code}")


class TestTheEnd(Step4Case):
    def complete(self):
        self.configure()
        self.save({"qbit_url": "http://192.168.1.10:8080", "qbit_user": "reader", "qbit_pass": "pw-123456"})
        self.mount_ingest()
        self.check_again([SEEN])

    def done(self):
        return self.get(wizard.PATH + "?step=done")[1]

    def test_nothing_set_up_yet_says_almost_done_and_names_what_is_missing(self):
        page = self.done()
        text = self.text(page)
        self.assertIn("Almost done", text)
        self.assertIn("Your tracker: still missing Your tracker's address, Session cookie", text)
        self.assertIn("Audiobind fetches nothing until every line marked as a problem is fixed.", text)
        self.assertIn(f'href="{wizard.PATH}?step=4"', page)
        self.assertNotIn("What happens now", text)

    def test_everything_set_up_says_done_and_what_happens_next(self):
        self.complete()
        page = self.done()
        text = self.text(page)
        self.assertIn("Done: downloading is set up", text)
        self.assertIn("Your tracker: the address, session cookie and search settings are saved.", text)
        self.assertIn("Download client: qBittorrent at http://192.168.1.10:8080, category audiobooks.", text)
        self.assertIn(f"Finished downloads are picked up from {self.ingest_dir}.", text)
        self.assertIn(f"Your tracker sees {SEEN}", text)
        self.assertRegex(text, r"Every \d+ minutes, Audiobind searches your tracker for the books on your "
                               r"wishlist")
        self.assertIn("hands the torrent to qBittorrent", text)
        self.assertIn("the converter picks the book up from the ingest folder", text)
        for href in ("/discover/wishlist", "/?status=all", "/settings?tab=watcher"):
            self.assertIn(f'href="{href}"', page)
        self.assertNotIn(COOKIE, page)
        self.assertNotIn("pw-123456", page)
        self.assertTrue(wizard.read(self.state)["dismissed"], "finished: the Library card does not come back")

    def test_a_mismatched_address_keeps_it_almost_done(self):
        self.complete()
        self.save({"expected_egress": OTHER})
        text = self.text(self.done())
        self.assertIn("Almost done", text)
        self.assertIn("your session was created for another address", text)

    def test_the_end_is_kept_across_a_restart(self):
        self.done()
        self.assertEqual(wizard.read(self.state)["step"], wizard.DONE)
        from audiobind.state import State

        self.state.close()
        self.state = State(self.root / "state")
        web.Handler.state = self.state
        self.assertIn('id="ab-wiz-summary"', self.get(wizard.PATH)[1])

    def test_the_end_needs_the_tracker_answer(self):
        self.state.set_meta(wizard.META_MODE, wizard.MODE_FILES)
        self.assertIn("Step 1 of 4", self.done())


class TestIndicator(Step4Case):
    @staticmethod
    def indicator(page):
        return re.search(r'<ol class="ab-wiz-steps">.*?</ol>', page, re.S).group(0)

    def test_in_the_tracker_flow_every_step_and_done_is_a_link(self):
        for shown, links in (("1", ("2", "3", "4", "done")), ("3", ("1", "2", "4", "done")),
                             ("done", ("1", "2", "3", "4"))):
            with self.subTest(shown=shown):
                bar = self.indicator(self.get(f"{wizard.PATH}?step={shown}")[1])
                self.assertEqual(bar.count("<a "), 4)
                for target in links:
                    self.assertIn(f'href="{wizard.PATH}?step={target}"', bar)
                self.assertEqual(bar.count('aria-current="step"'), 1)
                self.assertNotIn("aria-disabled", bar)

    def test_before_the_tracker_answer_no_later_step_is_a_link(self):
        self.state.set_meta(wizard.META_MODE, wizard.MODE_LIBRARY)
        bar = self.indicator(self.get(wizard.PATH)[1])
        self.assertEqual(bar.count("<a "), 0)
        self.assertEqual(bar.count('aria-disabled="true"'), 4)


class TestSignedOut(Step4Case):
    signed_in = False

    def test_steps_three_four_and_the_end_go_to_the_sign_in(self):
        seed_account(self.state)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        for step in ("3", "4", "done"):
            with self.subTest(step=step):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(f"{self.base}{wizard.PATH}?step={step}", timeout=10)
                self.assertEqual(caught.exception.code, 303)
                self.assertEqual(caught.exception.headers["Location"], "/login")
                caught.exception.close()


if __name__ == "__main__":
    import unittest

    unittest.main()

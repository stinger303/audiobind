"""Set up downloading, step 2: the download client. The pre-fill from the Host header, the save under the Watcher
tab's own keys, the plain-language test with its one retry on the loopback address, Back keeping the arrival
answer, and no secret ever coming back."""

import json
import re
import urllib.request
from unittest import mock

from audiobind import config, ingest, wizard
from audiobind import watcher, web
from audiobind.clients import base
from audiobind.config import CONFIG
from audiobind.qbit import QbitError

try:
    from test_wizard_step1 import Step1Case
except ImportError:
    from tests.test_wizard_step1 import Step1Case

LAN = "http://192.168.1.10:8080"
LOOP = "http://127.0.0.1:8080"
PASSWORD = "p@ss-WORD-9000"


def checked(categories=None, version="5.0.4", torrents=None):
    return {"version": version, "categories": categories, "categories_error": "", "torrents": torrents,
            "torrents_error": ""}


class FakeQbit:
    """Answers per address: an exception to raise, or the dict check() returns."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def factory(self, url=None):
        url = url if url is not None else watcher.qbit_url()
        self.asked.append(url)
        outcome = self.answers.get(url, QbitError(f"qBittorrent at {url} could not be reached (TimeoutError)",
                                                  kind=base.UNAVAILABLE))
        client = mock.Mock()
        if isinstance(outcome, Exception):
            client.check.side_effect = outcome
        else:
            client.check.return_value = outcome
        return client


class Step2Case(Step1Case):
    def setUp(self):
        super().setUp()
        for key in ("qbit_url", "qbit_user", "qbit_pass", "qbit_category", "download_client", "deluge_url"):
            patcher = mock.patch.object(CONFIG, key, "audiobooks" if key == "qbit_category" else "")
            patcher.start()
            self.addCleanup(patcher.stop)
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)
        w = watcher.WATCHER
        saved = (w._client, w._built, w._built_from, w.autostart)
        w._client, w._built, w._built_from, w.autostart = None, None, "", False
        self.addCleanup(lambda: setattr(w, "_client", saved[0]))
        self.addCleanup(lambda: setattr(w, "_built", saved[1]))
        self.addCleanup(lambda: setattr(w, "_built_from", saved[2]))
        self.addCleanup(lambda: setattr(w, "autostart", saved[3]))
        self.state.set_meta(wizard.META_MODE, wizard.MODE_TRACKER)

    def page2(self, host=None):
        return self.get(wizard.PATH + "?step=2", {"Host": host} if host else {})[1]

    def save(self, settings, clear=()):
        status, body = self.post("/api/settings", {"settings": settings, "clear": list(clear)})
        self.assertEqual(status, 200, body)

    def ask_test(self, fake):
        with mock.patch.object(watcher, "make_qbit", fake.factory):
            status, body = self.post(wizard.API, {"action": "test"})
        return status, json.loads(body), body

    @staticmethod
    def value_of(page, element_id):
        return re.search(rf'id="{element_id}" value="([^"]*)"', page).group(1)


class TestThePage(Step2Case):
    def test_the_boxes_and_their_defaults(self):
        page = self.page2()
        self.assertIn("Step 2 of 4: Download client", page)
        self.assertIn('<option value="qbittorrent" selected>qBittorrent</option>', page)
        self.assertEqual(self.value_of(page, "ab-wiz-category"), "audiobooks")
        self.assertIn('type="password" class="ab-in" id="ab-wiz-pass" value=""', page)
        for field in ("client", "url", "user", "pass", "category"):
            self.assertIn(f'<label class="ab-setting-label" for="ab-wiz-{field}">', page)
        for button in ("ab-wiz-test", "ab-wiz-continue", "ab-wiz-back"):
            self.assertIn(f'id="{button}"', page)
        self.assertIn('id="ab-wiz-test-fb" role="status"', page)

    def test_the_address_is_prefilled_from_the_host_the_browser_used(self):
        page = self.page2("192.168.1.10:8686")
        self.assertEqual(self.value_of(page, "ab-wiz-url"), "http://192.168.1.10:8080")
        self.assertIn("the address you are using to open Audiobind", page)

    def test_a_saved_address_wins_over_the_prefill(self):
        self.save({"qbit_url": "http://saved-box:9999"})
        page = self.page2("192.168.1.10:8686")
        self.assertEqual(self.value_of(page, "ab-wiz-url"), "http://saved-box:9999")
        self.assertNotIn("the address you are using to open Audiobind", page)

    def test_a_hostile_host_the_server_let_through_still_gives_an_empty_box(self):
        hostile = 'evil.example/"><script>alert(1)</script>'
        with mock.patch.object(CONFIG, "allowed_hosts", [hostile]), mock.patch.object(CONFIG, "allowed_hosts_set", True):
            page = self.page2(hostile)
        self.assertEqual(self.value_of(page, "ab-wiz-url"), "")
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn("evil.example", page)

    def test_a_missing_or_refused_host_gives_an_empty_box(self):
        state = self.state
        for host in ("", None, "evil.example"):
            with self.subTest(host=host):
                page = web.render_wizard(state, "2", host)
                self.assertEqual(self.value_of(page, "ab-wiz-url"), "")
        status, _page = self.get(wizard.PATH + "?step=2", {"Host": "evil.example"})
        self.assertEqual(status, 403, "the server's own Host check refuses it before the page is drawn")

    def test_the_client_select_lists_every_client_and_keeps_the_others_boxes_in_data(self):
        self.save({"download_client": "deluge", "deluge_url": "http://deluge-box:8112"})
        page = self.page2()
        self.assertIn('<option value="deluge" selected>Deluge</option>', page)
        self.assertEqual(self.value_of(page, "ab-wiz-url"), "http://deluge-box:8112")
        self.assertIn('id="ab-wiz-user-wrap" hidden', page, "Deluge has no user name")
        info = json.loads(re.search(r'data-clients="([^"]*)"', page).group(1).replace("&quot;", '"'))
        self.assertEqual(set(info), {"qbittorrent", "deluge", "transmission", "rtorrent"})
        self.assertEqual(info["deluge"]["keys"], {"url": "deluge_url", "user": None, "pass": "deluge_pass"})


class TestBack(Step2Case):
    def test_back_returns_to_step_one_with_the_arrival_choice_kept(self):
        self.mount_ingest(1)
        self.answer("tracker")
        page2 = self.get(wizard.PATH + "?step=2")[1]
        back = re.search(r'id="ab-wiz-back" href="([^"]+)"', page2).group(1)
        self.assertEqual(back, wizard.PATH + "?step=1")
        page1 = self.get(back)[1]
        self.assertIn('value="tracker" checked', page1)
        self.assertIn("Step 1 of 4", page1)
        self.assertIn('id="ab-wiz-next"', page1, "and the way forward is still there")
        self.assertEqual(wizard.read(self.state), {"mode": "tracker", "step": 1, "dismissed": False})

    def test_progress_at_step_two_survives_a_reload(self):
        self.page2()
        self.assertEqual(wizard.read(self.state)["step"], 2)
        self.assertIn("Step 2 of 4", self.get(wizard.PATH)[1])


class TestSavingAndTheSharedStore(Step2Case):
    def test_what_the_wizard_saves_is_the_watcher_tabs_own_setting_with_its_reset(self):
        self.save({"download_client": "qbittorrent", "qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD,
                   "qbit_category": "books"})
        for key, value in (("qbit_url", LAN), ("qbit_user", "reader"), ("qbit_category", "books")):
            self.assertEqual(self.state.get_meta(f"setting_{key}"), value)
        tab = web.render_settings(self.state, "watcher")
        self.assertEqual(watcher.qbit_url(), LAN)
        field = re.search(r'<div class="ab-setting" data-setting="qbit_url".*?</div>', tab, re.S).group(0)
        self.assertIn("set here", field)
        self.assertIn("Reset to container value", field)
        status, body = self.post("/api/settings", {"reset_keys": ["qbit_url", "qbit_user", "qbit_category"]})
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.state.get_meta("setting_qbit_url"))
        self.assertEqual(watcher.qbit_url(), "")

    def test_the_password_is_never_echoed_by_the_page_or_the_test(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        self.assertNotIn(PASSWORD, self.page2())
        self.assertNotIn(PASSWORD, web.render_settings(self.state, "watcher"))
        page = self.page2()
        self.assertIn('placeholder="set - leave blank to keep it"', page)
        self.assertIn('id="ab-wiz-clear-wrap">', page, "a saved password can be cleared")
        _status, _answer, raw = self.ask_test(FakeQbit({LAN: checked({"audiobooks": "/data/audiobooks"})}))
        self.assertNotIn(PASSWORD, raw)
        failing = FakeQbit({LAN: QbitError("qBittorrent refused the user name or password", kind=base.AUTH)})
        self.assertNotIn(PASSWORD, self.ask_test(failing)[2])

    def test_a_blank_password_keeps_the_saved_one_and_clear_removes_it(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        self.save({"qbit_url": LAN, "qbit_user": "reader"})  # what the page sends when the box is empty
        self.assertEqual(watcher._setting("qbit_pass"), PASSWORD)
        self.save({"qbit_url": LAN}, clear=["qbit_pass"])
        self.assertEqual(watcher._setting("qbit_pass"), "")

    def test_a_bad_value_is_refused_by_the_tabs_own_checks(self):
        status, body = self.post("/api/settings", {"settings": {"qbit_url": "ftp://nope"}})
        self.assertEqual(status, 400, body)
        self.assertIsNone(self.state.get_meta("setting_qbit_url"))

    def test_the_wizard_script_saves_through_the_tabs_endpoint_and_keys(self):
        script = web.WIZARD_SCRIPT
        self.assertIn("fetch('/api/settings'", script)
        self.assertIn("settings[keys.url]", script)
        self.assertIn("clear: clear", script)
        self.assertNotIn("setting_", script, "it never touches the store directly")


class TestThePlainLanguageTest(Step2Case):
    def test_signed_in_with_the_category_and_the_ingest_folder_said_plainly(self):
        self.mount_ingest(0)
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({LAN: checked({"audiobooks": "/data/audiobooks"})})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual(status, 200)
        self.assertEqual(answer["message"], "Signed in to qBittorrent 5.0.4")
        self.assertTrue(answer["ok"])
        self.assertEqual((answer["answered"], answer["retried"], answer["saved_url"]), (LAN, False, ""))
        self.assertIn("Category 'audiobooks' exists; qBittorrent saves it to /data/audiobooks.", answer["details"])
        self.assertIn(f"Audiobind reads its ingest folder: {self.ingest_dir}.", answer["details"])
        self.assertIn(web.ONE_FOLDER_THREE_NAMES, answer["details"])
        self.assertEqual(fake.asked, [LAN])

    def test_a_missing_category_says_where_to_create_it(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        status, answer, _raw = self.ask_test(FakeQbit({LAN: checked({"movies": "/data/movies"})}))
        self.assertEqual(answer["chain"], "problem")
        self.assertTrue(any("Categories" in line and "Add category" in line for line in answer["details"]))

    def test_a_timeout_retries_on_the_loopback_and_says_which_one_answered(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({LOOP: checked({"audiobooks": "/data/audiobooks"})})  # the LAN address times out
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual(status, 200)
        self.assertEqual(fake.asked, [LAN, LOOP], "one try each, in that order")
        self.assertTrue(answer["ok"] and answer["retried"])
        self.assertEqual(answer["answered"], LOOP)
        self.assertIn(f"did not answer at {LAN}, but it did at {LOOP}", answer["message"])
        self.assertIn("Signed in to qBittorrent 5.0.4", answer["message"])
        self.assertEqual(answer["saved_url"], LOOP)
        self.assertEqual(self.state.get_meta("setting_qbit_url"), LOOP, "the address that works is the one kept")
        self.assertEqual(watcher.qbit_url(), LOOP)
        self.assertIn("Category 'audiobooks' exists", " ".join(answer["details"]))

    def test_when_neither_answers_both_are_named_and_nothing_is_changed(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual(status, 502)
        self.assertFalse(answer["ok"])
        self.assertEqual(fake.asked, [LAN, LOOP])
        self.assertIn(f"Nothing answered at {LAN}, and nothing answered at {LOOP} either.", answer["message"])
        self.assertEqual(len(answer["details"]), 2)
        self.assertEqual(self.state.get_meta("setting_qbit_url"), LAN)
        self.assertEqual(answer["saved_url"], "")

    def test_a_loopback_that_answers_with_a_wrong_password_is_still_the_way_in(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({LOOP: QbitError("qBittorrent refused the user name or password", kind=base.AUTH)})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual(status, 502)
        self.assertIn("refused the user name or password", answer["message"])
        self.assertIn(f"but it did at {LOOP}", answer["message"])
        self.assertEqual(self.state.get_meta("setting_qbit_url"), LOOP)

    def test_a_refused_password_at_the_first_address_is_not_retried(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({LAN: QbitError("qBittorrent refused the user name or password", kind=base.AUTH)})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual((status, fake.asked, answer["retried"]), (502, [LAN], False))
        self.assertEqual(answer["message"], "qBittorrent refused the user name or password")

    def test_an_address_that_is_already_the_loopback_is_not_retried(self):
        self.save({"qbit_url": LOOP, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual((status, fake.asked, answer["retried"]), (502, [LOOP], False))

    def test_another_client_is_tested_as_the_tab_tests_it_with_no_retry(self):
        self.save({"download_client": "deluge", "deluge_url": "http://deluge-box:8112", "deluge_pass": PASSWORD})
        deluge = mock.Mock()
        deluge.version.return_value = "2.1.1"
        with mock.patch.object(watcher, "make_client", return_value=deluge):
            status, body = self.post(wizard.API, {"action": "test"})
        answer = json.loads(body)
        self.assertEqual((status, answer["message"]), (200, "Signed in to Deluge 2.1.1"))
        failing = mock.Mock()
        failing.version.side_effect = base.ClientError("Deluge could not be reached", kind=base.UNAVAILABLE)
        with mock.patch.object(watcher, "make_client", return_value=failing):
            status, body = self.post(wizard.API, {"action": "test"})
        self.assertEqual((status, json.loads(body)["retried"]), (502, False))

    def test_no_address_yet_is_said_plainly_and_nothing_is_called(self):
        fake = FakeQbit({})
        status, answer, _raw = self.ask_test(fake)
        self.assertEqual(status, 502)
        self.assertIn("There is no address yet.", answer["message"])
        self.assertEqual(fake.asked, [])

    def test_the_test_only_reads(self):
        self.save({"qbit_url": LAN, "qbit_user": "reader", "qbit_pass": PASSWORD})
        fake = FakeQbit({LAN: checked({"audiobooks": "/data/audiobooks"})})
        self.ask_test(fake)
        for method in ("add_torrent", "add", "delete", "set_category"):
            self.assertFalse(getattr(fake.factory(LAN), method).called, method)

    def test_the_test_is_post_only_signed_in_and_origin_checked(self):
        self.assertEqual(self.get(wizard.API)[0], 405)
        req = urllib.request.Request(self.base + wizard.API, data=b'{"action":"test"}', method="POST",
                                     headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class TestContinue(Step2Case):
    def test_continue_leads_to_step_three(self):
        page = self.get(wizard.PATH + "?step=3")[1]
        self.assertIn("Step 3 of 4: The address the tracker sees", page)
        self.assertNotIn("is not built yet", page)
        self.assertIn(f'href="{wizard.PATH}?step=2"', page, "Back goes to the download client")
        self.assertIn("abWizardClientContinue()", self.page2())
        self.assertIn("'/setup/downloading?step=3'", web.WIZARD_SCRIPT)


if __name__ == "__main__":
    import unittest

    unittest.main()

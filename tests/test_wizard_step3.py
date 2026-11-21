"""Set up downloading, step 3: the address the tracker sees. The page shows the last egress check on record
and sends nothing; Check again runs the watcher's own probe once (stubbed here: no network), stores it
where the watcher stores it, and says in plain words whether the address is the one the session was made
for."""

import html
import json
import re
from unittest import mock

from audiobind import config, egress, watcher, web, wizard
from audiobind.config import CONFIG

try:
    from test_wizard_step2 import Step2Case
except ImportError:
    from tests.test_wizard_step2 import Step2Case

SEEN = "198.51.100.7"
OTHER = "203.0.113.9"
HOME = "192.0.2.44"


class Step3Case(Step2Case):
    def setUp(self):
        super().setUp()
        for key in ("tracker_proxy", "tracker_home_ip", "tracker_expected_egress_ip"):
            patcher = mock.patch.object(CONFIG, key, "")
            patcher.start()
            self.addCleanup(patcher.stop)
        config._EFFECTIVE.clear()
        # no test in this file may reach the network: the one network call of the probe fails loudly
        guard = mock.patch.object(egress, "_fetch", side_effect=AssertionError("network call in a test"))
        guard.start()
        self.addCleanup(guard.stop)

    def page3(self):
        return self.get(wizard.PATH + "?step=3")[1]

    def check_again(self, answers):
        """POST Check again with egress.probe answering ``answers`` in turn (an exception = no address)."""
        with mock.patch.object(egress, "probe", side_effect=answers) as probe:
            status, body = self.post(wizard.API, {"action": "address"})
        return status, json.loads(body), probe

    def expect(self, ip):
        self.save({"expected_egress": ip})

    @staticmethod
    def text(page):
        box = re.search(r'<div class="ab-hintbox ab-wiz-addr".*?</div>', page, re.S).group(0)
        return " ".join(html.unescape(re.sub(r"<[^>]+>", "", box.replace("</li>", " "))).split())


class TestTheStep(Step3Case):
    def test_step_three_is_built_and_no_longer_says_not_built(self):
        page = self.page3()
        self.assertIn("Step 3 of 4: The address the tracker sees", page)
        self.assertNotIn("is not built yet", page)
        self.assertIn(3, wizard.BUILT)

    def test_showing_the_page_sends_nothing_and_asks_the_page_to_check_once(self):
        with mock.patch.object(egress, "probe") as probe:
            page = self.page3()
        probe.assert_not_called()
        self.assertIn('id="ab-wiz-addr" data-auto="1"', page)
        self.assertIn("Not checked yet.", self.text(page))
        self.assertIn("box.dataset.auto === '1'", web.WIZARD_SCRIPT)

    def test_the_probed_address_is_shown_with_when_it_was_checked(self):
        status, answer, probe = self.check_again([SEEN])
        self.assertEqual(status, 200)
        probe.assert_called_once_with("")
        page = self.page3()
        self.assertIn(SEEN, self.text(page))
        self.assertIn("leave from 198.51.100.7, straight from this container, with no proxy", self.text(page))
        self.assertRegex(self.text(page), r"Last checked: \d{4}-\d\d-\d\d \d\d:\d\d\.")
        self.assertNotIn("data-auto", page, "a check on record: the page does not check again by itself")

    def test_the_probe_goes_through_the_tracker_proxy_as_the_watcher_does(self):
        with mock.patch.object(CONFIG, "tracker_proxy", "http://vpn:8888"):
            config._EFFECTIVE.clear()
            _status, _answer, probe = self.check_again([SEEN])
            probe.assert_called_once_with("http://vpn:8888")
            self.assertIn("through your tracker proxy", self.text(self.page3()))

    def test_the_check_is_stored_where_the_watcher_stores_its_own(self):
        self.check_again([SEEN])
        rec = self.state.get_meta(watcher.META_EGRESS)
        self.assertEqual(rec["ip"], SEEN)
        self.assertEqual(rec["reason"], "")
        self.assertIsInstance(rec["at"], float)

    def test_no_session_address_given_says_it_cannot_compare(self):
        self.check_again([SEEN])
        text = self.text(self.page3())
        self.assertIn("You have not told Audiobind which address your tracker session was created for", text)
        self.assertIn("Create your session from 198.51.100.7", text)

    def test_a_match_is_said_plainly(self):
        self.expect(SEEN)
        _status, answer, _probe = self.check_again([SEEN])
        self.assertEqual(answer["verdict"], "match")
        self.assertIn("It matches your session's address.", answer["message"])
        self.assertIn("so the two match", self.text(self.page3()))

    def test_a_mismatch_names_the_consequence(self):
        self.expect(OTHER)
        _status, answer, _probe = self.check_again([SEEN])
        self.assertEqual(answer["verdict"], "mismatch")
        text = self.text(self.page3())
        self.assertIn(f"Your tracker session was created for {OTHER}, but your requests leave from {SEEN}.",
                      text)
        self.assertIn(f"The session will be refused; create a new one from this address ({SEEN})", text)

    def test_the_home_connection_is_named_as_such(self):
        with mock.patch.object(CONFIG, "tracker_home_ip", HOME):
            config._EFFECTIVE.clear()
            _status, answer, _probe = self.check_again([HOME])
            self.assertEqual(answer["verdict"], "home")
            self.assertIn("That is your home connection", self.text(self.page3()))

    def test_continue_and_back_are_always_there(self):
        page = self.page3()
        self.assertIn(f'id="ab-wiz-back" href="{wizard.PATH}?step=2"', page)
        self.assertIn(f'id="ab-wiz-continue" href="{wizard.PATH}?step=4"', page)


class TestCheckAgain(Step3Case):
    def test_check_again_probes_again_every_time(self):
        _s, first, probe1 = self.check_again([SEEN])
        _s, second, probe2 = self.check_again([OTHER])
        self.assertEqual(probe1.call_count, 1)
        self.assertEqual(probe2.call_count, 1)
        self.assertEqual((first["ip"], second["ip"]), (SEEN, OTHER))
        self.assertEqual(self.state.get_meta(watcher.META_EGRESS)["ip"], OTHER)
        self.assertIn(OTHER, self.text(self.page3()))
        self.assertIn(f"Checked just now: your requests leave from {OTHER}.", second["message"])
        self.assertIn('id="ab-wiz-addr"', second["html"], "the answer redraws the finding")

    def test_a_failed_probe_is_said_plainly_and_continue_still_works(self):
        failure = egress.EgressError("ipinfo.io: TimeoutError; api.ipify.org: URLError")
        status, answer, _probe = self.check_again([failure])
        self.assertEqual(status, 502)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["message"], "Checked just now: the address could not be found.")
        page = self.page3()
        text = self.text(page)
        self.assertIn("Audiobind could not find out its address", text)
        self.assertIn("You can still go on", text)
        self.assertNotIn("TimeoutError", page, "no exception names on the page")
        self.assertIn(f'id="ab-wiz-continue" href="{wizard.PATH}?step=4"', page)

    def test_a_failed_probe_with_a_session_address_says_the_tracker_is_left_alone(self):
        self.expect(OTHER)
        self.check_again([egress.EgressError("no answer")])
        self.assertIn("Until Audiobind can see its own address, it does not contact your tracker.",
                      self.text(self.page3()))

    def test_check_again_never_contacts_the_tracker(self):
        with mock.patch.object(watcher, "make_tracker") as make:
            self.check_again([SEEN])
        make.assert_not_called()

    def test_check_again_is_post_only_and_signed_in(self):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.base + wizard.API, data=b'{"action":"address"}', method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": "nothing=here"})
        with mock.patch.object(egress, "probe") as probe:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)  # a cookie of its own: the session is not added
        self.assertIn(caught.exception.code, (401, 403))
        caught.exception.close()
        probe.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()

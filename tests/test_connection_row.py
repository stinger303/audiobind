"""AB-AUTH-3: the Watcher status Connection row and setup step 3 read the stored outcome of real tracker
requests - never a new request, never the egress result.

No test here touches the network: the tracker client is built with a fake transport, and the outcome is
produced by the watcher's own probe going through it."""

import contextlib
import html as html_lib
import json
import re
import time
import unittest
from unittest import mock

from audiobind import egress, watcher, web, wizard
from audiobind.config import CONFIG

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase
from tests.tracker_cfg import make_client, tracker_cfg

EGRESS_IP = "203.0.113.77"
SIGNED_IN = "signed in"
REJECTED = "session rejected - paste a fresh cookie in Settings > Watcher"
NOT_CHECKED = "not checked yet - no tracker request has succeeded since startup"

OK_BODY = json.dumps({"data": [{"id": 1, "title": "Dune", "author_info": json.dumps({"1": "Frank Herbert"}),
                                "filetype": "m4b"}]}).encode()


class Net:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def connection_cell(html: str) -> str:
    match = re.search(r"<dt>Connection</dt><dd>(.*?)</dd>", html, re.S)
    assert match, "no Connection row"
    return html_lib.unescape(match.group(1))


class ConnectionRowCase(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.cfg = tracker_cfg("some-saved-cookie-value-123456")
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        super().tearDown()

    def request_through(self, answer):
        """One real probe through the watcher's client on a fake transport (the outcome gets recorded)."""
        net = Net(answer)
        client = make_client(transport=net)
        with mock.patch.object(watcher.WATCHER, "_client", client):
            watcher.probe_tracker(self.state, "Dune")
        self.assertEqual(len(net.calls), 1)  # one request, nothing extra
        return net

    def row(self) -> str:
        return connection_cell(web.render_watcher_section(self.state))


class Outcomes(ConnectionRowCase):
    def test_a_successful_request_renders_signed_in(self):
        self.request_through((200, {}, OK_BODY))
        self.assertEqual(self.row(), SIGNED_IN)

    def test_a_rejection_renders_the_warning_and_names_settings(self):
        self.request_through((403, {}, b"Error, you are not signed in"))
        cell = self.row()
        self.assertIn(REJECTED, cell)
        self.assertIn("Settings > Watcher", cell)
        self.assertIn('class="warn"', cell)

    def test_no_outcome_renders_not_checked_yet(self):
        self.assertEqual(self.row(), NOT_CHECKED)

    def test_a_5xx_renders_not_checked_yet_not_a_rejection(self):
        self.request_through((503, {}, b"maintenance"))
        cell = self.row()
        self.assertEqual(cell, NOT_CHECKED)
        self.assertNotIn("rejected", cell)

    def test_a_timeout_after_a_success_goes_back_to_not_checked(self):
        self.request_through((200, {}, OK_BODY))
        self.request_through(TimeoutError("timed out"))
        self.assertEqual(self.row(), NOT_CHECKED)

    def test_a_request_that_was_never_sent_records_nothing(self):
        client = make_client(transport=Net(AssertionError("must not send")))
        client.backoff.fail_fixed(180)
        with mock.patch.object(watcher.WATCHER, "_client", client):
            watcher.probe_tracker(self.state, "Dune")
        self.assertIsNone(self.state.get_meta(watcher.META_SESSION))
        self.assertEqual(self.row(), NOT_CHECKED)


class NeverTheEgressResult(ConnectionRowCase):
    def armed(self):
        """The guard armed for EGRESS_IP with the address lookup faked: nothing leaves the test."""
        self.state.set_meta(watcher.META_EGRESS, {"ip": EGRESS_IP, "at": time.time(), "reason": ""})
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.multiple(CONFIG, tracker_expected_egress_ip=EGRESS_IP))
        stack.enter_context(mock.patch.object(egress, "probe", return_value=EGRESS_IP))
        return stack

    def test_the_row_text_never_contains_the_egress_ip(self):
        with self.armed():
            for answer in ((200, {}, OK_BODY), (403, {}, b"Error, you are not signed in"), (503, {}, b"")):
                self.request_through(answer)
                html = web.render_watcher_section(self.state)
                self.assertIn(EGRESS_IP, html)  # the egress row exists and carries it, on its own row
                self.assertIn("<dt>Address seen</dt>", html)
                self.assertNotIn(EGRESS_IP, connection_cell(html))

    def test_a_good_egress_check_alone_does_not_say_signed_in(self):
        with self.armed():
            self.assertEqual(self.row(), NOT_CHECKED)


class SaveLeavesNoStaleVerdict(ConnectionRowCase):
    """AB-AUTH-7: a save or reset on a tracker setting turns a stored rejection into unknown (never
    signed in); a save elsewhere leaves it; a later real success still reads signed in."""

    def reject(self):
        self.request_through((403, {}, b"Error, you are not signed in"))
        self.assertIn(REJECTED, self.row())  # the stale verdict is really there first

    def save(self, settings):
        status, body = self.request("/api/settings", {"settings": settings})
        self.assertEqual(status, 200, body)

    def test_a_tracker_settings_save_reads_not_checked_yet(self):
        self.reject()
        self.save({"tracker_session": "fresh-cookie-value-0123456789"})
        self.assertEqual(self.row(), NOT_CHECKED)
        self.assertIsNone(self.state.get_meta(watcher.META_SESSION))

    def test_reset_to_container_value_on_a_tracker_key_reads_not_checked_yet(self):
        self.save({"tracker_session": "override-cookie-value-0123456789"})
        self.reject()
        status, body = self.request("/api/settings", {"reset_keys": ["tracker_session"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.row(), NOT_CHECKED)

    def test_a_save_that_does_not_touch_the_tracker_keeps_the_rejection(self):
        self.reject()
        self.save({"watch_interval": 60})
        self.assertIn(REJECTED, self.row())

    def test_a_real_success_after_the_save_flips_to_signed_in(self):
        self.reject()
        self.save({"tracker_session": "fresh-cookie-value-0123456789"})
        self.request_through((200, {}, OK_BODY))
        self.assertEqual(self.row(), SIGNED_IN)


class SetupStepThree(ConnectionRowCase):
    def test_the_line_follows_the_stored_outcome_and_points_at_the_test(self):
        self.assertIn("not checked yet", wizard.session_line(self.state))
        self.assertIn("The Test in the next step is what checks it", wizard.session_line(self.state))
        self.request_through((403, {}, b"Error, you are not signed in"))
        self.assertIn("rejected", wizard.session_line(self.state))
        self.request_through((200, {}, OK_BODY))
        self.assertIn("succeeded", wizard.session_line(self.state))

    def test_the_line_is_on_the_page_and_sends_nothing(self):
        with mock.patch.object(watcher, "make_tracker", side_effect=AssertionError("no request")):
            page = web._wizard_step3(self.state)
        self.assertIn('id="ab-wiz-session"', page)
        self.assertIn("Session: not checked yet.", page)


if __name__ == "__main__":
    unittest.main()

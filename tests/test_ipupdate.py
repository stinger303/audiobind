"""Parity feature 3: the allowed-IP updater (IP_UPDATER, ipupdate.decide, Use the current IP).

Nothing here touches the network: the egress probe is a fake, the tracker transport records calls.
There is no tracker write to test. No allowed-IP endpoint could be verified (docs/ip-updater.md),
so these tests check that the updater never sends a tracker request at all.
"""

import json
import logging
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from audiobind import config, egress, ipupdate, watcher, web
from audiobind.config import CONFIG
from audiobind.ipupdate import NOOP, REFUSE, UPDATE, decide
from audiobind.state import State
from tests.tracker_cfg import tracker_cfg

try:
    from test_watcher import NOTHING, WatcherCase
except ImportError:
    from tests.test_watcher import NOTHING, WatcherCase

HOME = "203.0.113.7"
PINNED = "198.51.100.7"
NEW = "198.51.100.99"
NOW = 1_800_000_000.0


class TestDecide(unittest.TestCase):
    """The truth table, one row per rule, in decide()'s own order."""

    def test_probe_failure_refuses_even_with_an_address(self):
        outcome, why = decide(NEW, PINNED, HOME, None, NOW, probe_error="ipinfo.io: URLError")
        self.assertEqual(outcome, REFUSE)
        self.assertIn("could not be checked", why)
        self.assertIn("URLError", why)

    def test_empty_or_blank_observed_refuses(self):
        for observed in ("", "   ", None):
            self.assertEqual(decide(observed, PINNED, HOME, None, NOW)[0], REFUSE, repr(observed))

    def test_junk_observed_refuses(self):
        outcome, why = decide("not-an-ip", PINNED, "", None, NOW)
        self.assertEqual(outcome, REFUSE)
        self.assertIn("not an IP address", why)

    def test_home_ip_refuses_and_wins_over_everything_after_it(self):
        outcome, why = decide(HOME, PINNED, HOME, None, NOW)
        self.assertEqual(outcome, REFUSE)
        self.assertIn("home connection", why)
        # even when the home IP is also the pin, it is never "already allowed"
        self.assertEqual(decide(HOME, HOME, HOME, None, NOW)[0], REFUSE)

    def test_home_ip_compared_as_addresses_not_text(self):
        self.assertEqual(decide("2001:db8::1", PINNED, "2001:0db8:0:0::1", None, NOW)[0], REFUSE)

    def test_already_the_allowed_ip_is_noop(self):
        outcome, why = decide(PINNED, PINNED, HOME, None, NOW)
        self.assertEqual(outcome, NOOP)
        self.assertIn("already the allowed IP", why)
        self.assertEqual(decide(" 2001:db8::5 ", "2001:0db8::5", "", None, NOW)[0], NOOP)

    def test_nothing_pinned_is_noop(self):
        outcome, why = decide(NEW, "", HOME, None, NOW)
        self.assertEqual(outcome, NOOP)
        self.assertIn("no allowed IP is pinned", why)

    def test_under_an_hour_refuses(self):
        outcome, why = decide(NEW, PINNED, HOME, NOW - 3599, NOW)
        self.assertEqual(outcome, REFUSE)
        self.assertIn("at most one change an hour", why)

    def test_future_last_update_refuses(self):
        outcome, why = decide(NEW, PINNED, HOME, NOW + 60, NOW)
        self.assertEqual(outcome, REFUSE)
        self.assertIn("future", why)

    def test_an_hour_or_more_updates(self):
        for last in (None, 0, NOW - 3600, NOW - 86400):
            outcome, why = decide(NEW, PINNED, HOME, last, NOW)
            self.assertEqual(outcome, UPDATE, last)
            self.assertIn(f"from {PINNED} to {NEW}", why)

    def test_no_home_ip_configured_still_updates(self):
        self.assertEqual(decide(NEW, PINNED, "", None, NOW)[0], UPDATE)

    def test_mode_normalises_and_defaults_to_off(self):
        self.assertEqual([ipupdate.mode(v) for v in ("", "OFF", " Notify ", "auto", "yes")],
                         ["off", "off", "notify", "auto", "off"])


class TestConfig(unittest.TestCase):
    def test_default_is_off_and_values_are_checked_at_load(self):
        with mock.patch.dict("os.environ", {"IP_UPDATER": ""}):
            self.assertEqual(config.Config.from_env().ip_updater, "off")
        for value in ("notify", "AUTO", " off "):
            with mock.patch.dict("os.environ", {"IP_UPDATER": value}):
                self.assertEqual(config.Config.from_env().ip_updater, value.strip().lower())
        with mock.patch.dict("os.environ", {"IP_UPDATER": "on"}), self.assertRaises(RuntimeError) as ctx:
            config.Config.from_env()
        self.assertIn("IP_UPDATER", str(ctx.exception))


class UpdaterCase(WatcherCase):
    MODE = "notify"

    def setUp(self):
        super().setUp()
        saved = dict(config._EFFECTIVE)
        self.addCleanup(lambda: (config._EFFECTIVE.clear(), config._EFFECTIVE.update(saved)))
        for p in (mock.patch.object(CONFIG, "tracker_home_ip", HOME),
                  mock.patch.object(CONFIG, "tracker_expected_egress_ip", PINNED),
                  mock.patch.object(CONFIG, "tracker_proxy", "http://vpn:8888"),
                  mock.patch.object(CONFIG, "ip_updater", self.MODE)):
            p.start()
            self.patches.append(p)
        self.fake_tracker(NOTHING)

    def probing(self, observed):
        def fake(proxy, timeout=10.0):
            if isinstance(observed, Exception):
                raise observed
            return observed
        return mock.patch.object(egress, "probe", fake)

    def run_cycle(self, observed):
        with self.probing(observed):
            return self.cycle()


class TestOffIsUnchanged(UpdaterCase):
    MODE = "off"

    def test_off_adds_no_log_line_no_status_key_no_page_rows_and_no_action(self):
        with self.assertLogs("audiobind", level=logging.DEBUG) as logs:
            summary = self.run_cycle(NEW)
        self.assertEqual(summary["egress_guard"], "blocked")
        self.assertFalse(any("allowed-IP updater" in line for line in logs.output), logs.output)
        self.assertIsNone(self.state.get_meta(watcher.META_IPUPDATE))
        status = self.w.status(self.state)
        self.assertNotIn("ip_updater", status)
        self.assertEqual(web._ip_updater_rows(status), [])
        with self.probing(NEW):
            ok, why = watcher.use_current_ip(self.state)
        self.assertFalse(ok)
        self.assertIn("IP_UPDATER=off", why)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)

    def test_off_never_calls_the_updater(self):
        with mock.patch.object(watcher.Watcher, "_ip_notice", side_effect=AssertionError("not with off")):
            self.run_cycle(NEW)


class TestNotify(UpdaterCase):
    def test_drift_is_logged_once_per_new_ip_and_the_guard_still_refuses(self):
        with self.assertLogs("audiobind", level=logging.WARNING) as logs:
            first = self.run_cycle(NEW)
            self.run_cycle(NEW)
        notices = [line for line in logs.output if "allowed-IP updater" in line]
        self.assertEqual(len(notices), 1, logs.output)
        self.assertIn(NEW, notices[0])
        self.assertIn("Use the current IP", notices[0])
        self.assertEqual(first["egress_guard"], "blocked", "the guard's verdict is untouched")
        self.assertEqual(self.transport.calls, [], "no tracker request, ever")

    def test_back_in_step_then_a_new_drift_is_reported_again(self):
        self.run_cycle(NEW)
        self.run_cycle(PINNED)
        self.assertEqual(self.state.get_meta(watcher.META_IPUPDATE)["notified_ip"], "")
        with self.assertLogs("audiobind", level=logging.WARNING) as logs:
            self.run_cycle(NEW)
        self.assertTrue(any("allowed-IP updater" in line for line in logs.output))

    def test_home_ip_and_probe_failure_are_never_offered(self):
        for observed in (HOME, egress.EgressError("ipinfo.io: URLError")):
            self.run_cycle(observed)
            self.assertIsNone(self.state.get_meta(watcher.META_IPUPDATE))
            status = self.w.status(self.state)["ip_updater"]
            self.assertEqual(status["decision"], REFUSE, observed)

    def test_a_failing_updater_does_not_break_the_cycle(self):
        self.want()
        with mock.patch.object(watcher.ipupdate, "decide", side_effect=RuntimeError("boom")), \
                self.assertLogs("audiobind", level=logging.ERROR) as logs:
            summary = self.run_cycle(PINNED)
        self.assertEqual(summary["egress_guard"], "ok")
        self.assertEqual(len(self.transport.calls), 1, "the search still ran")
        self.assertTrue(any("drift check failed" in line for line in logs.output))

    def test_status_carries_the_updater(self):
        self.run_cycle(NEW)
        upd = self.w.status(self.state)["ip_updater"]
        self.assertEqual((upd["mode"], upd["can_write"], upd["decision"]), ("notify", False, UPDATE))
        self.assertIsNone(upd["last_updated_at"])
        rows = dict(web._ip_updater_rows(self.w.status(self.state)))
        self.assertIn("IP_UPDATER=notify", rows["Allowed-IP updater"])
        self.assertIn("drifted", rows["Allowed IP"])
        self.assertIn("Use the current IP", rows["Last updated"])


class TestUseCurrentIp(UpdaterCase):
    def test_updates_the_pin_records_it_and_logs_old_to_new(self):
        with self.probing(NEW), self.assertLogs("audiobind", level=logging.INFO) as logs:
            ok, message = watcher.use_current_ip(self.state)
        self.assertTrue(ok, message)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), NEW)
        rec = self.state.get_meta(watcher.META_IPUPDATE)
        self.assertEqual((rec["old"], rec["new"]), (PINNED, NEW))
        self.assertTrue(any(f"{PINNED} -> {NEW}" in line for line in logs.output), logs.output)
        self.assertEqual(self.transport.calls, [], "nothing is sent to the tracker")
        summary = self.run_cycle(NEW)
        self.assertEqual(summary["egress_guard"], "ok", "the next cycle checks against the new pin")

    def test_at_most_once_an_hour(self):
        with self.probing(NEW):
            self.assertTrue(watcher.use_current_ip(self.state)[0])
        with self.probing("198.51.100.200"):
            ok, why = watcher.use_current_ip(self.state)
        self.assertFalse(ok)
        self.assertIn("one change an hour", why)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), NEW)

    def test_refuses_home_ip_probe_failure_and_no_drift(self):
        for observed, words in ((HOME, "home connection"), (OSError("down"), "could not be checked"),
                                (PINNED, "already the allowed IP")):
            with self.probing(observed):
                ok, why = watcher.use_current_ip(self.state)
            self.assertFalse(ok, observed)
            self.assertIn(words, why)
            self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        self.assertIsNone(self.state.get_meta(watcher.META_IPUPDATE))


class TestAutoActsAsNotify(UpdaterCase):
    MODE = "auto"

    def test_auto_changes_nothing_and_says_why(self):
        with self.assertLogs("audiobind", level=logging.WARNING) as logs:
            self.run_cycle(NEW)
        self.assertTrue(any("IP_UPDATER=auto: no tracker endpoint" in line for line in logs.output), logs.output)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        self.assertEqual(self.transport.calls, [])
        rows = dict(web._ip_updater_rows(self.w.status(self.state)))
        self.assertIn("acting as <code>notify</code>", rows["Allowed-IP updater"])


class TestApi(unittest.TestCase):
    """POST /api/watcher {"action": "use_current_ip"}: an unknown action with off, as before."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        saved = dict(config._EFFECTIVE)
        self.addCleanup(lambda: (config._EFFECTIVE.clear(), config._EFFECTIVE.update(saved)))
        self.patches = [tracker_cfg("c"), mock.patch.object(CONFIG, "tracker_home_ip", HOME),
                        mock.patch.object(CONFIG, "tracker_expected_egress_ip", PINNED)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.state.close()
        self.dir.cleanup()

    def post(self):
        handler = mock.Mock(spec=web.Handler)
        handler.state = self.state
        handler._read_json.return_value = {"action": "use_current_ip"}
        sent = []
        handler._send.side_effect = lambda code, body, ctype: sent.append((code, json.loads(body)))
        web.Handler._handle_watcher(handler)
        return sent[0]

    def test_off_is_an_unknown_action(self):
        with mock.patch.object(CONFIG, "ip_updater", "off"), \
                mock.patch.object(egress, "probe", side_effect=AssertionError("no probe with off")):
            self.assertEqual(self.post(), (400, {"error": "unknown action"}))

    def test_notify_refusal_is_409_and_success_is_200(self):
        with mock.patch.object(CONFIG, "ip_updater", "notify"):
            with mock.patch.object(egress, "probe", return_value=HOME):
                code, body = self.post()
            self.assertEqual(code, 409)
            self.assertIn("home connection", body["error"])
            with mock.patch.object(egress, "probe", return_value=NEW):
                code, body = self.post()
            self.assertEqual(code, 200)
            self.assertEqual(body["egress_expected"], NEW)
            self.assertEqual(body["ip_updater"]["last_new"], NEW)


if __name__ == "__main__":
    unittest.main()

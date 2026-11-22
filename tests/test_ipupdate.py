"""Parity feature 3: the allowed-IP updater (IP_UPDATER, ipupdate.decide, Use the current IP).

Nothing here touches the network: the egress probe is a fake, the tracker transport records calls,
and the MAM IP endpoints (jsonIp.php, dynamicSeedbox.php) go through a scripted MamTransport.
The search and download endpoints are never used by the updater.
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
        self.assertTrue(any("IP_UPDATER=auto: no MyAnonamouse session" in line for line in logs.output), logs.output)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        self.assertEqual(self.transport.calls, [])
        rows = dict(web._ip_updater_rows(self.w.status(self.state)))
        self.assertIn("acting as <code>notify</code>", rows["Allowed-IP updater"])


# ---------------------------------------------------------------------------- the MAM write path
#: a stand-in, not a session: the tests check that this string never reaches a log, a message or the page
SESSION = "FAKE-SESSION-SENTINEL-0001"
MAM_URL = "https://www.myanonamouse.net"
OK_SET = (200, b'{"Success": true, "msg": "Completed", "ip": "198.51.100.99"}')


def ip_answer(ip):
    return 200, json.dumps({"ip": ip, "ASN": 64496}).encode()


class MamTransport:
    """Fake transport for ipupdate: one scripted answer per URL, consumed in order, every call recorded."""

    def __init__(self, ip=(), seedbox=()):
        self.answers = {ipupdate.JSON_IP_URL: list(ip), ipupdate.DYNAMIC_SEEDBOX_URL: list(seedbox)}
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        queue = self.answers[url]
        if not queue:
            raise AssertionError(f"unexpected call to {url}")
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def urls(self):
        return [url.rsplit("/", 1)[-1] for _m, url, _h in self.calls]


class TestWrite(unittest.TestCase):
    """ipupdate.write / read_ip against a mocked transport. No network."""

    def write(self, transport):
        return ipupdate.write(SESSION, NEW, transport)

    def fails(self, transport, kind):
        with self.assertRaises(ipupdate.UpdateError) as ctx:
            self.write(transport)
        self.assertEqual(ctx.exception.kind, kind, str(ctx.exception))
        self.assertNotIn(SESSION, str(ctx.exception))
        return str(ctx.exception)

    def test_success_is_update_then_verify(self):
        t = MamTransport(ip=[ip_answer(NEW)], seedbox=[OK_SET])
        self.assertEqual(self.write(t), NEW)
        self.assertEqual(t.urls(), ["dynamicSeedbox.php", "jsonIp.php"])
        for method, _url, headers in t.calls:
            self.assertEqual(method, "GET")
            self.assertEqual(headers["Cookie"], f"mam_id={SESSION}")

    def test_session_rejected_is_auth(self):
        t = MamTransport(seedbox=[(403, b'{"Success": false, "msg": "Invalid session"}')])
        self.assertIn("renew TRACKER_SESSION", self.fails(t, ipupdate.AUTH))
        self.assertEqual(t.urls(), ["dynamicSeedbox.php"], "nothing to verify after a refusal")
        # a redirect to the login page is the same thing
        self.fails(MamTransport(seedbox=[(302, b"")]), ipupdate.AUTH)

    def test_session_not_allowed_is_refused(self):
        t = MamTransport(seedbox=[(403, b'{"Success": false, "msg": "Incorrect session type - not allowed this function"}')])
        why = self.fails(t, ipupdate.REFUSED)
        self.assertIn("Allow session to set dynamic seedbox IP", why)
        self.fails(MamTransport(seedbox=[(429, b'{"Success": false, "msg": "Last change too recent"}')]),
                   ipupdate.REFUSED)

    def test_unreachable_is_unavailable(self):
        self.fails(MamTransport(seedbox=[OSError("connection refused")]), ipupdate.UNAVAILABLE)
        self.fails(MamTransport(seedbox=[(503, b"<html>down</html>")]), ipupdate.UNAVAILABLE)
        # the update went out, the verify read did not come back: still not a success
        why = self.fails(MamTransport(seedbox=[OK_SET], ip=[TimeoutError()]), ipupdate.UNAVAILABLE)
        self.assertIn("could not be verified", why)

    def test_verify_disagreement_is_a_failure_not_a_success(self):
        t = MamTransport(seedbox=[OK_SET], ip=[ip_answer(PINNED)])
        why = self.fails(t, ipupdate.ERROR)
        self.assertIn(f"now shows {PINNED}, not {NEW}", why)
        self.assertIn("not counted as a success", why)

    def test_junk_answers_are_errors(self):
        self.fails(MamTransport(seedbox=[(200, b"<html>login</html>")]), ipupdate.ERROR)
        self.fails(MamTransport(seedbox=[OK_SET], ip=[(200, b'{"ip": "nope"}')]), ipupdate.ERROR)

    def test_the_session_is_never_echoed(self):
        body = json.dumps({"Success": False, "msg": f"Invalid session {SESSION}"}).encode()
        why = self.fails(MamTransport(seedbox=[(403, body)]), ipupdate.AUTH)
        self.assertIn("[session]", why)

    def test_no_session_sends_nothing(self):
        t = MamTransport()
        with self.assertRaises(ipupdate.UpdateError) as ctx:
            ipupdate.write("", NEW, t)
        self.assertEqual((ctx.exception.kind, t.calls), (ipupdate.AUTH, []))

    def test_read_ip(self):
        self.assertEqual(ipupdate.read_ip(SESSION, MamTransport(ip=[ip_answer(" 198.51.100.99 ")])), NEW)

    def test_is_mam(self):
        for url, want in ((MAM_URL, True), ("https://myanonamouse.net/", True), ("https://t.myanonamouse.net", True),
                          ("https://tracker.example", False), ("https://myanonamouse.net.evil.example", False),
                          ("https://notmyanonamouse.net", False), ("", False)):
            self.assertEqual(ipupdate.is_mam(url), want, url)

    def test_real_transport_only_requests_the_two_urls(self):
        transport = ipupdate.urllib_transport()
        with self.assertRaises(ipupdate.UpdateError):
            transport("GET", "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php", {}, 1)


class MamCase(UpdaterCase):
    """The watcher with a MyAnonamouse tracker URL and session, and a mocked MAM transport."""

    MODE = "auto"

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(CONFIG, "tracker_url", MAM_URL), mock.patch.object(CONFIG, "tracker_session", SESSION)):
            p.start()
            self.patches.append(p)
        self.mam = MamTransport()
        p = mock.patch.object(watcher, "make_ip_transport", lambda route: self.mam)
        p.start()
        self.patches.append(p)

    def script(self, ip=(), seedbox=()):
        self.mam.answers[ipupdate.JSON_IP_URL].extend(ip)
        self.mam.answers[ipupdate.DYNAMIC_SEEDBOX_URL].extend(seedbox)

    def page(self):
        return "".join(v for _k, v in web._ip_updater_rows(self.w.status(self.state)))

    def assert_no_session(self, *texts):
        for text in texts:
            self.assertNotIn(SESSION, str(text))


class TestAutoWrites(MamCase):
    def test_auto_writes_verifies_repins_and_logs_old_to_new(self):
        self.script(ip=[ip_answer(NEW), ip_answer(NEW)], seedbox=[OK_SET])
        with self.assertLogs("audiobind", level=logging.INFO) as logs:
            summary = self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), ["jsonIp.php", "dynamicSeedbox.php", "jsonIp.php"])
        self.assertEqual(summary["egress_guard"], "blocked", "this cycle's verdict stands")
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), NEW)
        rec = self.state.get_meta(watcher.META_IPUPDATE)
        self.assertEqual((rec["old"], rec["new"], rec["last_result"]["ok"]), (PINNED, NEW, True))
        self.assertAlmostEqual(rec["last_updated_at"], time.time(), delta=5)
        self.assertTrue(any("INFO" in line and f"{PINNED} -> {NEW}" in line for line in logs.output), logs.output)
        self.assertEqual(self.transport.calls, [], "the search and download endpoints are not involved")
        self.assertIn("set on MyAnonamouse", self.page())
        self.assert_no_session(logs.output, rec, self.page())
        self.assertEqual(self.run_cycle(NEW)["egress_guard"], "ok", "the next cycle passes on the new pin")

    def test_mam_answer_is_the_source_of_truth(self):
        # the probe says NEW, MAM sees the pinned IP: nothing has drifted as far as MAM is concerned
        self.script(ip=[ip_answer(PINNED)])
        self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), ["jsonIp.php"])
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)

    def test_failed_write_is_recorded_shown_and_not_retried_within_the_hour(self):
        self.script(ip=[ip_answer(NEW), ip_answer(NEW)],
                    seedbox=[(403, b'{"Success": false, "msg": "Incorrect session type"}')])
        with self.assertLogs("audiobind", level=logging.WARNING) as logs:
            self.run_cycle(NEW)
            self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), ["jsonIp.php", "dynamicSeedbox.php", "jsonIp.php"], "one write attempt")
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        rec = self.state.get_meta(watcher.META_IPUPDATE)
        self.assertEqual((rec["last_result"]["ok"], rec["last_result"]["kind"]), (False, "refused"))
        self.assertNotIn("last_updated_at", rec)
        self.assertIn("failed (refused)", self.page())
        self.assert_no_session(logs.output, self.page())

    def test_verify_disagreement_does_not_repin(self):
        self.script(ip=[ip_answer(NEW), ip_answer(PINNED)], seedbox=[OK_SET])
        self.run_cycle(NEW)
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        self.assertEqual(self.state.get_meta(watcher.META_IPUPDATE)["last_result"]["kind"], "error")
        self.assertIn("not counted as a success", self.page())


class TestGuardGatesTheWrite(MamCase):
    """No MAM request at all when the probe says home or failed; no write without a home IP to compare."""

    def test_home_ip_or_probe_failure_never_reaches_mam(self):
        for observed in (HOME, egress.EgressError("ipinfo.io: URLError")):
            self.run_cycle(observed)
            with self.probing(observed):
                self.assertFalse(watcher.use_current_ip(self.state)[0])
        self.assertEqual(self.mam.calls, [])

    def test_mam_seeing_the_home_ip_is_never_written(self):
        self.script(ip=[ip_answer(HOME)])
        self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), ["jsonIp.php"])

    def test_pin_without_home_blocks_even_the_credentialed_read(self):
        with mock.patch.object(CONFIG, "tracker_home_ip", ""):
            self.script(ip=[ip_answer(NEW)])
            summary = self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), [])
        self.assertTrue(summary["normal_tracker_blocked"])
        self.assertFalse(summary["updater_attempted"])
        self.assertIn("TRACKER_HOME_IP is not set", summary["updater_result"])

    def test_verified_drift_reports_recovery_separately_from_the_refusal(self):
        self.script(ip=[ip_answer(NEW), ip_answer(NEW)], seedbox=[OK_SET])
        with self.assertLogs("audiobind", level=logging.INFO) as logs:
            summary = self.run_cycle(NEW)
        self.assertEqual(self.mam.urls(), ["jsonIp.php", "dynamicSeedbox.php", "jsonIp.php"])
        self.assertNotIn("no request was sent to the tracker", "\n".join(logs.output))
        self.assertTrue(summary["normal_tracker_blocked"])
        self.assertTrue(summary["updater_attempted"])
        self.assertIn("verified", summary["updater_result"])
        rows = dict(web._ip_updater_rows(self.w.status(self.state)))
        self.assertIn("Attempted", rows["Last cycle's IP recovery"])

    def test_route_changes_after_recovery_read_never_reach_the_write(self):
        self.script(ip=[ip_answer(NEW)], seedbox=[OK_SET])

        def transport(*args):
            response = self.mam(*args)
            CONFIG.tracker_proxy = "http://other-proxy:8888"
            return response

        with mock.patch.object(CONFIG, "tracker_proxy", "http://original-proxy:8888"), \
                mock.patch.object(watcher, "make_ip_transport", return_value=transport) as factory:
            summary = self.run_cycle(NEW)
        self.assertEqual(factory.call_args.args[0].settings["tracker_proxy"], "http://original-proxy:8888")
        self.assertEqual(self.mam.urls(), ["jsonIp.php"])
        self.assertTrue(summary["updater_attempted"])
        self.assertIn("routing settings changed", summary["updater_result"])
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)

    def test_not_mam_means_no_mam_call(self):
        with mock.patch.object(CONFIG, "tracker_url", "https://tracker.example"):
            self.run_cycle(NEW)
            self.assertFalse(self.w.status(self.state)["ip_updater"]["can_write"])
        self.assertEqual(self.mam.calls, [])


class TestNotifyStaysReadOnly(MamCase):
    MODE = "notify"

    def test_notify_never_calls_the_update_endpoint(self):
        self.script(ip=[ip_answer(NEW)] * 3)
        for _ in range(3):
            self.run_cycle(NEW)
        self.assertNotIn("dynamicSeedbox.php", self.mam.urls())
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)


class TestOffWithMam(MamCase):
    MODE = "off"

    def test_off_makes_no_mam_call(self):
        self.run_cycle(NEW)
        self.assertEqual(self.mam.calls, [])
        self.assertNotIn("ip_updater", self.w.status(self.state))


class TestButtonWrites(MamCase):
    MODE = "notify"  # the button is the owner's explicit say-so: it writes in notify too

    def test_button_writes_verifies_and_repins(self):
        self.script(ip=[ip_answer(NEW), ip_answer(NEW)], seedbox=[OK_SET])
        with self.probing(NEW):
            ok, message = watcher.use_current_ip(self.state)
        self.assertTrue(ok, message)
        self.assertIn("verified", message)
        self.assertEqual(self.mam.urls(), ["jsonIp.php", "dynamicSeedbox.php", "jsonIp.php"])
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), NEW)

    def test_button_failure_says_why_and_keeps_the_pin(self):
        self.script(ip=[ip_answer(NEW)], seedbox=[(403, b'{"Success": false, "msg": "Invalid session"}')])
        with self.probing(NEW):
            ok, message = watcher.use_current_ip(self.state)
        self.assertFalse(ok)
        self.assertIn("(auth)", message)
        self.assert_no_session(message, self.page())
        self.assertEqual(config.effective_val("tracker_expected_egress_ip"), PINNED)
        self.assertIn("failed (auth)", self.page())


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


# ---------------------------------------------------------------- pass 3: IP_UPDATER is a page setting
try:
    from test_watcher_gui_settings import WatcherSettingsCase
except ImportError:
    from tests.test_watcher_gui_settings import WatcherSettingsCase


class TestPageSetting(WatcherSettingsCase):
    """ip_updater is a Settings > Watcher select, like download_client: page value > env > off."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(CONFIG, "ip_updater", "off")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plumbing_matches_the_other_selects(self):
        self.assertIn("ip_updater", config.SETTING_KEYS)
        self.assertNotIn("ip_updater", config.GENERAL_SETTING_KEYS, "never wiped by Reset to defaults")
        self.assertEqual(config.SETTING_MAP["ip_updater"], "ip_updater")
        self.assertIs(config.SETTING_TYPES["ip_updater"], str)
        self.assertEqual(config.SETTING_CHOICES["ip_updater"], config.IP_UPDATER_MODES)
        self.assertEqual(config.setting_env("ip_updater"), "off")

    def test_save_effective_page_round_trip(self):
        status, body, _raw = self.save({"settings": {"ip_updater": "Notify"}})
        self.assertEqual(status, 200, body)
        self.assertIn("ip_updater", body["custom"])
        self.assertEqual(config.setting_effective("ip_updater"), "notify")
        self.assertEqual(watcher.ip_updater_mode(), "notify")
        field = self.field("ip_updater")
        self.assertIn('<option value="notify" selected>notify</option>', field)
        self.assertIn('data-initial="notify"', field)
        self.assertIn("set here", field)

    def test_a_page_value_overrides_the_env_and_reset_hands_it_back(self):
        with mock.patch.object(CONFIG, "ip_updater", "auto"):
            self.assertEqual(watcher.ip_updater_mode(), "auto")
            self.assertIn("from the container", self.field("ip_updater"))
            self.assertEqual(self.save({"settings": {"ip_updater": "off"}})[0], 200)
            self.assertEqual(watcher.ip_updater_mode(), "off")
            self.assertIn('<option value="off" selected>off</option>', self.field("ip_updater"))
            self.assertEqual(self.save({"reset_keys": ["ip_updater"]})[0], 200)
            self.assertEqual(watcher.ip_updater_mode(), "auto")

    def test_a_bad_page_value_is_rejected_and_nothing_is_stored(self):
        status, body, _raw = self.save({"settings": {"ip_updater": "on"}})
        self.assertEqual(status, 400)
        self.assertIn("ip_updater: must be one of off, notify, auto", body["error"])
        self.assertIsNone(self.state.get_meta("setting_ip_updater"))
        self.assertEqual(watcher.ip_updater_mode(), "off")

    def test_off_renders_the_section_but_no_status_rows(self):
        page = self.page()
        field = self.field("ip_updater", page)
        self.assertIn('<div class="ab-acct-title">Allowed-IP updater</div>', page)
        self.assertIn('<option value="off" selected>off</option>', field)
        for mode, sentence in web.IP_UPDATER_MODE_HINTS.items():
            self.assertIn(f"<li><b>{mode}</b> &#8212; {web._html(sentence)}</li>", field)
        self.assertIn('id="ab-ipupd-off"', field)
        self.assertNotIn("ab-ipupd-grid", page)
        self.assertNotIn('id="ab-watch-useip"', page)
        self.assertNotIn("<dt>Allowed IP</dt>", page)

    def test_notify_and_auto_render_the_status_rows(self):
        for mode in ("notify", "auto"):
            with self.subTest(mode=mode):
                self.assertEqual(self.save({"settings": {"ip_updater": mode}})[0], 200)
                field = self.field("ip_updater")
                self.assertIn(f'<option value="{mode}" selected>{mode}</option>', field)
                self.assertNotIn('id="ab-ipupd-off"', field)
                self.assertIn('<dl class="ab-watch-grid ab-ipupd-grid">', field)
                for row in ("Allowed-IP updater", "Allowed IP", "Last updated"):
                    self.assertIn(f"<dt>{row}</dt>", field)
                self.assertIn(f"IP_UPDATER={mode}", field)
                self.assertIn('id="ab-watch-useip"', field)


class TestPageOffBeatsEnvNotify(UpdaterCase):
    """The container says notify, the page says off: off wins, and off still costs nothing."""

    MODE = "notify"

    def setUp(self):
        super().setUp()
        config._EFFECTIVE["ip_updater"] = "off"

    def test_no_updater_call_no_log_line_no_status_key(self):
        with mock.patch.object(watcher.Watcher, "_ip_notice", side_effect=AssertionError("not with off")), \
                self.assertLogs("audiobind", level=logging.DEBUG) as logs:
            self.run_cycle(NEW)
        self.assertFalse(any("allowed-IP updater" in line for line in logs.output), logs.output)
        self.assertEqual(self.transport.calls, [])
        self.assertNotIn("ip_updater", self.w.status(self.state))


if __name__ == "__main__":
    unittest.main()

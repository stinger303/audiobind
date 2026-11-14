"""0.18.0-dev2/dev3: the VPN-safe MAM client - no fingerprint, MAM_PROXY (fail closed), the egress guard.

Nothing here touches the network: the MAM transport, the proxy opener and the egress probe are
all fakes.
"""

import json
import logging
import pathlib
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from audiobind import __version__, config, egress, mam, watcher, web
from audiobind.config import CONFIG, Config
from audiobind.state import State

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

try:
    from test_watcher import NOTHING, FakeQbit, WatcherCase, answer, entry
except ImportError:
    from tests.test_watcher import NOTHING, FakeQbit, WatcherCase, answer, entry

HOME = "67.176.81.133"
VPN = "213.152.187.195"


def recording_client(**kwargs):
    seen = []

    def transport(method, url, data, headers, timeout):
        seen.append(dict(headers))
        return 200, {}, b'{"error": "Nothing returned, out of 0"}'

    clock = mock.Mock(return_value=0.0)
    limiter = mam.RateLimiter(clock=clock, sleep=lambda s: None)
    return mam.MamClient("cookie", transport=transport, limiter=limiter, **kwargs), seen


class TestFingerprint(unittest.TestCase):
    def test_default_sends_no_user_agent_and_no_version(self):
        client, seen = recording_client()
        client.search("Project Hail Mary", "Andy Weir")
        client.transport = lambda m, u, d, h, t: (seen.append(dict(h)) or (200, {}, b"d4:infod4:name1:aee"))
        client.download(1, False)
        self.assertEqual(len(seen), 2)
        for headers in seen:
            self.assertNotIn("user-agent", {k.lower() for k in headers})
            blob = " ".join(f"{k}: {v}" for k, v in headers.items())
            self.assertNotIn("audiobind/", blob)
            self.assertNotIn(__version__, blob)

    def test_owner_user_agent_is_used_verbatim(self):
        client, seen = recording_client(user_agent="Mozilla/5.0 (X11; Linux x86_64)")
        client.search("x")
        self.assertEqual(seen[0]["User-Agent"], "Mozilla/5.0 (X11; Linux x86_64)")

    def test_config_reads_the_env(self):
        with mock.patch.dict("os.environ", {"MAM_USER_AGENT": " Custom/1 ", "MAM_PROXY": "", "MAM_HOME_WAN_IP": ""}):
            cfg = Config.from_env()
        self.assertEqual((cfg.mam_user_agent, cfg.mam_proxy, cfg.mam_home_wan_ip), ("Custom/1", "", ""))
        self.assertNotIn("MAM_PROXY", repr(cfg))


class TestProxy(unittest.TestCase):
    def setUp(self):
        mam._OPENERS.clear()
        self.addCleanup(mam._OPENERS.clear)

    def test_opener_is_built_once_and_routes_http_and_https_through_the_proxy(self):
        a = mam._opener("http://vpn:8888")
        self.assertIs(a, mam._opener("http://vpn:8888"))
        proxies = [h for h in a.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {"http": "http://vpn:8888", "https": "http://vpn:8888"})
        direct = mam._opener("")
        self.assertIsNot(direct, a)

    def test_client_default_transport_carries_the_proxy(self):
        opened = []

        class Fake:
            def open(self, request, timeout=None):
                opened.append(request.full_url)
                raise urllib.error.URLError("proxy down")

        with mock.patch.object(mam, "_opener", return_value=Fake()) as get:
            client = mam.MamClient("c", proxy="http://vpn:8888",
                                   limiter=mam.RateLimiter(clock=lambda: 0.0, sleep=lambda s: None))
            with self.assertRaises(mam.MamError) as ctx:
                client.search("x")
        get.assert_called_once_with("http://vpn:8888")
        self.assertEqual(len(opened), 1, "a failing proxied request is attempted once - no direct retry")
        self.assertIn("could not be reached", str(ctx.exception))

    def test_failing_proxy_never_produces_a_second_unproxied_attempt(self):
        attempts = []

        class Failing:
            def __init__(self, proxy):
                self.proxy = proxy

            def open(self, request, timeout=None):
                attempts.append(self.proxy)
                raise OSError("connection refused")

        with mock.patch.object(mam, "_opener", side_effect=lambda proxy="": Failing(proxy)), \
                mock.patch.object(urllib.request, "build_opener",
                                  side_effect=AssertionError("a direct opener must never be built")), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("no direct urlopen")):
            with self.assertRaises(mam.MamError):
                mam.urllib_transport("GET", mam.download_url(5, False), None, {}, 1, proxy="http://vpn:8888")
        self.assertEqual(attempts, ["http://vpn:8888"])

    def test_direct_failure_is_unchanged(self):
        class Failing:
            def open(self, request, timeout=None):
                raise urllib.error.URLError("no route")

        with mock.patch.object(mam, "_opener", return_value=Failing()):
            with self.assertRaises(urllib.error.URLError):
                mam.urllib_transport("GET", mam.download_url(5, False), None, {}, 1)

    def test_config_accepts_http_and_https_and_empty(self):
        for value in ("http://vpn:8888", "https://proxy.example:3128", "http://user:pw@10.0.0.2:8888/"):
            with mock.patch.dict("os.environ", {"MAM_PROXY": value}):
                self.assertEqual(Config.from_env().mam_proxy, value.rstrip("/"))

    def test_malformed_proxy_fails_at_config_load_naming_the_variable(self):
        for value in ("vpn:8888", "socks5://vpn:1080", "http://", "http://vpn:notaport", "ftp://vpn:21",
                      "http://vpn:8888/path", "just words"):
            with mock.patch.dict("os.environ", {"MAM_PROXY": value}):
                with self.assertRaises(RuntimeError, msg=value) as ctx:
                    Config.from_env()
            self.assertIn("MAM_PROXY", str(ctx.exception))

    def test_malformed_home_ip_fails_at_config_load(self):
        with mock.patch.dict("os.environ", {"MAM_HOME_WAN_IP": "67.176.81"}):
            with self.assertRaises(RuntimeError) as ctx:
                Config.from_env()
        self.assertIn("MAM_HOME_WAN_IP", str(ctx.exception))

    def test_watcher_factory_passes_proxy_and_user_agent(self):
        with mock.patch.object(CONFIG, "mam_id", "c"), mock.patch.object(CONFIG, "mam_proxy", "http://vpn:8888"), \
                mock.patch.object(CONFIG, "mam_user_agent", "UA/1"):
            client = watcher.make_mam()
        self.assertEqual(client.transport.keywords, {"proxy": "http://vpn:8888"})
        self.assertEqual(client._user_agent, "UA/1")


class TestEgressModule(unittest.TestCase):
    def fetcher(self, answers):
        calls = []

        def fetch(url, proxy, timeout):
            calls.append((url, proxy))
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        return fetch, calls

    def test_probe_strips_whitespace_and_validates(self):
        fetch, calls = self.fetcher([f" {VPN}\n"])
        with mock.patch.object(egress, "_fetch", fetch):
            self.assertEqual(egress.probe("http://vpn:8888"), VPN)
        self.assertEqual(calls, [("https://ipinfo.io/ip", "http://vpn:8888")])

    def test_probe_falls_back_once_and_accepts_ipv6(self):
        fetch, calls = self.fetcher([OSError("down"), "2001:db8::1\n"])
        with mock.patch.object(egress, "_fetch", fetch):
            self.assertEqual(egress.probe(""), "2001:db8::1")
        self.assertEqual([c[0] for c in calls], ["https://ipinfo.io/ip", "https://api.ipify.org"])

    def test_probe_rejects_junk_and_raises_after_one_fallback(self):
        fetch, calls = self.fetcher(["<html>rate limited</html>", "not an ip"])
        with mock.patch.object(egress, "_fetch", fetch):
            with self.assertRaises(egress.EgressError):
                egress.probe("")
        self.assertEqual(len(calls), 2)

    def test_judge(self):
        self.assertEqual(egress.judge("", VPN)[0], "off")
        self.assertEqual(egress.judge("", "")[0], "off")
        self.assertEqual(egress.judge(HOME, HOME)[0], "blocked")
        self.assertIn(HOME, egress.judge(HOME, HOME)[1])
        self.assertEqual(egress.judge(HOME, VPN)[0], "ok")
        self.assertEqual(egress.judge(HOME, "")[0], "unknown")
        self.assertEqual(egress.judge("2001:db8::1", "2001:0db8:0:0:0:0:0:1")[0], "blocked")


class GuardCase(WatcherCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(CONFIG, "mam_home_wan_ip", HOME),
                  mock.patch.object(CONFIG, "mam_proxy", "http://vpn:8888")):
            p.start()
            self.patches.append(p)
        self.want()

    def run_cycle(self, observed, *answers):
        """A cycle with a fake probe; returns (summary, probe calls)."""
        self.mam(*answers)
        calls = []

        def fake_probe(proxy, timeout=10.0):
            calls.append(proxy)
            if isinstance(observed, Exception):
                raise observed
            return observed

        with mock.patch.object(egress, "probe", fake_probe):
            return self.cycle(), calls


class TestGuardInTheWatcher(GuardCase):
    def test_blocked_refuses_with_zero_mam_calls(self):
        summary, probes = self.run_cycle(HOME, answer(entry(1, "Project Hail Mary")))
        self.assertEqual(self.transport.calls, [], "not one MAM request")
        self.assertEqual(self.w.client().requests, 0)
        self.assertEqual(FakeQbit.instances, [])
        self.assertEqual(summary["egress_guard"], "blocked")
        self.assertIn(HOME, summary["error"])
        self.assertEqual(probes, ["http://vpn:8888"], "the probe goes through the configured proxy")
        self.assertEqual(self.row()["status"], "wanted")
        self.assertIn(HOME, self.state.get_meta(watcher.META_LAST_ERROR)["message"])
        self.assertEqual(self.state.get_meta(watcher.META_LAST_CYCLE)["egress_guard"], "blocked")

    def test_blocked_logs_an_error(self):
        with self.assertLogs("audiobind", level=logging.ERROR) as logs:
            self.run_cycle(HOME)
        self.assertTrue(any("egress guard" in line and HOME in line for line in logs.output), logs.output)

    def test_unknown_refuses_with_zero_mam_calls(self):
        summary, _ = self.run_cycle(egress.EgressError("ipinfo.io: URLError; api.ipify.org: URLError"),
                                    answer(entry(1, "Project Hail Mary")))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(summary["egress_guard"], "unknown")
        self.assertIn("could not be verified", summary["error"])
        self.assertEqual(self.row()["status"], "wanted")

    def test_dry_run_is_not_exempt(self):
        with mock.patch.object(CONFIG, "dry_run", True):
            watcher.CONTROL._dry_run = True
            summary, _ = self.run_cycle(HOME, answer(entry(1, "Project Hail Mary")))
        self.assertTrue(summary["dry_run"])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(summary["egress_guard"], "blocked")

    def test_ok_proceeds_and_logs_the_egress_ip_once(self):
        with self.assertLogs("audiobind", level=logging.INFO) as logs:
            summary, probes = self.run_cycle(VPN, NOTHING)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(summary["egress_guard"], "ok")
        self.assertEqual(probes, ["http://vpn:8888"])
        self.assertEqual([line for line in logs.output if f"MAM egress: {VPN}" in line].__len__(), 1)

    def test_probe_runs_every_cycle(self):
        _, first = self.run_cycle(VPN, NOTHING)
        _, second = self.run_cycle(HOME)
        self.assertEqual((len(first), len(second)), (1, 1))

    def test_a_blocked_cycle_recovers_when_the_vpn_is_back(self):
        self.run_cycle(HOME)
        summary, _ = self.run_cycle(VPN, NOTHING)
        self.assertEqual(summary["egress_guard"], "ok")
        self.assertEqual(len(self.transport.calls), 1)

    def test_status_fields(self):
        self.run_cycle(VPN, NOTHING)
        status = self.w.status(self.state)
        self.assertEqual((status["egress_ip"], status["egress_guard"]), (VPN, "ok"))
        self.assertTrue(status["egress_checked_at"])
        self.assertIn(VPN, status["egress_reason"])
        self.run_cycle(HOME)
        status = self.w.status(self.state)
        self.assertEqual((status["egress_ip"], status["egress_guard"]), (HOME, "blocked"))
        self.assertIn(HOME, status["egress_reason"])
        self.run_cycle(OSError("x"))
        status = self.w.status(self.state)
        self.assertEqual(status["egress_guard"], "unknown")
        self.assertIn("could not be verified", status["egress_reason"])


class TestGuardOff(WatcherCase):
    def test_empty_config_means_no_probe_at_all(self):
        self.want()
        self.mam(NOTHING)
        with mock.patch.object(egress, "probe", side_effect=AssertionError("no probe when unarmed")), \
                mock.patch.object(egress, "_fetch", side_effect=AssertionError("no network")):
            summary = self.cycle()
        self.assertEqual(summary["egress_guard"], "off")
        self.assertEqual(len(self.transport.calls), 1)
        status = self.w.status(self.state)
        self.assertEqual((status["egress_guard"], status["egress_ip"], status["egress_checked_at"]), ("off", "", None))


class TestGuardUI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.patches = [mock.patch.object(CONFIG, "mam_id", "c"), mock.patch.object(CONFIG, "qbit_user", "u"),
                        mock.patch.object(CONFIG, "qbit_pass", "p"),
                        mock.patch.object(CONFIG, "mam_home_wan_ip", HOME)]
        for p in self.patches:
            p.start()
        watcher.WATCHER._paused = None

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.state.close()
        self.dir.cleanup()

    def record(self, ip):
        self.state.set_meta(watcher.META_EGRESS, {"ip": ip, "at": time.time(), "reason": ""})

    def test_ok_shows_what_mam_sees_and_the_guard(self):
        self.record(VPN)
        html = web.render_settings(self.state)
        self.assertIn(f"MAM sees <b>{VPN}</b>", html)
        self.assertIn("Checked ", html)
        self.assertIn(f"refuses to run from your home IP ({HOME})", html)
        self.assertNotIn("Stopped:", html)

    def test_blocked_notice_on_settings_and_the_wishlist_bar(self):
        self.record(HOME)
        html = web.render_settings(self.state)
        self.assertIn("Stopped: MAM would not see the allowed address.", html)
        self.assertIn("which is your home connection", html)
        self.assertIn("start your VPN container (or fix MAM_PROXY)", html)
        self.assertNotIn("http://", html)
        bar = web.render_discover(self.state, "wishlist")
        self.assertIn("this connection is not the one you allowed", bar)
        self.assertNotIn("is disabled", bar)

    def test_unknown_after_a_check_stops_the_bar_but_never_checked_does_not(self):
        self.assertNotIn("stopped", web._watcher_bar(watcher.WATCHER.status(self.state)))
        self.state.set_meta(watcher.META_EGRESS, {"ip": "", "at": time.time(), "reason": "the egress IP could not be verified (x)"})
        bar = web._watcher_bar(watcher.WATCHER.status(self.state))
        self.assertIn("cannot confirm this connection is the VPN", bar)

    def test_unarmed_says_so(self):
        with mock.patch.object(CONFIG, "mam_home_wan_ip", ""):
            html = web.render_settings(self.state)
        self.assertIn("not armed (set the allowed IP below, or MAM_HOME_WAN_IP", html)


OTHER = "203.0.113.9"


class ExpectedCase(WatcherCase):
    HOME_SET = ""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(CONFIG, "mam_home_wan_ip", self.HOME_SET),
                  mock.patch.object(CONFIG, "mam_expected_egress_ip", VPN),
                  mock.patch.object(CONFIG, "mam_proxy", "http://vpn:8888")):
            p.start()
            self.patches.append(p)
        config._EFFECTIVE.clear()
        self.want()

    def tearDown(self):
        config._EFFECTIVE.clear()
        super().tearDown()

    run_cycle = GuardCase.run_cycle


class TestExpectedJudge(unittest.TestCase):
    def test_order_and_reasons(self):
        self.assertEqual(egress.judge("", "", "")[0], "off")
        self.assertEqual(egress.judge("", VPN, VPN), ("ok", f"MAM sees {VPN}, the allowed address"))
        verdict, reason = egress.judge("", OTHER, VPN)
        self.assertEqual(verdict, "blocked")
        self.assertEqual(reason, f"MAM would see {OTHER}, but this installation only allows {VPN}")
        self.assertEqual(egress.judge("", "", VPN)[0], "unknown")
        # home wins over expected, with the home reason
        verdict, reason = egress.judge(HOME, HOME, VPN)
        self.assertEqual(verdict, "blocked")
        self.assertIn("home connection", reason)
        # a different, non-expected address: expected-mismatch reason
        self.assertIn("only allows", egress.judge(HOME, OTHER, VPN)[1])
        self.assertEqual(egress.judge(HOME, VPN, VPN)[0], "ok")
        # unchanged when expected is empty
        self.assertEqual(egress.judge(HOME, VPN, "")[0], "ok")
        self.assertEqual(egress.judge(HOME, HOME, "")[0], "blocked")


class TestExpectedInTheWatcher(ExpectedCase):
    def test_match_proceeds(self):
        summary, probes = self.run_cycle(VPN, NOTHING)
        self.assertEqual(summary["egress_guard"], "ok")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(probes), 1, "the probe runs for the expected IP alone, with no home IP set")

    def test_mismatch_is_blocked_with_zero_mam_calls(self):
        summary, _ = self.run_cycle(OTHER, answer(entry(1, "Project Hail Mary")))
        self.assertEqual(summary["egress_guard"], "blocked")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.w.client().requests, 0)
        self.assertEqual(FakeQbit.instances, [])
        self.assertIn(f"MAM would see {OTHER}, but this installation only allows {VPN}", summary["error"])
        self.assertEqual(self.row()["status"], "wanted")
        status = self.w.status(self.state)
        self.assertEqual((status["egress_guard"], status["egress_expected"], status["egress_ip"]),
                         ("blocked", VPN, OTHER))
        self.assertIn("only allows", status["egress_reason"])

    def test_probe_failure_is_unknown_with_zero_mam_calls(self):
        summary, _ = self.run_cycle(egress.EgressError("down"), answer(entry(1, "Project Hail Mary")))
        self.assertEqual(summary["egress_guard"], "unknown")
        self.assertEqual(self.transport.calls, [])

    def test_status_before_any_check_names_the_expected_ip(self):
        status = self.w.status(self.state)
        self.assertEqual((status["egress_guard"], status["egress_expected"]), ("unknown", VPN))


class TestBothSet(ExpectedCase):
    HOME_SET = HOME

    def test_home_wins_with_the_home_reason(self):
        summary, _ = self.run_cycle(HOME, NOTHING)
        self.assertEqual(summary["egress_guard"], "blocked")
        self.assertIn("home connection", summary["error"])
        self.assertNotIn("only allows", summary["error"])
        self.assertEqual(self.transport.calls, [])

    def test_other_address_gets_the_expected_mismatch_reason(self):
        summary, _ = self.run_cycle(OTHER, NOTHING)
        self.assertEqual(summary["egress_guard"], "blocked")
        self.assertIn("only allows", summary["error"])
        self.assertEqual(self.transport.calls, [])


class TestExpectedEmptyKeepsDev2(GuardCase):
    def test_home_only_is_unchanged(self):
        with mock.patch.object(CONFIG, "mam_expected_egress_ip", ""):
            summary, _ = self.run_cycle(OTHER, NOTHING)
            self.assertEqual(summary["egress_guard"], "ok")
            self.assertEqual(len(self.transport.calls), 1)
            summary, _ = self.run_cycle(HOME, NOTHING)
            self.assertEqual(summary["egress_guard"], "blocked")
            self.assertEqual(self.transport.calls, [], "run_cycle installs a fresh transport: none used")


class TestExpectedConfig(unittest.TestCase):
    def test_junk_is_rejected_naming_the_variable(self):
        for value in ("213.152.187", "vpn.example", "1.2.3.4/32", "999.1.1.1"):
            with mock.patch.dict("os.environ", {"MAM_EXPECTED_EGRESS_IP": value}):
                with self.assertRaises(RuntimeError, msg=value) as ctx:
                    Config.from_env()
            self.assertIn("MAM_EXPECTED_EGRESS_IP", str(ctx.exception))

    def test_empty_and_valid_are_accepted(self):
        with mock.patch.dict("os.environ", {"MAM_EXPECTED_EGRESS_IP": ""}):
            self.assertEqual(Config.from_env().mam_expected_egress_ip, "")
        with mock.patch.dict("os.environ", {"MAM_EXPECTED_EGRESS_IP": f" {VPN} "}):
            self.assertEqual(Config.from_env().mam_expected_egress_ip, VPN)
        with mock.patch.dict("os.environ", {"MAM_EXPECTED_EGRESS_IP": "2001:0db8::1"}):
            self.assertEqual(Config.from_env().mam_expected_egress_ip, "2001:db8::1")


class TestExpectedSetting(LiveServerCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(CONFIG, "mam_id", "x")  # these tests are about the MAM surfaces
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(CONFIG, "mam_expected_egress_ip", "")
        p.start()
        self.addCleanup(p.stop)
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def post(self, settings=None, **extra):
        body = dict(extra)
        if settings is not None:
            body["settings"] = settings
        req = urllib.request.Request(
            self.base + "/api/settings", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def w_status(self):
        return watcher.WATCHER.status(self.state)

    def test_invalid_is_refused_and_not_stored(self):
        for junk in ("not-an-ip", "1.2.3", "1.2.3.4/24", 12345, ["1.2.3.4"]):
            status, body = self.post({"expected_egress": junk})
            self.assertEqual(status, 400, junk)
            self.assertIn("expected_egress", body["error"])
            self.assertIn("not an IPv4 or IPv6 address", body["error"])
        self.assertIsNone(self.state.get_meta("setting_expected_egress"))
        self.assertEqual(config.effective_val("mam_expected_egress_ip"), "")

    def test_valid_persists_and_is_what_the_watcher_reads(self):
        status, body = self.post({"expected_egress": f" {VPN} "})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["custom"], ["expected_egress"])
        self.assertEqual(self.state.get_meta("setting_expected_egress"), VPN)
        self.assertEqual(config.effective_val("mam_expected_egress_ip"), VPN)
        self.assertEqual(watcher._setting("mam_expected_egress_ip"), VPN)
        self.assertEqual(self.w_status()["egress_expected"], VPN)
        page = web.render_settings(self.state)
        self.assertIn('data-setting="expected_egress"', page)
        self.assertIn(f'value="{VPN}" data-initial="{VPN}"', page)
        self.assertIn("Only reach MAM from one IP", page)
        self.assertIn("Leave it empty to skip this check.", page)

    def test_blank_turns_it_off_even_over_an_env_value(self):
        with mock.patch.object(CONFIG, "mam_expected_egress_ip", VPN):
            page = web.render_settings(self.state)
            self.assertIn(f'value="{VPN}" data-initial="{VPN}"', page, "the env value is the visible default")
            status, body = self.post({"expected_egress": ""})
            self.assertEqual(status, 200, body)
            self.assertEqual(config.effective_val("mam_expected_egress_ip"), "")
            self.assertEqual(self.w_status()["egress_expected"], "")
            self.assertEqual(self.w_status()["egress_guard"], "off")

    def test_reset_hands_back_the_env_value(self):
        self.post({"expected_egress": OTHER})
        with mock.patch.object(CONFIG, "mam_expected_egress_ip", VPN):
            status, body = self.post(reset=True)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["env_defaults"]["expected_egress"], VPN)
            self.assertEqual(config.effective_val("mam_expected_egress_ip"), VPN)


class TestExpectedUI(TestGuardUI):
    def test_mismatch_shows_observed_expected_verdict_and_what_to_change(self):
        with mock.patch.object(CONFIG, "mam_expected_egress_ip", VPN), \
                mock.patch.object(CONFIG, "mam_home_wan_ip", ""):
            self.record(OTHER)
            html = web.render_settings(self.state)
        self.assertIn(f"MAM sees <b>{OTHER}</b>", html)
        self.assertIn(f"expected <b>{VPN}</b>", html)
        self.assertIn("blocked", html)
        self.assertIn(f"only allows {VPN}", html)
        self.assertIn("start your VPN container", html)
        self.assertIn(f"only reaches MAM from {VPN}", html)

    def test_match_shows_allowed(self):
        with mock.patch.object(CONFIG, "mam_expected_egress_ip", VPN), \
                mock.patch.object(CONFIG, "mam_home_wan_ip", ""):
            self.record(VPN)
            html = web.render_settings(self.state)
        self.assertIn('<span class="ok">allowed</span>', html)


if __name__ == "__main__":
    unittest.main()

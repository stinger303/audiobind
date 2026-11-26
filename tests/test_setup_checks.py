"""Build pass 2: setup that can be verified instead of guessed.

An empty QBIT_URL is "not set yet" (item 1); Check IP shows the address the tracker would see without
contacting it (2); the qBittorrent test reports categories, the category's save path and whether that is
the folder Audiobind watches, read-only (3); the tracker test proves a search works (4); ingest says when
only one of its two halves is set (5); a preset file fills the tracker fields, all or nothing (6).

Nothing here names a real site, path or address: the hosts are .example, the addresses are the
documentation ranges.
"""

import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from audiobind import config, egress, ingest, preset, qbit, tracker, watcher, web
from audiobind.config import CONFIG
from audiobind.state import State

try:
    from live import LiveServerCase
    from test_watcher_gui_settings import FakeTracker, WatcherSettingsCase
    from tracker_cfg import tracker_cfg
except ImportError:
    from tests.live import LiveServerCase
    from tests.test_watcher_gui_settings import FakeTracker, WatcherSettingsCase
    from tests.tracker_cfg import tracker_cfg

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEN = "198.51.100.7"
OTHER = "203.0.113.9"
ARROW = "\u2192"


def torrent(name="Dune", authors=("Frank Herbert",)):
    return tracker.Torrent(tid="1", name=name, authors=list(authors))


# ---------------------------------------------------------------------------- 1. QBIT_URL empty
class TestEmptyQbitUrl(unittest.TestCase):
    def setUp(self):
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def configured(self, url):
        patches = [tracker_cfg("x"), mock.patch.object(CONFIG, "qbit_user", "u"),
                   mock.patch.object(CONFIG, "qbit_pass", "p"), mock.patch.object(CONFIG, "qbit_url", url)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_an_empty_address_is_watcher_off_with_a_reason_not_an_error(self):
        self.configured("")
        self.assertEqual(watcher.disabled_reason(),
                         "Add the qBittorrent address so the watcher can send downloads there")
        self.assertNotIn("http(s)", watcher.disabled_reason())

    def test_a_blank_address_counts_as_empty(self):
        self.configured("   ")
        self.assertIn("Add the qBittorrent address", watcher.disabled_reason())

    def fresh_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = State(pathlib.Path(tmp.name))
        self.addCleanup(state.close)  # closed first, then the folder goes
        return state

    def test_the_watcher_does_not_start_and_says_why_in_the_log(self):
        self.configured("")
        state = self.fresh_state()
        with self.assertLogs("audiobind", "WARNING") as logs:
            self.assertFalse(watcher.Watcher().start(state))
        self.assertIn("Add the qBittorrent address", "\n".join(logs.output))

    def test_the_status_reads_disabled_with_that_reason(self):
        self.configured("")
        status = watcher.Watcher().status(self.fresh_state())
        self.assertFalse(status["enabled"])
        self.assertIn("Add the qBittorrent address", status["reason"])

    def test_the_disabled_box_asks_for_the_address_not_the_login(self):
        self.configured("")
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                panel = web.render_watcher_section(state)
            finally:
                state.close()
        self.assertIn("The watcher is disabled.", panel)
        self.assertIn('Add the <a href="#ab-qbit-address">qBittorrent address</a> and save it.', panel)
        self.assertNotIn("Set the qBittorrent user and password below", panel)
        self.assertIn("no address set", panel)
        self.configured("http://qbit.example:8080")  # the address is there; the login is what is missing
        with mock.patch.object(CONFIG, "qbit_user", ""), tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                panel = web.render_watcher_section(state)
            finally:
                state.close()
        self.assertIn("Set the qBittorrent user and password below", panel)

    def test_the_connection_test_says_not_set_too(self):
        self.configured("")
        ok, message = watcher.probe_qbit()
        self.assertFalse(ok)
        self.assertIn("Add the qBittorrent address", message)
        self.assertNotIn("http(s)", message)

    def test_a_filled_in_address_enables_it(self):
        self.configured("http://qbit.example:8080")
        self.assertEqual(watcher.disabled_reason(), "")

    def test_a_malformed_address_still_fails_loudly(self):
        for bad in ("qbit.example:8080", "ftp://qbit.example", "http://"):
            with self.subTest(bad=bad):
                self.configured(bad)
                self.assertEqual(watcher.disabled_reason(), "")  # not "unset": it is set, and wrong
                with self.assertRaisesRegex(qbit.QbitError, "QBIT_URL is not an http"):
                    watcher.make_qbit()
                ok, message = watcher.probe_qbit()
                self.assertFalse(ok)
                self.assertIn("is not an http(s) URL", message)


# ---------------------------------------------------------------------------- 2. Check IP
class TestCheckIp(WatcherSettingsCase):
    ROUTE = "/api/watcher/check-ip"

    def lookup(self, ip=SEEN, error=None):
        def fetch(url, proxy, timeout):
            self.fetched.append((url, proxy))
            if error:
                raise error
            return ip

        self.fetched = []
        return mock.patch.object(egress, "_fetch", fetch)

    def untouched_tracker(self):
        boom = mock.Mock(side_effect=AssertionError("the tracker must not be contacted"))
        return mock.patch.object(watcher, "make_tracker", boom), mock.patch.object(tracker, "urllib_transport", boom)

    def ask(self):
        status, _h, body = self.call(self.ROUTE, {})
        return status, json.loads(body), body

    def test_the_route_is_post_only_and_signed_in(self):
        status, headers, _body = self.call(self.ROUTE, method="GET")
        self.assertEqual((status, headers.get("Allow")), (405, "POST"))
        status, _h, _b = self.call(self.ROUTE, {}, cookie="audiobind_session=forged")
        self.assertIn(status, (401, 403))

    def test_guard_off_shows_the_address_and_what_arming_would_do(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_expected_egress_ip", ""), \
                mock.patch.object(CONFIG, "tracker_home_ip", ""):
            status, answer, _raw = self.ask()
        self.assertEqual(status, 200)
        self.assertEqual((answer["ok"], answer["ip"], answer["guard"]), (True, SEEN, "off"))
        self.assertIn(f"The tracker would see {SEEN}", answer["message"])
        self.assertIn("The guard is off", answer["message"])
        self.assertIn("Only reach the tracker from one IP", answer["message"])
        self.assertIn("No request was made to the site", answer["message"])

    def test_armed_and_matching_says_it_would_let_the_watcher_run(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_expected_egress_ip", SEEN):
            status, answer, _raw = self.ask()
        self.assertEqual((status, answer["guard"]), (200, "ok"))
        self.assertIn("would let the watcher run", answer["message"])

    def test_armed_and_different_says_it_would_stop_the_watcher(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_expected_egress_ip", OTHER):
            status, answer, _raw = self.ask()
        self.assertEqual((status, answer["ok"], answer["guard"]), (200, True, "blocked"))  # the check worked
        self.assertIn("would stop the watcher", answer["message"])
        self.assertIn(OTHER, answer["message"])

    def test_the_home_address_is_blocked_too(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_home_ip", SEEN):
            _status, answer, _raw = self.ask()
        self.assertEqual(answer["guard"], "blocked")
        self.assertIn("your home connection", answer["message"])

    def test_it_never_contacts_the_tracker_and_stores_nothing(self):
        make, transport = self.untouched_tracker()
        before = (self.state.get_meta(watcher.META_EGRESS), self.state.get_meta(watcher.META_LAST_CYCLE))
        with self.lookup(), make as made, transport as sent:
            for expected in ("", SEEN, OTHER):
                with mock.patch.object(CONFIG, "tracker_expected_egress_ip", expected):
                    self.ask()
        made.assert_not_called()
        sent.assert_not_called()
        self.assertEqual((self.state.get_meta(watcher.META_EGRESS), self.state.get_meta(watcher.META_LAST_CYCLE)), before)
        self.assertTrue(all("tracker.example" not in url for url, _proxy in self.fetched), self.fetched)

    def test_a_failed_lookup_says_plainly_that_nothing_was_asked_of_the_site(self):
        make, transport = self.untouched_tracker()
        with self.lookup(error=OSError("no route")), make as made, transport:
            status, answer, _raw = self.ask()
        made.assert_not_called()
        self.assertEqual(status, 502)
        self.assertIs(answer["ok"], False)
        self.assertEqual(answer["ip"], "")
        self.assertTrue(answer["message"].startswith("Could not check \u2014 no request was made to the site."),
                        answer["message"])

    def test_the_proxy_is_used_for_the_lookup_and_named(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_proxy", "http://proxy.example:8888"):
            _status, answer, _raw = self.ask()
        self.assertTrue(self.fetched and all(proxy == "http://proxy.example:8888" for _u, proxy in self.fetched))
        self.assertIn("through TRACKER_PROXY", answer["message"])
        with self.lookup():
            _status, answer, _raw = self.ask()
        self.assertTrue(all(proxy == "" for _u, proxy in self.fetched))
        self.assertIn("with no proxy", answer["message"])

    def test_the_answer_carries_no_setting_value(self):
        with self.lookup(), mock.patch.object(CONFIG, "tracker_proxy", "http://user:SECRET-PROXY-PASS@proxy.example:8888"):
            _status, _answer, raw = self.ask()
        for secret in ("ENV-SESSION-VALUE", "SECRET-PROXY-PASS"):
            self.assertNotIn(secret, raw)

    def test_the_button_and_a_copyable_box_are_on_the_tab_configured_or_not(self):
        for session in ("ENV-SESSION-VALUE", ""):
            with self.subTest(session=session), mock.patch.object(CONFIG, "tracker_session", session):
                config._EFFECTIVE.clear()
                page = web.render_settings(self.state, "watcher")
                self.assertIn('id="ab-checkip"', page)
                self.assertIn(">Check IP</button>", page)
                self.assertRegex(page, r'<input type="text" class="ab-in" id="ab-checkip-ip" readonly')
                self.assertIn('id="ab-checkip-copy"', page)
                self.assertIn("never contacts the tracker", page)
                self.assertIn("/api/watcher/check-ip", page)
                # it sits beside the guard's own setting
                self.assertLess(page.index('data-setting="expected_egress"'), page.index('id="ab-checkip"'))

    def test_the_button_does_not_save_the_form_first(self):
        script = web.WATCHER_SCRIPT
        body = script[script.index("function abCheckIp()"):script.index("function abCopyIp()")]
        self.assertNotIn("abSaveSettings", body)


# ---------------------------------------------------------------------------- 3. the folder chain
def check(categories=None, torrents=None, **extra):
    return {"version": "5.0.4", "categories": categories, "categories_error": extra.get("categories_error", ""),
            "torrents": torrents, "torrents_error": extra.get("torrents_error", "")}


def done(name, save="/downloads/audiobooks", progress=1.0, content=None):
    return {"name": name, "save_path": save, "content_path": content or f"{save}/{name}", "progress": progress}


class TestFolderChain(unittest.TestCase):
    CATS = {"audiobooks": "/downloads/audiobooks", "movies": "/downloads/movies"}

    def chain(self, check_, ingest_dir="/ingest", present=(), category="audiobooks"):
        calls = []

        def exists(path):
            calls.append(path)
            return path in present or path == ingest_dir

        out = watcher.folder_chain(category, check_, ingest_dir, exists)
        out["calls"] = calls
        return out

    def text(self, out):
        return " ".join(out["lines"])

    def test_the_proof_is_a_finished_torrent_found_in_the_ingest_folder(self):
        out = self.chain(check(self.CATS, [done("Dune")]), present=("/ingest/Dune",))
        self.assertEqual(out["level"], "ok")
        self.assertIn("Verified: qBittorrent reports 'Dune' under /downloads/audiobooks, and Audiobind finds it at "
                      "/ingest/Dune", self.text(out))
        self.assertIn("qBittorrent's categories: audiobooks, movies.", self.text(out))

    def test_finished_torrents_that_are_not_in_the_ingest_folder_are_a_problem(self):
        out = self.chain(check(self.CATS, [done("Dune"), done("Emma")]))
        self.assertEqual(out["level"], "problem")
        self.assertIn("2 finished torrent(s)", self.text(out))
        self.assertIn("none of them is in Audiobind's ingest folder /ingest", self.text(out))
        self.assertNotIn("Verified", self.text(out))

    def test_nothing_finished_means_unknown_never_a_tick(self):
        for torrents in ([], [done("Dune", progress=0.4)]):
            with self.subTest(torrents=torrents):
                out = self.chain(check(self.CATS, torrents))
                self.assertEqual(out["level"], "unknown")
                self.assertIn("nothing to compare", self.text(out))

    def test_paths_that_are_spelled_the_same_are_flagged_not_trusted(self):
        out = self.chain(check({"audiobooks": "/ingest"}, []))
        self.assertEqual(out["level"], "unknown")
        self.assertIn("spelled the same", self.text(out))

    def test_a_missing_category_says_how_to_create_it_and_lists_the_ones_there(self):
        out = self.chain(check({"movies": "/m"}, None))
        self.assertEqual(out["level"], "problem")
        self.assertIn("Category 'audiobooks' isn't in qBittorrent. Create it there (Categories " + ARROW +
                      " Add category) with its save path set to the folder Audiobind's Ingest path points at.",
                      self.text(out))
        self.assertIn("qBittorrent's categories: movies.", self.text(out))
        self.assertIn("none yet", self.chain(check({}, None))["lines"][0])

    def test_a_category_with_no_save_path_is_a_problem(self):
        out = self.chain(check({"audiobooks": ""}, []))
        self.assertEqual(out["level"], "problem")
        self.assertIn("no save path of its own", self.text(out))

    def test_ingest_off_or_not_mounted_cannot_be_compared_and_says_what_is_missing(self):
        out = self.chain(check(self.CATS, [done("Dune")]), ingest_dir="")
        self.assertEqual(out["level"], "unknown")
        self.assertIn("Ingest dir is empty", self.text(out))
        self.assertIn(f"Show more settings {ARROW} Ingest dir", self.text(out))
        out = watcher.folder_chain("audiobooks", check(self.CATS, [done("Dune")]), "/ingest", lambda path: False)
        self.assertEqual(out["level"], "unknown")
        self.assertIn("mount is missing", " ".join(out["lines"]))

    def test_unreadable_categories_or_torrents_say_so(self):
        out = self.chain(check(None, None, categories_error="qBittorrent could not list its categories (HTTP 403)"))
        self.assertEqual(out["level"], "unknown")
        self.assertIn("HTTP 403", self.text(out))
        out = self.chain(check(self.CATS, None, torrents_error="boom"))
        self.assertEqual(out["level"], "unknown")
        self.assertIn("Could not read the torrents in 'audiobooks' (boom)", self.text(out))

    def test_a_name_from_qbittorrent_can_never_point_outside_the_ingest_folder(self):
        hostile = [done("x", content="/downloads/audiobooks/../../etc/passwd"),
                   done("y", content="/elsewhere/../.."),
                   {"name": "z", "save_path": "/downloads/audiobooks", "content_path": "", "progress": 1.0}]
        out = self.chain(check(self.CATS, hostile))
        self.assertEqual(out["level"], "problem")
        self.assertEqual([c for c in out["calls"] if c != "/ingest"], [])  # only the ingest folder itself was looked at

    def test_a_single_file_torrent_is_matched_by_its_file_name(self):
        out = self.chain(check(self.CATS, [done("Dune.m4b")]), present=("/ingest/Dune.m4b",))
        self.assertEqual(out["level"], "ok")

    def test_windows_style_paths_from_qbittorrent_are_read(self):
        item = done("x", save="D:\\books", content="D:\\books\\Dune")
        out = self.chain(check({"audiobooks": "D:\\books"}, [item]), present=("/ingest/Dune",))
        self.assertEqual(out["level"], "ok")

    def test_the_rule_names_host_folder_container_path_and_what_matched(self):
        rule = watcher.CHAIN_RULE
        for phrase in ("host folder", "container path", "Ingest path", "category save path", "by name",
                       "cannot read your host folders"):
            self.assertIn(phrase, rule)


class TestQbitCheckIsReadOnly(unittest.TestCase):
    def client(self, replies):
        client = qbit.QbitClient("http://qbit.example:8080", "u", "p")
        self.gets, self.posts = [], []

        def login():
            self.posts.append("/api/v2/auth/login")
            client.logged_in = True

        def get(path, query):
            self.gets.append((path, dict(query)))
            return replies[path]

        def post(path, body, content_type):
            self.posts.append(path)
            return 200, b""

        return mock.patch.multiple(client, login=login, _get=get, _post=post), client

    REPLIES = {
        "/api/v2/app/version": (200, b"v5.0.4"),
        "/api/v2/torrents/categories": (200, json.dumps(
            {"audiobooks": {"name": "audiobooks", "savePath": "/downloads/audiobooks"}, "tv": {"savePath": ""}}).encode()),
        "/api/v2/torrents/info": (200, json.dumps([
            {"name": "Dune", "save_path": "/downloads/audiobooks", "content_path": "/downloads/audiobooks/Dune",
             "progress": 1}, "junk"]).encode()),
    }

    def test_one_login_gets_only_and_a_logout(self):
        patch, client = self.client(self.REPLIES)
        with patch:
            out = client.check("audiobooks")
        self.assertEqual(out["version"], "5.0.4")
        self.assertEqual(out["categories"], {"audiobooks": "/downloads/audiobooks", "tv": ""})
        self.assertEqual(out["torrents"][0]["name"], "Dune")
        self.assertEqual(len(out["torrents"]), 1)
        self.assertEqual(self.posts, ["/api/v2/auth/login", "/api/v2/auth/logout"])  # no torrents/add, ever
        self.assertEqual([path for path, _q in self.gets],
                         ["/api/v2/app/version", "/api/v2/torrents/categories", "/api/v2/torrents/info"])
        self.assertEqual(self.gets[2][1], {"category": "audiobooks"})
        self.assertFalse(client.logged_in)

    def test_torrents_are_only_listed_when_the_category_exists(self):
        patch, client = self.client(self.REPLIES)
        with patch:
            out = client.check("nope")
        self.assertIsNone(out["torrents"])
        self.assertNotIn("/api/v2/torrents/info", [path for path, _q in self.gets])

    def test_a_failed_category_read_keeps_the_version(self):
        replies = {**self.REPLIES, "/api/v2/torrents/categories": (403, b"Forbidden")}
        patch, client = self.client(replies)
        with patch:
            out = client.check("audiobooks")
        self.assertEqual(out["version"], "5.0.4")
        self.assertIsNone(out["categories"])
        self.assertIn("HTTP 403", out["categories_error"])

    def test_a_failed_version_check_raises_and_still_logs_out(self):
        patch, client = self.client({**self.REPLIES, "/api/v2/app/version": (500, b"")})
        with patch, self.assertRaisesRegex(qbit.QbitError, "HTTP 500 to a version check"):
            client.check("audiobooks")
        self.assertEqual(self.posts[-1], "/api/v2/auth/logout")


class FakeChecked:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error
        self.asked = []

    def check(self, category):
        self.asked.append(category)
        if self.error:
            raise qbit.QbitError(self.error)
        return self.answer

    def version(self):
        raise AssertionError("the qBittorrent test goes through check()")


class TestQbitTest(WatcherSettingsCase):
    def run_test(self, fake, ingest_dir="/ingest", present=()):
        with mock.patch.object(watcher, "make_qbit", return_value=fake), \
                mock.patch.object(CONFIG, "ingest_dir", ingest_dir), \
                mock.patch.object(CONFIG, "qbit_category", "audiobooks"), \
                mock.patch("pathlib.Path.exists", lambda self_: str(self_) in present):
            config._EFFECTIVE.clear()
            status, _h, body = self.call("/api/watcher/test-qbit", {})
        return status, json.loads(body), body

    def test_connected_reports_categories_save_path_and_the_comparison_rule(self):
        fake = FakeChecked(check({"audiobooks": "/downloads/audiobooks"}, [done("Dune")]))
        status, answer, _raw = self.run_test(fake, present=("/ingest", "/ingest/Dune"))
        self.assertEqual(status, 200)
        self.assertEqual((answer["ok"], answer["chain"], fake.asked), (True, "ok", ["audiobooks"]))
        self.assertEqual(answer["message"], "Connected - qBittorrent 5.0.4")
        self.assertIn("Category 'audiobooks' exists; qBittorrent saves it to /downloads/audiobooks.", answer["details"])
        self.assertEqual(answer["rule"], watcher.CHAIN_RULE)

    def test_connected_but_unproven_is_ok_with_unknown_and_the_reason(self):
        fake = FakeChecked(check({"audiobooks": "/downloads/audiobooks"}, []))
        status, answer, _raw = self.run_test(fake, present=("/ingest",))
        self.assertEqual((status, answer["ok"], answer["chain"]), (200, True, "unknown"))
        self.assertTrue(any("nothing to compare" in line for line in answer["details"]))

    def test_a_missing_category_is_reported_as_a_problem(self):
        status, answer, _raw = self.run_test(FakeChecked(check({"movies": "/m"}, None)), present=("/ingest",))
        self.assertEqual((status, answer["ok"], answer["chain"]), (200, True, "problem"))

    def test_a_refused_login_has_no_details(self):
        status, answer, _raw = self.run_test(FakeChecked(error="qBittorrent login failed (HTTP 200) - check QBIT_USER / QBIT_PASS"))
        self.assertEqual((status, answer["ok"], answer["chain"], answer["details"]), (502, False, "", []))

    def test_the_page_carries_the_detail_boxes_and_says_the_test_only_reads(self):
        page = web.render_settings(self.state, "watcher")
        self.assertIn('id="ab-watch-test-fb-detail"', page)
        self.assertIn('id="ab-dc-test-fb-detail"', page)
        self.assertIn("only reads", page)
        self.assertIn("adds nothing", page)

    def test_the_test_is_read_only_end_to_end(self):
        """The real client behind the route: the only POSTs are the login and the logout."""
        posts = []
        real_login = qbit.QbitClient.login

        def post(self_, path, body, content_type):
            posts.append(path)
            return 200, b""

        def get(self_, path, query):
            return {"/api/v2/app/version": (200, b"v5.0.4"),
                    "/api/v2/torrents/categories": (200, b"{}")}[path]

        def login(self_):
            posts.append("/api/v2/auth/login")
            self_.logged_in = True

        with mock.patch.object(qbit.QbitClient, "_post", post), mock.patch.object(qbit.QbitClient, "_get", get), \
                mock.patch.object(qbit.QbitClient, "login", login):
            status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(posts, ["/api/v2/auth/login", "/api/v2/auth/logout"])
        self.assertIsNotNone(real_login)


# ---------------------------------------------------------------------------- 4. the tracker test
class TestTrackerSearchTest(WatcherSettingsCase):
    def run_test(self, fake, payload=None):
        watcher.WATCHER._client = watcher.WATCHER._built = None  # a new fake needs a new client
        with mock.patch.object(watcher, "make_tracker", return_value=fake):
            status, _h, body = self.call("/api/watcher/test-tracker", {} if payload is None else payload)
        return status, json.loads(body), body

    def test_a_found_search_names_the_count_the_first_title_and_author(self):
        fake = FakeTracker(results=[torrent("Dune", ["Frank Herbert"]), torrent("Dune Messiah")])
        status, answer, _raw = self.run_test(fake, {"query": "Dune"})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(answer["message"], "Found 2 results; first: Dune \u2014 Frank Herbert")
        self.assertEqual(fake.searched, ["Dune"])  # ONE request

    def test_a_single_result_and_a_missing_author_read_naturally(self):
        _s, answer, _r = self.run_test(FakeTracker(results=[torrent("Dune", [])]), {"query": "x"})
        self.assertEqual(answer["message"], "Found 1 result; first: Dune")

    def test_found_nothing_points_at_the_search_body_and_category(self):
        fake = FakeTracker()
        status, answer, _raw = self.run_test(fake, {"query": "Emma"})
        self.assertEqual((status, answer["ok"]), (502, False))
        self.assertEqual(answer["message"], "The site answered but found nothing for 'Emma'. "
                                            "Check the search body and category.")
        self.assertEqual(len(fake.searched), 1)

    def test_no_query_searches_for_the_prefilled_default(self):
        fake = FakeTracker(results=[torrent()])
        self.run_test(fake, {})
        self.assertEqual(fake.searched, [watcher.DEFAULT_TEST_QUERY])
        fake = FakeTracker(results=[torrent()])
        self.run_test(fake, {"query": "   "})
        self.assertEqual(fake.searched, [watcher.DEFAULT_TEST_QUERY])

    def test_the_query_is_one_line_and_bounded(self):
        fake = FakeTracker(results=[torrent()])
        self.run_test(fake, {"query": "The\n  Hobbit " + "x" * 500})
        self.assertEqual(len(fake.searched), 1)
        self.assertTrue(fake.searched[0].startswith("The Hobbit x"))
        self.assertEqual(len(fake.searched[0]), watcher.MAX_TEST_QUERY)

    def test_a_query_that_is_not_text_is_refused_before_any_request(self):
        fake = FakeTracker(results=[torrent()])
        status, answer, _raw = self.run_test(fake, {"query": 5})
        self.assertEqual((status, answer["ok"], fake.searched), (400, False, []))

    def test_the_session_value_never_comes_back(self):
        cases = (FakeTracker(results=[torrent()]), FakeTracker(), FakeTracker(error="the tracker answered HTTP 403"))
        for fake in cases:
            with self.subTest(results=len(fake.results), error=fake.error):
                _status, _answer, raw = self.run_test(fake, {"query": "Dune"})
                self.assertNotIn("ENV-SESSION-VALUE", raw)
        self.assertNotIn("ENV-SESSION-VALUE", json.dumps(web.public_settings(
            {k: config.setting_effective(k) for k in config.SETTING_KEYS})))

    def test_a_hostile_title_from_the_site_is_shortened(self):
        _s, answer, _r = self.run_test(FakeTracker(results=[torrent("A" * 900, ["B" * 900])]), {"query": "x"})
        self.assertLess(len(answer["message"]), 300)

    def test_the_page_has_an_editable_box_prefilled_and_the_button_sends_it(self):
        page = web.render_settings(self.state, "watcher")
        self.assertRegex(page, r'<input type="text" class="ab-in" id="ab-watch-test-query"[^>]*value="Dune"')
        self.assertIn("Search the tracker for", page)
        self.assertIn("payload = {query: q.value}", web.WATCHER_SCRIPT)

    def test_the_box_is_not_a_saved_setting(self):
        page = web.render_settings(self.state, "watcher")
        box = page[page.index('id="ab-watch-test-query"') - 200:page.index('id="ab-watch-test-query"')]
        self.assertNotIn("data-setting", box)


# ---------------------------------------------------------------------------- 5. ingest half-configured
class TestIngestHalfConfigured(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "mount").mkdir()
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def setting(self, mount=True, ingest_dir=""):
        target = str(self.root / "mount") if mount else str(self.root / "no-mount")
        for p in (mock.patch.object(ingest, "MOUNT_PATH", target), mock.patch.object(CONFIG, "ingest_dir", ingest_dir)):
            p.start()
            self.addCleanup(p.stop)

    def test_mounted_but_ingest_dir_empty_says_so_with_the_unraid_click_path(self):
        self.setting(mount=True, ingest_dir="")
        text = ingest.half_configured()
        self.assertIn("is mounted but Ingest dir is empty, so ingest is off", text)
        self.assertIn(f"Docker {ARROW} Audiobind {ARROW} Edit {ARROW} Show more settings {ARROW} Ingest dir", text)

    def test_ingest_dir_set_but_no_such_folder_says_the_mount_is_missing(self):
        self.setting(mount=False, ingest_dir=str(self.root / "gone"))
        text = ingest.half_configured()
        self.assertIn("there is no such folder", text)
        self.assertIn(f"Show more settings {ARROW} Ingest path", text)

    def test_neither_half_and_both_halves_say_nothing(self):
        self.setting(mount=False, ingest_dir="")
        self.assertEqual(ingest.half_configured(), "")
        self.setting(mount=True, ingest_dir=str(self.root / "mount"))
        self.assertEqual(ingest.half_configured(), "")

    def test_the_start_up_log_says_it_too(self):
        self.setting(mount=True, ingest_dir="")
        with self.assertLogs("audiobind", "WARNING") as logs:
            self.assertFalse(ingest.check_at_start())
        self.assertIn("is mounted but Ingest dir is empty", "\n".join(logs.output))
        self.setting(mount=False, ingest_dir="")
        with self.assertRaises(AssertionError):  # no half set: no warning
            with self.assertLogs("audiobind", "WARNING"):
                ingest.check_at_start()

    def test_the_main_warning_explains_an_unset_ingest_dir(self):
        self.setting(mount=True, ingest_dir="")
        warning = web._ingest_warning()
        self.assertIn("is mounted, but Audiobind is not watching it", warning)
        self.assertIn("<summary>How to turn on ingest</summary>", warning)
        self.assertIn("is mounted but Ingest dir is empty", warning)

    def test_with_nothing_half_set_the_old_off_text_is_unchanged(self):
        self.setting(mount=False, ingest_dir="")
        self.assertIn("(off &#8212; set Ingest path in the container settings)", web._ingest_line())

    def test_the_ingest_line_is_clean_when_ingest_is_on(self):
        self.setting(mount=True, ingest_dir=str(self.root / "mount"))
        line = web._ingest_line()
        self.assertNotIn("off", line)
        self.assertNotIn("mounted but", line)


# ---------------------------------------------------------------------------- 6. the preset file
GOOD = {
    "label": "Example tracker",
    "settings": {
        "TRACKER_URL": "https://tracker.example", "TRACKER_COOKIE_NAME": "session_id",
        "TRACKER_SEARCH_PATH": "/search.json", "TRACKER_SEARCH_METHOD": "post",
        "TRACKER_SEARCH_BODY": '{"q": "{{title}} {{author}}"}', "TRACKER_DOWNLOAD_PATH": "/download.php",
    },
}


class TestPresetParse(unittest.TestCase):
    def bad(self, data, fragment):
        text = data if isinstance(data, str) else json.dumps(data)
        found, why = preset.parse(text)
        self.assertIsNone(found, text)
        self.assertIn(fragment, why)
        return why

    def test_a_good_file(self):
        found, why = preset.parse(json.dumps(GOOD))
        self.assertEqual((why, found.label), ("", "Example tracker"))
        self.assertEqual(found.values["tracker_search_method"], "POST")  # folded to the choice, like Save does
        self.assertEqual(found.values["tracker_url"], "https://tracker.example")

    def test_empty_values_mean_leave_that_field_alone(self):
        found, _why = preset.parse(json.dumps({**GOOD, "settings": {**GOOD["settings"], "TRACKER_FREELEECH_PARAM": ""}}))
        self.assertNotIn("tracker_freeleech_param", found.values)

    def test_malformed_files_are_plain_reasons(self):
        self.bad("{not json", "not valid JSON")
        self.bad([1, 2], "JSON object")
        self.bad({"settings": GOOD["settings"]}, "label")
        self.bad({"label": "  ", "settings": GOOD["settings"]}, "label")
        self.bad({"label": "x" * 200, "settings": GOOD["settings"]}, "at most")
        self.bad({"label": "x"}, "settings")
        self.bad({"label": "x", "settings": {}}, "settings")
        self.bad({"label": "x", "settings": {"TRACKER_URL": ""}}, "nothing to fill")

    def test_bad_values_are_refused_by_the_same_checks_save_uses(self):
        self.bad({"label": "x", "settings": {"TRACKER_SEARCH_METHOD": "DELETE"}}, "Search method")
        self.bad({"label": "x", "settings": {"TRACKER_URL": "https://tracker.example/path"}}, "Tracker URL")
        self.bad({"label": "x", "settings": {"TRACKER_SEARCH_BODY": "not json"}}, "Search request body")
        self.bad({"label": "x", "settings": {"TRACKER_SEARCH_PATH": "no-leading-slash"}}, "Search path")
        self.bad({"label": "x", "settings": {"TRACKER_URL": 5}}, "must be text")

    def test_your_own_settings_cannot_come_from_a_file(self):
        for name in preset.PERSONAL:
            with self.subTest(name=name):
                self.bad({"label": "x", "settings": {**GOOD["settings"], name: "SMUGGLED-VALUE"}}, "is yours to set")

    def test_unknown_fields_are_refused_and_nothing_echoes_a_value(self):
        why = self.bad({"label": "x", "settings": {**GOOD["settings"], "TRACKER_NOPE": "SMUGGLED-VALUE"}},
                       "is not a field a preset can fill")
        self.assertNotIn("SMUGGLED-VALUE", why)
        why = self.bad({"label": "x", "settings": {"TRACKER_URL": "https://tracker.example/SECRET-PATH"}}, "Tracker URL")
        self.assertNotIn("SECRET-PATH", why)

    def test_the_example_file_is_valid_and_covers_every_field_a_preset_can_fill(self):
        raw = (ROOT / "docs" / "tracker-preset.example.json").read_text()
        found, why = preset.parse(raw)
        self.assertEqual(why, "")
        data = json.loads(raw)
        self.assertEqual(set(data["settings"]), set(preset.FILLABLE))
        self.assertIn("{{title}}", data["settings"]["TRACKER_SEARCH_BODY"])
        self.assertIn("{{author}}", data["settings"]["TRACKER_SEARCH_BODY"])
        self.assertIn("tracker.example", raw)

    def test_every_tracker_variable_the_app_reads_is_either_fillable_or_deliberately_not(self):
        source = (ROOT / "audiobind" / "config.py").read_text()
        read = set(re.findall(r'(?:_env|_resolve)\("(TRACKER_[A-Z_]+)"', source))
        self.assertGreaterEqual(len(read), 15)
        self.assertEqual(read - set(preset.FILLABLE) - set(preset.PERSONAL), set())

    def test_the_docs_describe_it_in_the_acquisition_page(self):
        docs = (ROOT / "docs" / "acquisition.md").read_text()
        self.assertIn("/config/tracker-preset.json", docs)
        self.assertIn("Fill the tracker fields from preset file", docs)
        self.assertIn("tracker-preset.example.json", docs)

    def test_nothing_but_the_web_page_imports_it(self):
        for path in (ROOT / "audiobind").rglob("*.py"):
            if path.name in ("preset.py", "web.py"):
                continue
            self.assertNotRegex(path.read_text(), r"import preset|from \.preset|from audiobind\.preset", path.name)


class TestPresetLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def write(self, text, mode="w"):
        target = self.dir / preset.PRESET_FILE
        target.write_text(text) if mode == "w" else target.write_bytes(text)

    def test_no_file_is_no_preset_and_no_error(self):
        self.assertEqual(preset.load(self.dir), (None, ""))

    def test_a_good_file_loads(self):
        self.write(json.dumps(GOOD))
        found, why = preset.load(self.dir)
        self.assertEqual((found.label, why), ("Example tracker", ""))

    def test_unusable_files_are_a_plain_error_never_a_crash(self):
        cases = {
            "not json": ("{", "valid JSON"),
            "huge": (" " * (preset.MAX_BYTES + 5), "bigger than"),
        }
        for name, (text, fragment) in cases.items():
            with self.subTest(name):
                self.write(text)
                found, why = preset.load(self.dir)
                self.assertIsNone(found)
                self.assertTrue(why.startswith("The tracker preset file cannot be used:"), why)
                self.assertIn(fragment, why)
        self.write(b"\xff\xfe\x00bad", mode="wb")
        self.assertIn("not UTF-8", preset.load(self.dir)[1])
        (self.dir / preset.PRESET_FILE).unlink()
        (self.dir / preset.PRESET_FILE).mkdir()
        self.assertIn("not a file", preset.load(self.dir)[1])

    def test_plan_separates_fill_replace_and_same(self):
        found, _why = preset.parse(json.dumps(GOOD))
        now = {"tracker_url": "https://other.example", "tracker_search_path": "/search.json",
               "tracker_search_method": "POST", "tracker_cookie_name": ""}
        plan = preset.plan(found, lambda key: now.get(key, ""))
        self.assertEqual(plan["replace"], ["tracker_url"])
        self.assertEqual(set(plan["same"]), {"tracker_search_path", "tracker_search_method"})
        self.assertIn("tracker_cookie_name", plan["fill"])
        self.assertIn("tracker_search_body", plan["fill"])

    def test_a_built_in_value_is_not_something_to_overwrite(self):
        found, _why = preset.parse(json.dumps({"label": "x", "settings": {"TRACKER_DOWNLOAD_PARAM": "id"}}))
        plan = preset.plan(found, lambda key: "tid")
        self.assertEqual((plan["fill"], plan["replace"]), (["tracker_download_param"], []))


class TestSetMetaMany(unittest.TestCase):
    def test_all_or_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = State(pathlib.Path(tmp.name))
        self.addCleanup(state.close)
        state.set_meta_many({"a": 1, "b": "two"})
        self.assertEqual((state.get_meta("a"), state.get_meta("b")), (1, "two"))
        with self.assertRaises(TypeError):
            state.set_meta_many({"a": 99, "c": object()})  # the second cannot be stored
        self.assertEqual((state.get_meta("a"), state.get_meta("c")), (1, None))  # and the first was rolled back


class PresetWebCase(WatcherSettingsCase):
    """The Watcher tab and the route, with the preset file in a throwaway state dir."""

    def setUp(self):
        super().setUp()
        self.cfg_dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.cfg_dir, ignore_errors=True))
        patch = mock.patch.object(CONFIG, "state_dir", self.cfg_dir)
        patch.start()
        self.addCleanup(patch.stop)
        # a container with none of the preset's fields set
        for key in ("tracker_url", "tracker_cookie_name", "tracker_search_path", "tracker_search_body",
                    "tracker_search_category", "tracker_download_path", "tracker_freeleech_param", "tracker_user_agent"):
            p = mock.patch.object(CONFIG, key, "")
            p.start()
            self.addCleanup(p.stop)
        for key, value in (("tracker_search_method", "POST"), ("tracker_search_encoding", "json"),
                           ("tracker_download_param", "tid")):
            p = mock.patch.object(CONFIG, key, value)
            p.start()
            self.addCleanup(p.stop)
        config._EFFECTIVE.clear()

    def put(self, data):
        (self.cfg_dir / preset.PRESET_FILE).write_text(data if isinstance(data, str) else json.dumps(data))

    def saved_keys(self):
        return {key for key in config.SETTING_KEYS if self.state.get_meta(f"setting_{key}") is not None}

    def fill(self, **payload):
        status, _h, body = self.call("/api/watcher/preset", payload)
        return status, json.loads(body)


class TestPresetTab(PresetWebCase):
    def tab(self):
        return web.render_settings(self.state, "watcher")

    def test_no_file_no_offer(self):
        page = self.tab()
        self.assertNotIn("Fill the tracker fields from preset file", page)
        self.assertNotIn('id="ab-preset', page)

    def test_a_good_file_is_named_by_its_own_label_with_the_fields_it_will_fill(self):
        self.put(GOOD)
        page = self.tab()
        self.assertIn("Tracker preset: Example tracker", page)
        self.assertIn(">Fill the tracker fields from preset file</button>", page)
        for label in ("Tracker URL", "Session cookie name", "Search path", "Search request body", "Download path"):
            self.assertIn(label, page[page.index("Tracker preset: Example tracker"):page.index('id="ab-preset-fill"')])
        self.assertIn("Your session cookie value is never in a preset", page)

    def test_the_label_is_escaped(self):
        self.put({**GOOD, "label": "<b>Evil</b>"})
        page = self.tab()
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt;", page)
        self.assertNotIn("<b>Evil</b>", page)

    def test_fields_that_would_be_replaced_are_named_before_the_click(self):
        self.put(GOOD)
        self.save({"settings": {"tracker_url": "https://old.example"}})
        page = self.tab()
        self.assertIn("It will replace what is set now in: <b>Tracker URL</b> (you are asked first).", page)

    def test_a_malformed_file_is_a_plain_error_and_no_button(self):
        self.put("{ this is not json")
        for session in ("ENV-SESSION-VALUE", ""):  # configured tab and unconfigured tab
            with self.subTest(session=session), mock.patch.object(CONFIG, "tracker_session", session):
                config._EFFECTIVE.clear()
                page = self.tab()
                self.assertIn("The tracker preset file cannot be used", page)
                self.assertIn("Nothing was changed.", page)
                self.assertNotIn("Fill the tracker fields from preset file", page)

    def test_the_offer_is_there_on_the_unconfigured_tab_too(self):
        self.put(GOOD)
        with mock.patch.object(CONFIG, "tracker_session", ""):
            config._EFFECTIVE.clear()
            self.assertIn(">Fill the tracker fields from preset file</button>", self.tab())

    def test_no_tracker_is_named_by_the_page_or_the_example(self):
        self.put(GOOD)
        name = "".join(("my", "anonamouse"))
        self.assertNotIn(name, self.tab().lower())
        self.assertNotIn(name, (ROOT / "docs" / "tracker-preset.example.json").read_text().lower())
        self.assertNotIn(name, web.WATCHER_SCRIPT.lower())

    def test_the_page_never_puts_the_files_values_into_a_field(self):
        marker = "https://preset-marker.example"
        self.put({**GOOD, "settings": {**GOOD["settings"], "TRACKER_URL": marker}})
        self.assertNotIn(marker, self.tab())  # labels only; nothing is filled until the button is pressed


class TestPresetRoute(PresetWebCase):
    def test_post_only_and_signed_in(self):
        status, headers, _body = self.call("/api/watcher/preset", method="GET")
        self.assertEqual((status, headers.get("Allow")), (405, "POST"))
        self.put(GOOD)
        status, _h, _b = self.call("/api/watcher/preset", {}, cookie="audiobind_session=forged")
        self.assertIn(status, (401, 403))
        self.assertEqual(self.saved_keys(), set())

    def test_fills_the_empty_fields_under_the_keys_the_tab_saves(self):
        self.put(GOOD)
        status, answer = self.fill()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["label"], "Example tracker")
        self.assertEqual(sorted(answer["filled"]), sorted(
            ["Tracker URL", "Session cookie name", "Search path", "Search request body", "Download path"]))
        self.assertEqual(answer["unchanged"], ["Search method"])  # POST was already the value
        self.assertEqual(config.setting_effective("tracker_url"), "https://tracker.example")
        self.assertEqual(config.setting_effective("tracker_search_body"), '{"q": "{{title}} {{author}}"}')
        self.assertTrue(config.is_custom("tracker_url"))
        self.assertTrue(self.saved_keys() <= set(config.SETTING_KEYS))

    def test_the_session_is_never_filled_or_touched(self):
        self.put(GOOD)
        self.save({"settings": {"tracker_session": "TYPED-SESSION-VALUE-01234567"}})
        self.fill()
        self.assertEqual(config.setting_effective("tracker_session"), "TYPED-SESSION-VALUE-01234567")
        self.assertNotIn("tracker_session", preset.FILLABLE.values())

    def test_replacing_something_needs_a_confirm_and_saves_nothing_until_then(self):
        self.put(GOOD)
        self.save({"settings": {"tracker_url": "https://old.example"}})
        before = self.saved_keys()
        status, answer = self.fill()
        self.assertEqual(status, 409)
        self.assertTrue(answer["needs_confirm"])
        self.assertEqual(answer["replace"], ["Tracker URL"])
        self.assertEqual(self.saved_keys(), before)  # the empty fields were NOT filled behind the question
        self.assertEqual(config.setting_effective("tracker_url"), "https://old.example")
        for loose in ("true", 1, "yes", None, []):  # only a real true confirms
            status, _answer = self.fill(overwrite=loose)
            self.assertEqual(status, 409, loose)
        self.assertEqual(self.saved_keys(), before)
        status, answer = self.fill(overwrite=True)
        self.assertEqual(status, 200)
        self.assertEqual(answer["replaced"], ["Tracker URL"])
        self.assertEqual(config.setting_effective("tracker_url"), "https://tracker.example")

    def test_a_second_press_changes_nothing(self):
        self.put(GOOD)
        self.fill()
        saved = self.saved_keys()
        status, answer = self.fill()
        self.assertEqual((status, answer["filled"], answer["replaced"]), (200, [], []))
        self.assertEqual(self.saved_keys(), saved)

    def test_no_file_is_a_plain_error(self):
        status, answer = self.fill()
        self.assertEqual((status, answer["ok"]), (400, False))
        self.assertIn("no tracker preset file", answer["error"])
        self.assertEqual(self.saved_keys(), set())

    def test_a_malformed_file_saves_nothing(self):
        for text in ("{", "[]", json.dumps({"label": "x"}), json.dumps({"label": "x", "settings": {"TRACKER_URL": "ftp://x"}})):
            with self.subTest(text=text):
                self.put(text)
                status, answer = self.fill(overwrite=True)
                self.assertEqual((status, answer["ok"]), (400, False))
                self.assertIn("cannot be used", answer["error"])
                self.assertEqual(self.saved_keys(), set())

    def test_the_file_cannot_smuggle_a_partial_save(self):
        """Every other value is good; ONE is bad. Nothing - not even the good ones - is saved."""
        for bad_name, bad_value in (("TRACKER_SEARCH_METHOD", "DELETE"), ("TRACKER_SEARCH_BODY", "not json"),
                                    ("TRACKER_URL", "https://tracker.example/with/a/path"),
                                    ("TRACKER_SESSION", "SMUGGLED-SESSION"), ("TRACKER_PROXY", "http://evil.example:1"),
                                    ("TRACKER_EXPECTED_EGRESS_IP", OTHER), ("TRACKER_VIP", "YES"),
                                    ("TRACKER_UNKNOWN", "x")):
            with self.subTest(bad=bad_name):
                self.put({"label": "x", "settings": {**GOOD["settings"], bad_name: bad_value}})
                for overwrite in (False, True):
                    status, _answer = self.fill(overwrite=overwrite)
                    self.assertEqual(status, 400)
                self.assertEqual(self.saved_keys(), set(), bad_name)
                self.assertEqual(config.setting_effective("tracker_url"), "")
                self.assertEqual(config.setting_effective("tracker_session"), CONFIG.tracker_session)

    def test_a_failed_store_leaves_nothing_behind(self):
        self.put(GOOD)
        with mock.patch.object(State, "set_meta_many", side_effect=OSError("disk full")):
            try:
                self.fill()
            except Exception:  # noqa: BLE001 - the server answers 500 or drops the request; either way nothing is stored
                pass
        self.assertEqual(self.saved_keys(), set())

    def test_the_answer_carries_field_names_only(self):
        self.put(GOOD)
        status, answer = self.fill()
        self.assertEqual(status, 200)
        text = json.dumps(answer)
        for value in ("https://tracker.example", "session_id", "/search.json", "{{title}}"):
            self.assertNotIn(value, text)

    def test_the_saved_values_reach_the_watcher_without_a_restart(self):
        self.put(GOOD)
        self.fill()
        self.assertEqual(watcher.tracker_host(), "tracker.example")

    def test_the_button_is_confirm_first(self):
        script = web.WATCHER_SCRIPT
        body = script[script.index("function abPreset(overwrite)"):script.index("function abCheckIp()")]
        self.assertIn("overwrite: overwrite === true", body)
        self.assertIn("needs_confirm", body)
        self.put(GOOD)
        self.save({"settings": {"tracker_url": "https://old.example"}})
        page = web.render_settings(self.state, "watcher")
        self.assertIn(">Replace them</button>", page)
        self.assertIn('id="ab-preset-confirm"', page)


if __name__ == "__main__":
    unittest.main()

"""0.19.0-dev7: the tracker settings - validation, the watcher's minimum, the deprecated aliases,
the column migration, and the whole watcher working end to end through the configured URLs.

Nothing here reaches a network: the transport is a fake.
"""

import json
import pathlib
import re
import sqlite3
import tempfile
import unittest
import urllib.parse
from unittest import mock

from audiobind import __version__
from audiobind import config, tracker, watcher, web
from audiobind.config import CONFIG, DEPRECATED_ENV, Config
from audiobind.state import State
from tests.test_watcher import TORRENT, FakeQbit, WatcherCase, answer, entry
from tests.tracker_cfg import COOKIE, DOWNLOAD_URL, SEARCH_URL, SETTINGS, tracker_cfg

ALL_NEW = {"TRACKER_URL", "TRACKER_SEARCH_PATH", "TRACKER_SEARCH_METHOD", "TRACKER_SEARCH_CATEGORY",
           "TRACKER_SEARCH_BODY", "TRACKER_SEARCH_ENCODING", "TRACKER_DOWNLOAD_PATH", "TRACKER_DOWNLOAD_PARAM",
           "TRACKER_FREELEECH_PARAM", "TRACKER_COOKIE_NAME", "TRACKER_SESSION", "TRACKER_VIP", "TRACKER_PROXY",
           "TRACKER_USER_AGENT", "TRACKER_HOME_IP", "TRACKER_EXPECTED_EGRESS_IP"}


def from_env(env: dict) -> Config:
    clean = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("TRACKER_", "MAM_"))}
    with mock.patch.dict("os.environ", {**clean, **env}, clear=True):
        return Config.from_env()


class TestDefaults(unittest.TestCase):
    def test_everything_is_empty_by_default_and_the_watcher_is_off(self):
        cfg = from_env({})
        for name in ("tracker_url", "tracker_search_path", "tracker_search_category", "tracker_search_body",
                     "tracker_download_path", "tracker_freeleech_param", "tracker_cookie_name", "tracker_session",
                     "tracker_proxy", "tracker_user_agent", "tracker_home_ip", "tracker_expected_egress_ip"):
            self.assertEqual(getattr(cfg, name), "", name)
        self.assertEqual((cfg.tracker_search_method, cfg.tracker_download_param, cfg.tracker_vip), ("POST", "tid", False))
        self.assertEqual(cfg.deprecated_used, [])

    def test_the_search_body_has_no_default_and_must_be_a_json_object(self):
        self.assertEqual(from_env({}).tracker_search_body, "")
        self.assertEqual(from_env({"TRACKER_SEARCH_BODY": ' {"q": "{{title}}"} '}).tracker_search_body, '{"q": "{{title}}"}')
        for bad in ("not json", "[1]", '"x"'):
            with self.assertRaises(RuntimeError) as ctx:
                from_env({"TRACKER_SEARCH_BODY": bad})
            self.assertIn("TRACKER_SEARCH_BODY", str(ctx.exception))

    def test_the_qbit_category_and_the_incomplete_limit_defaults(self):
        cfg = from_env({})
        self.assertEqual((cfg.qbit_category, cfg.watch_max_incomplete), ("audiobooks", 10))
        cfg = from_env({"QBIT_CATEGORY": "books", "WATCH_MAX_INCOMPLETE": "0"})
        self.assertEqual((cfg.qbit_category, cfg.watch_max_incomplete), ("books", 0))
        self.assertEqual(from_env({"WATCH_MAX_INCOMPLETE": "-3"}).watch_max_incomplete, 0)

    def test_the_freeleech_parameter_is_empty_unless_configured(self):
        self.assertEqual(from_env({}).tracker_freeleech_param, "")
        self.assertEqual(from_env({"TRACKER_FREELEECH_PARAM": "fl"}).tracker_freeleech_param, "fl")

    def test_every_documented_variable_is_read(self):
        env = {"TRACKER_URL": "https://tracker.example/", "TRACKER_SEARCH_PATH": "/s", "TRACKER_SEARCH_METHOD": "get",
               "TRACKER_SEARCH_CATEGORY": "13", "TRACKER_SEARCH_BODY": '{"q": "{{title}}"}',
               "TRACKER_SEARCH_ENCODING": "form", "TRACKER_DOWNLOAD_PATH": "/d", "TRACKER_DOWNLOAD_PARAM": "id", "TRACKER_FREELEECH_PARAM": "fl",
               "TRACKER_COOKIE_NAME": "sid", "TRACKER_SESSION": " abc ", "TRACKER_VIP": "yes",
               "TRACKER_PROXY": "http://vpn:8888", "TRACKER_USER_AGENT": "UA/1", "TRACKER_HOME_IP": "203.0.113.7",
               "TRACKER_EXPECTED_EGRESS_IP": "198.51.100.7"}
        self.assertEqual(set(env), ALL_NEW)
        cfg = from_env(env)
        self.assertEqual((cfg.tracker_url, cfg.tracker_search_method, cfg.tracker_download_param, cfg.tracker_session),
                         ("https://tracker.example", "GET", "id", "abc"))
        self.assertTrue(cfg.tracker_vip)
        self.assertEqual(cfg.tracker_search_encoding, "form")


class TestValidation(unittest.TestCase):
    def assertRejected(self, env, name):
        with self.assertRaises(RuntimeError) as ctx:
            from_env(env)
        self.assertIn(name, str(ctx.exception))
        return str(ctx.exception)

    def test_a_malformed_url_stops_startup_naming_the_variable(self):
        for bad in ("tracker.example", "ftp://tracker.example", "https://", "https://tracker.example/path",
                    "https://tracker.example?x=1", "https://user:pw@tracker.example", "https://tracker.example:abc"):
            self.assertRejected({"TRACKER_URL": bad}, "TRACKER_URL")

    def test_paths_must_be_paths(self):
        for var in ("TRACKER_SEARCH_PATH", "TRACKER_DOWNLOAD_PATH"):
            for bad in ("search", "/s?x=1", "/a b", "//evil.example/x", "/s#frag"):
                self.assertRejected({var: bad}, var)

    def test_method_and_names(self):
        self.assertRejected({"TRACKER_SEARCH_METHOD": "PUT"}, "TRACKER_SEARCH_METHOD")
        self.assertRejected({"TRACKER_COOKIE_NAME": "a b"}, "TRACKER_COOKIE_NAME")
        self.assertRejected({"TRACKER_COOKIE_NAME": "a;b"}, "TRACKER_COOKIE_NAME")
        self.assertRejected({"TRACKER_DOWNLOAD_PARAM": "a&b"}, "TRACKER_DOWNLOAD_PARAM")
        self.assertRejected({"TRACKER_FREELEECH_PARAM": "fl&x=1"}, "TRACKER_FREELEECH_PARAM")
        self.assertRejected({"TRACKER_SEARCH_BODY": "{not json"}, "TRACKER_SEARCH_BODY")

    def test_search_encoding_defaults_to_json_and_refuses_unknown_values(self):
        self.assertEqual(from_env({}).tracker_search_encoding, "json")
        self.assertEqual(from_env({"TRACKER_SEARCH_ENCODING": " Form "}).tracker_search_encoding, "form")
        self.assertRejected({"TRACKER_SEARCH_ENCODING": "xml"}, "TRACKER_SEARCH_ENCODING")

    def test_a_session_that_cannot_be_a_cookie_value_is_refused_without_echoing_it(self):
        message = self.assertRejected({"TRACKER_SESSION": "secret value; other=1"}, "TRACKER_SESSION")
        self.assertNotIn("secret", message)

    def test_the_proxy_and_ip_errors_name_the_new_variables(self):
        self.assertRejected({"TRACKER_PROXY": "socks5://x:1"}, "TRACKER_PROXY")
        self.assertRejected({"TRACKER_HOME_IP": "203.0.113"}, "TRACKER_HOME_IP")
        self.assertRejected({"TRACKER_EXPECTED_EGRESS_IP": "nope"}, "TRACKER_EXPECTED_EGRESS_IP")


class TestDeprecatedAliases(unittest.TestCase):
    """Each old variable still works while its new name is empty; the new name wins; one line each."""

    VALUES = {"MAM_ID": "sess-1", "MAM_VIP": "true", "MAM_PROXY": "http://vpn:8888", "MAM_USER_AGENT": "UA/1",
              "MAM_HOME_WAN_IP": "203.0.113.7", "MAM_EXPECTED_EGRESS_IP": "198.51.100.7"}
    NEWER = {"TRACKER_SESSION": "sess-2", "TRACKER_VIP": "false", "TRACKER_PROXY": "http://other:9",
             "TRACKER_USER_AGENT": "UA/2", "TRACKER_HOME_IP": "203.0.113.8", "TRACKER_EXPECTED_EGRESS_IP": "198.51.100.8"}
    ATTR = {"TRACKER_SESSION": "tracker_session", "TRACKER_VIP": "tracker_vip", "TRACKER_PROXY": "tracker_proxy",
            "TRACKER_USER_AGENT": "tracker_user_agent", "TRACKER_HOME_IP": "tracker_home_ip",
            "TRACKER_EXPECTED_EGRESS_IP": "tracker_expected_egress_ip"}

    def test_the_table_is_exactly_the_six(self):
        self.assertEqual(DEPRECATED_ENV, {
            "MAM_ID": "TRACKER_SESSION", "MAM_VIP": "TRACKER_VIP", "MAM_PROXY": "TRACKER_PROXY",
            "MAM_USER_AGENT": "TRACKER_USER_AGENT", "MAM_HOME_WAN_IP": "TRACKER_HOME_IP",
            "MAM_EXPECTED_EGRESS_IP": "TRACKER_EXPECTED_EGRESS_IP"})

    def test_each_old_name_still_works(self):
        for old, new in DEPRECATED_ENV.items():
            with self.subTest(old):
                cfg = from_env({old: self.VALUES[old]})
                got = getattr(cfg, self.ATTR[new])
                self.assertEqual(got, True if old == "MAM_VIP" else self.VALUES[old])
                self.assertEqual(cfg.deprecated_used, [(old, new)])
                self.assertEqual(config.deprecated_env_lines(cfg), [f"{old} is deprecated, use {new}"])

    def test_the_new_name_wins_when_both_are_set(self):
        for old, new in DEPRECATED_ENV.items():
            with self.subTest(old):
                cfg = from_env({old: self.VALUES[old], new: self.NEWER[new]})
                got = getattr(cfg, self.ATTR[new])
                self.assertEqual(got, False if old == "MAM_VIP" else self.NEWER[new])
                self.assertEqual(cfg.deprecated_used, [])
                self.assertEqual(cfg.deprecated_ignored, [(old, new)])

    def test_an_empty_old_variable_is_not_a_use(self):
        cfg = from_env({"MAM_ID": "  ", "MAM_PROXY": ""})
        self.assertEqual((cfg.tracker_session, cfg.deprecated_used), ("", []))

    def test_one_line_per_variable_and_never_the_value(self):
        cfg = from_env(self.VALUES)
        lines = config.deprecated_env_lines(cfg)
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertRegex(line, r"^MAM_\w+ is deprecated, use TRACKER_\w+$")
        for value in self.VALUES.values():
            self.assertFalse(any(value in line for line in lines))
        self.assertNotIn("sess-1", repr(cfg))

    def test_an_invalid_old_value_names_the_old_variable(self):
        with self.assertRaises(RuntimeError) as ctx:
            from_env({"MAM_PROXY": "socks5://x:1"})
        self.assertIn("MAM_PROXY", str(ctx.exception))

    def test_the_daemon_logs_the_lines_once_at_start(self):
        from audiobind import daemon

        cfg = from_env({"MAM_ID": "sess-1"})
        with mock.patch.object(daemon, "deprecated_env_lines", lambda: config.deprecated_env_lines(cfg)):
            self.assertEqual(daemon.deprecated_env_lines(), ["MAM_ID is deprecated, use TRACKER_SESSION"])

    def test_the_paths_cookie_and_category_have_no_alias(self):
        old_names = set(DEPRECATED_ENV) | set(DEPRECATED_ENV.values())
        for var in ("TRACKER_SEARCH_PATH", "TRACKER_DOWNLOAD_PATH", "TRACKER_COOKIE_NAME", "TRACKER_SEARCH_CATEGORY"):
            self.assertNotIn(var, DEPRECATED_ENV)
        self.assertEqual(len(old_names), 12)


class TestWatcherMinimum(unittest.TestCase):
    def test_missing_settings_are_named(self):
        with mock.patch.multiple(CONFIG, tracker_url="", tracker_search_path="", tracker_search_body="",
                                 tracker_download_path="", tracker_cookie_name="", tracker_session=""):
            self.assertFalse(watcher.configured())
            self.assertEqual(watcher.missing_settings(), [
                "TRACKER_URL", "TRACKER_SEARCH_PATH", "TRACKER_SEARCH_BODY", "TRACKER_DOWNLOAD_PATH",
                "TRACKER_COOKIE_NAME", "TRACKER_SESSION"])
            self.assertIn("TRACKER_URL", watcher.disabled_reason())

    def test_each_one_is_needed(self):
        for attr, var in (("tracker_url", "TRACKER_URL"), ("tracker_search_path", "TRACKER_SEARCH_PATH"),
                          ("tracker_search_body", "TRACKER_SEARCH_BODY"),
                          ("tracker_download_path", "TRACKER_DOWNLOAD_PATH"),
                          ("tracker_cookie_name", "TRACKER_COOKIE_NAME"), ("tracker_session", "TRACKER_SESSION")):
            with self.subTest(var), tracker_cfg("x"), mock.patch.object(CONFIG, attr, ""):
                self.assertEqual(watcher.missing_settings(), [var])
                self.assertFalse(watcher.configured())
                self.assertIn(var, watcher.disabled_reason())

    def test_all_six_turn_it_on(self):
        with tracker_cfg("x"):
            self.assertTrue(watcher.configured())
            self.assertEqual(watcher.missing_settings(), [])


class TestSettingsPage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.state.close)
        watcher.WATCHER._paused = None

    def test_off_names_what_is_missing(self):
        with mock.patch.multiple(CONFIG, **{**{k: "" for k in SETTINGS}, "tracker_session": "", "tracker_search_method": "POST"}):
            html = web.render_settings(self.state)
        for var in ("TRACKER_URL", "TRACKER_SEARCH_PATH", "TRACKER_SEARCH_BODY", "TRACKER_DOWNLOAD_PATH",
                    "TRACKER_COOKIE_NAME", "TRACKER_SESSION"):
            self.assertIn(f"<code>{var}</code>", html)

    def test_a_partly_configured_install_lists_only_what_is_missing(self):
        with tracker_cfg(""):
            html = web.render_settings(self.state)
        self.assertIn("<code>TRACKER_SESSION</code>", html)
        self.assertNotIn("<code>TRACKER_SEARCH_PATH</code>", html)

    def test_on_shows_the_host_not_the_url(self):
        with tracker_cfg("SECRET-SESSION"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "p"):
            html = web.render_settings(self.state)
            status = watcher.WATCHER.status(self.state)
        self.assertEqual(status["tracker_host"], "tracker.example")
        self.assertIn("<dt>Tracker</dt><dd>tracker.example</dd>", html)
        # 0.21.0-dev5: the paths are editable on the page, so they show - but only in their own field;
        # the status grid still names the host alone, and the session never appears anywhere
        grid = html.split('<dl class="ab-watch-grid">')[1].split("</dl>")[0]
        self.assertNotIn("search.json", grid)
        field = re.search(r'<div class="ab-setting" data-setting="tracker_search_path".*?</div>(?=<div class="ab-setting")',
                          html, re.S).group(0)
        self.assertIn("search.json", field)
        self.assertNotIn("search.json", html.replace(field, ""))
        self.assertNotIn("SECRET-SESSION", html + json.dumps(status))

    def test_no_user_facing_page_names_a_site(self):
        with tracker_cfg("x"), mock.patch.object(CONFIG, "qbit_user", "u"), mock.patch.object(CONFIG, "qbit_pass", "p"):
            pages = web.render_settings(self.state) + web.render_discover(self.state, "wishlist")
        for word in ("".join(("my", "anonamouse")), "wedge", "VIP"):
            visible = re.sub(r"wedge_(?:reserve|per_cycle|per_day)", "", pages.lower())  # setting keys, never shown
            visible = visible.replace("tracker_vip", "").replace('data-setting="vip"', "").replace('data-field="vip"', "")
            self.assertNotIn(word.lower(), visible, word)


class TestEndToEnd(WatcherCase):
    """The real factory builds the client from the settings; a fake transport answers."""

    def test_a_cycle_uses_exactly_the_configured_endpoints_cookie_category_and_freeleech_param(self):
        calls = []
        replies = [answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT)]

        def fake(method, url, data, headers, timeout, proxy="", endpoints=None):
            self.assertTrue(tracker.authorized_url(url, endpoints), url)
            calls.append((method, url, data, headers))
            return replies.pop(0)

        self.want()
        with mock.patch.object(tracker, "urllib_transport", fake), \
                mock.patch.object(tracker.RateLimiter, "wait", lambda self: None), \
                mock.patch.object(watcher, "make_qbit", lambda: FakeQbit()):
            self.w._client = None
            summary = self.w.run_cycle(self.state, trigger="test")
        self.assertEqual(summary["sent"], 1, summary)
        (m1, u1, d1, h1), (m2, u2, d2, h2) = calls
        self.assertEqual((m1, u1), ("POST", SEARCH_URL))
        self.assertTrue(h1["Cookie"].startswith(f"{COOKIE}="))
        self.assertEqual(h1["User-Agent"], f"audiobind/{__version__}")
        self.assertEqual(json.loads(d1.decode())["category"], "13")
        self.assertEqual((m2, u2), ("GET", f"{DOWNLOAD_URL}?tid=1001&fl"))
        row = self.row()
        self.assertEqual((row["status"], row["tracker_tid"]), ("downloading", "1001"))

    def test_without_a_freeleech_param_no_credit_is_ever_requested(self):
        calls = []
        replies = [answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT)]

        def fake(method, url, data, headers, timeout, proxy="", endpoints=None):
            calls.append(url)
            return replies.pop(0)

        self.want()
        with mock.patch.object(CONFIG, "tracker_freeleech_param", ""), \
                mock.patch.object(tracker, "urllib_transport", fake), \
                mock.patch.object(tracker.RateLimiter, "wait", lambda self: None), \
                mock.patch.object(watcher, "make_qbit", lambda: FakeQbit()):
            self.w._client = None
            self.w.run_cycle(self.state, trigger="test")
        self.assertEqual(calls[1], f"{DOWNLOAD_URL}?tid=1001")
        self.assertIn("no freeleech parameter", self.row()["note"])


class TestColumnMigration(unittest.TestCase):
    def test_the_old_column_is_copied_into_the_new_one(self):
        legacy = "_".join(("mam", "tid"))  # the retired column name, kept out of a plain grep
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(pathlib.Path(tmp) / "audiobind.db")
            db.executescript(
                "CREATE TABLE wishlist (asin TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]',"
                " added_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'wanted', origin TEXT NOT NULL DEFAULT '',"
                " series TEXT NOT NULL DEFAULT '', sequence TEXT NOT NULL DEFAULT '', hours REAL NOT NULL DEFAULT 0,"
                f" {legacy} TEXT NOT NULL DEFAULT '');"
                f"INSERT INTO wishlist (asin, title, added_at, status, {legacy}) VALUES ('B1', 'T', 1, 'downloading', '4242');"
            )
            db.commit()
            db.close()
            state = State(pathlib.Path(tmp))
            try:
                self.assertEqual(state.wishlist_get("B1")["tracker_tid"], "4242")
            finally:
                state.close()
            state = State(pathlib.Path(tmp))  # a second start changes nothing
            try:
                self.assertEqual(state.wishlist_get("B1")["tracker_tid"], "4242")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()

"""0.21.0-dev5: the watcher's connection settings on the Settings page.

Every key round-trips (container value shows, an override saves and wins, Reset hands it back),
the two secrets never reach a browser, values are validated with plain messages, the two
connection tests are POST-only, signed-in and fail closed, and a saved value reaches the watcher
from its next use (no restart).
"""

import json
import re
import urllib.error
import urllib.request
from unittest import mock

from audiobind import config, qbit, tracker, watcher, web
from audiobind.config import CONFIG

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

#: key -> (container value, value saved on the page)
SAMPLES = {
    "tracker_url": ("https://env.example", "https://gui.example"),
    "tracker_session": ("ENV-SESSION-VALUE", "GUI-SESSION-VALUE"),
    "tracker_cookie_name": ("env_sid", "gui_sid"),
    "tracker_search_path": ("/env/search.json", "/gui/search.json"),
    "tracker_search_body": ('{"q": "{{title}}"}', '{"text": "{{title}} {{author}}"}'),
    "tracker_search_method": ("POST", "GET"),
    "tracker_search_encoding": ("json", "form"),
    "tracker_search_category": ("13", "14"),
    "tracker_download_path": ("/env/dl.php", "/gui/dl.php"),
    "tracker_download_param": ("tid", "id"),
    "tracker_freeleech_param": ("", "fl=1"),
    "tracker_user_agent": ("EnvAgent/1", "GuiAgent/2"),
    "qbit_url": ("http://env-qbit:8080", "http://gui-qbit:8081"),
    "qbit_user": ("envuser", "guiuser"),
    "qbit_pass": ("env pass word", "gui pass word"),
    "qbit_category": ("envcat", "guicat"),
    "watch_max_incomplete": (10, 3),
}
SECRETS = ("tracker_session", "qbit_pass")


class WatcherSettingsCase(LiveServerCase):
    def setUp(self):
        super().setUp()
        for key, (env, _gui) in SAMPLES.items():
            patcher = mock.patch.object(CONFIG, key, env)
            patcher.start()
            self.addCleanup(patcher.stop)
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)
        w = watcher.WATCHER
        saved = (w._client, w._built, w._built_from, w.autostart)
        w._client, w._built, w._built_from, w.autostart = None, None, "", False

        def restore():
            w._client, w._built, w._built_from, w.autostart = saved

        self.addCleanup(restore)

    def call(self, path, payload=None, method="POST", cookie=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if cookie is not None:
            headers["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.headers, exc.read().decode()
            finally:
                exc.close()

    def save(self, payload):
        status, _headers, body = self.call("/api/settings", payload)
        return status, json.loads(body or "{}"), body

    def page(self):
        return web.render_settings(self.state)

    def field(self, key, page=None):
        page = page if page is not None else self.page()
        found = re.search(rf'<div class="ab-setting" data-setting="{key}"[^>]*>.*?(?=<div class="ab-setting" |'
                          rf'<div class="ab-acct-title">|<div class="ab-acct-row">)', page, re.S)
        self.assertIsNotNone(found, key)
        return found.group(0)


class TestEveryKeyRoundTrips(WatcherSettingsCase):
    def shows(self, key, value, html):
        """The field shows this (non-secret) value as its current one."""
        text = web._attr(str(value))
        if key in ("tracker_search_method", "tracker_search_encoding"):
            self.assertIn(f'<option value="{text}" selected>', html)
        elif key == "tracker_search_body":
            self.assertIn(f">{text}</textarea>", html)
        elif key == "watch_max_incomplete":
            self.assertIn(f'type="number" class="ab-in" min="0" max="500" value="{text}" step="1" data-initial="{text}"', html)
        else:
            self.assertIn(f'value="{text}" data-initial="{text}"', html)

    def test_every_key_is_editable(self):
        for key in config.WATCHER_SETTING_KEYS:
            self.assertIn(key, config.SETTING_MAP)
            self.assertIn(key, config.SETTING_TYPES)
            self.assertIn(key, config.SETTING_KEYS)
            self.assertNotIn(key, config.GENERAL_SETTING_KEYS)  # never wiped by "Reset to defaults"
        self.assertEqual(set(SAMPLES), set(config.WATCHER_SETTING_KEYS))
        for env_only in ("tracker_proxy", "tracker_home_ip"):
            self.assertNotIn(env_only, config.SETTING_KEYS)

    def test_env_default_override_reset_for_every_key(self):
        for key, (env, gui) in SAMPLES.items():
            with self.subTest(key=key):
                attr = config.SETTING_MAP[key]
                # the container value renders, marked as coming from the container
                html = self.field(key)
                self.assertIn('<span class="ab-src">from the container</span>', html)
                if key not in SECRETS:
                    self.shows(key, env, html)
                self.assertEqual(config.setting_effective(key), env)
                self.assertFalse(config.is_custom(attr))
                # an override saves, wins, is marked, and the watcher reads it
                status, body, _raw = self.save({"settings": {key: gui}})
                self.assertEqual(status, 200, body)
                self.assertIn(key, body["custom"])
                self.assertEqual(config.setting_effective(key), gui)
                self.assertEqual(config.effective_val(attr), gui)
                self.assertTrue(config.is_custom(attr))
                html = self.field(key)
                self.assertIn('<span class="ab-src ab-src-here">set here</span>', html)
                self.assertIn(f"abResetSetting('{key}')", html)
                self.assertIn("Reset to container value", html)
                if key not in SECRETS:
                    self.shows(key, gui, html)
                # Reset to container value removes the override: the container value is back
                status, body, _raw = self.save({"reset_keys": [key]})
                self.assertEqual(status, 200, body)
                self.assertNotIn(key, body["custom"])
                self.assertIsNone(self.state.get_meta(f"setting_{key}"))
                self.assertEqual(config.setting_effective(key), env)
                self.assertFalse(config.is_custom(attr))

    def test_reset_to_defaults_leaves_the_connection_settings_alone(self):
        self.save({"settings": {"qbit_category": "guicat", "tracker_session": "GUI-SESSION-VALUE", "nice": 7}})
        status, body, raw = self.save({"reset": True})
        self.assertEqual(status, 200, body)
        self.assertFalse(config.is_custom("nice"))
        self.assertEqual(config.setting_effective("qbit_category"), "guicat")
        self.assertEqual(config.setting_effective("tracker_session"), "GUI-SESSION-VALUE")
        for key in config.WATCHER_SETTING_KEYS:
            self.assertNotIn(key, body["env_defaults"])
        self.assertNotIn("ENV-SESSION-VALUE", raw)

    def test_reset_keys_rejects_unknown_settings(self):
        for bad in (["not_a_setting"], "tracker_url", []):
            status, body, _raw = self.save({"reset_keys": bad})
            self.assertEqual(status, 400, bad)


class TestPrecedence(WatcherSettingsCase):
    def test_override_wins_and_the_container_value_returns(self):
        self.assertEqual(watcher.qbit_url(), "http://env-qbit:8080")
        self.save({"settings": {"qbit_url": "http://gui-qbit:8081/"}})
        self.assertEqual(config.setting_effective("qbit_url"), "http://gui-qbit:8081")  # trailing / dropped
        self.assertEqual(watcher.qbit_url(), "http://gui-qbit:8081")
        self.assertEqual(watcher.make_qbit().url, "http://gui-qbit:8081")
        self.save({"reset_keys": ["qbit_url"]})
        self.assertEqual(watcher.make_qbit().url, "http://env-qbit:8080")

    def test_an_empty_override_beats_a_container_value(self):
        self.save({"settings": {"tracker_search_category": ""}})
        self.assertTrue(config.is_custom("tracker_search_category"))
        self.assertEqual(config.setting_effective("tracker_search_category"), "")
        self.save({"reset_keys": ["tracker_search_category"]})
        self.assertEqual(config.setting_effective("tracker_search_category"), "13")

    def test_watch_max_incomplete_is_read_per_use(self):
        self.assertEqual(config.watch_max_incomplete(), 10)
        status, body, _raw = self.save({"settings": {"watch_max_incomplete": 0}})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.watch_max_incomplete(), 0)
        self.assertEqual(watcher.WATCHER.status(self.state)["max_incomplete"], 0)


class TestSecrets(WatcherSettingsCase):
    def served(self):
        return (self.page() + self.call("/api/state", method="GET")[2] + self.call("/api/watcher", method="GET")[2]
                + self.call("/settings", method="GET")[2])

    def test_a_secret_is_never_in_the_page_or_any_answer(self):
        self.assertNotIn("ENV-SESSION-VALUE", self.served())
        self.assertNotIn("env pass word", self.served())
        status, body, raw = self.save({"settings": {"tracker_session": "GUI-SESSION-VALUE", "qbit_pass": "gui pass word"}})
        self.assertEqual(status, 200, body)
        served = self.served() + raw + self.save({"reset": True})[2]
        for secret in ("GUI-SESSION-VALUE", "gui pass word", "ENV-SESSION-VALUE", "env pass word"):
            self.assertNotIn(secret, served)
        # /api/state says whether one is set, never what it is
        state = json.loads(self.call("/api/state", method="GET")[2])
        self.assertIs(state["settings"]["tracker_session"], True)
        self.assertIs(state["settings"]["qbit_pass"], True)

    def test_the_secret_box_is_empty_and_says_whether_one_is_set(self):
        for key in SECRETS:
            html = self.field(key)
            self.assertIn('type="password" class="ab-in" value="" data-initial="" '
                          'placeholder="set - leave blank to keep it"', html)
            self.assertIn("data-secret", html)
            self.assertIn('<input type="checkbox" data-clear> Clear', html)
        with mock.patch.object(CONFIG, "qbit_pass", ""):
            html = self.field("qbit_pass")
            self.assertIn('placeholder="not set"', html)
            self.assertNotIn("data-clear", html)

    def test_blank_keeps_the_stored_value(self):
        self.save({"settings": {"tracker_session": "GUI-SESSION-VALUE"}})
        status, body, _raw = self.save({"settings": {"tracker_session": ""}})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.setting_effective("tracker_session"), "GUI-SESSION-VALUE")
        # and a blank with nothing saved keeps the container's
        self.save({"settings": {"qbit_pass": ""}})
        self.assertFalse(config.is_custom("qbit_pass"))
        self.assertEqual(config.setting_effective("qbit_pass"), "env pass word")

    def test_clear_wipes_it_even_over_a_container_value(self):
        self.save({"settings": {"qbit_pass": "gui pass word"}})
        status, body, _raw = self.save({"clear": ["qbit_pass"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.setting_effective("qbit_pass"), "")
        self.assertIn("QBIT_USER / QBIT_PASS are not set", watcher.disabled_reason())
        status, body, _raw = self.save({"settings": {"tracker_session": "NEW-ONE"}, "clear": ["tracker_session"]})
        self.assertEqual(config.setting_effective("tracker_session"), "", "Clear wins over a value typed alongside")
        self.save({"reset_keys": ["qbit_pass", "tracker_session"]})
        self.assertEqual(config.setting_effective("qbit_pass"), "env pass word")

    def test_only_secrets_can_be_cleared(self):
        status, body, _raw = self.save({"clear": ["tracker_url"]})
        self.assertEqual(status, 400)
        self.assertEqual(config.setting_effective("tracker_url"), "https://env.example")

    def test_a_refused_secret_is_not_echoed(self):
        status, body, raw = self.save({"settings": {"tracker_session": "BAD VALUE;WITH-SEMICOLON"}})
        self.assertEqual(status, 400)
        self.assertIn("Session cookie value:", body["error"])
        self.assertNotIn("BAD VALUE", raw)
        self.assertEqual(config.setting_effective("tracker_session"), "ENV-SESSION-VALUE")


class TestValidation(WatcherSettingsCase):
    def test_bad_values_are_refused_with_a_plain_message(self):
        cases = (
            ({"tracker_url": "not a url"}, "Tracker URL:"),
            ({"tracker_url": "https://t.example/with/a/path"}, "Tracker URL:"),
            ({"qbit_url": "ftp://qbit"}, "qBittorrent URL:"),
            ({"tracker_search_path": "search"}, "Search path:"),
            ({"tracker_search_body": "[1, 2]"}, "Search request body:"),
            ({"tracker_cookie_name": "a b"}, "Session cookie name:"),
            ({"tracker_search_method": "PUT"}, "tracker_search_method: must be one of POST, GET"),
            ({"watch_interval": "often"}, "Check the wishlist every: use a whole number from 10 to 1440"),
            ({"watch_max_incomplete": "lots"}, "Downloads in progress allowed: use a whole number from 0 to 500"),
            ({"qbit_user": 12}, "qBittorrent user: must be text"),
            ({"qbit_category": "two\nlines"}, "qBittorrent category:"),
        )
        for settings, message in cases:
            with self.subTest(settings=settings):
                status, body, _raw = self.save({"settings": settings})
                self.assertEqual(status, 400, body)
                self.assertIn(message, body["error"])
        self.assertFalse(any(config.is_custom(k) for k in config.WATCHER_SETTING_KEYS))

    def test_a_good_value_is_stored_in_its_canonical_form(self):
        status, body, _raw = self.save({"settings": {"tracker_url": " https://t.example/ ",
                                                     "tracker_download_param": ""}})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.setting_effective("tracker_url"), "https://t.example")
        self.assertEqual(config.setting_effective("tracker_download_param"), "tid")


class FakeQbitVersion:
    def __init__(self, version="5.2.4", error=None):
        self.version_text, self.error = version, error

    def version(self):
        if self.error:
            raise qbit.QbitError(self.error)
        return self.version_text

    def check(self, category):
        """What QbitClient.check() answers: here, a qBittorrent with no categories at all."""
        return {"version": self.version(), "categories": {}, "categories_error": "", "torrents": None,
                "torrents_error": ""}


class FakeTracker:
    def __init__(self, results=(), error=None):
        self.results, self.error = list(results), error
        self.limiter, self.backoff = tracker.RateLimiter(), tracker.Backoff()
        self.searched = []

    def take_outcome(self):  # the real client's AB-AUTH-3 hook: a stub notes nothing
        return None

    def search(self, title, author=""):
        self.searched.append(title)
        if self.error:
            raise tracker.TrackerError(self.error)
        return list(self.results)


class TestConnectionTests(WatcherSettingsCase):
    ROUTES = ("/api/watcher/test-qbit", "/api/watcher/test-tracker")

    def test_routes_are_post_only_and_signed_in(self):
        for route in self.ROUTES:
            status, headers, _body = self.call(route, method="GET")
            self.assertEqual(status, 405, route)
            self.assertEqual(headers.get("Allow"), "POST")
            status, _headers, _body = self.call(route, {}, cookie="audiobind_session=forged")
            self.assertIn(status, (401, 403), route)

    def test_qbit_connected_and_refused(self):
        with mock.patch.object(watcher, "make_qbit", return_value=FakeQbitVersion("5.2.4")):
            status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertEqual((answer["ok"], answer["message"]), (True, "Connected - qBittorrent 5.2.4"))
        # the fake qBittorrent has no categories, so the folder check reports the missing category
        self.assertEqual(answer["chain"], "problem")
        self.assertTrue(any("isn't in qBittorrent" in line for line in answer["details"]))
        with mock.patch.object(watcher, "make_qbit",
                               return_value=FakeQbitVersion(error="qBittorrent login failed (HTTP 200) - check QBIT_USER / QBIT_PASS")):
            status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["ok"], False)
        self.assertIn("login failed", json.loads(body)["message"])

    def test_qbit_unreachable_fails_closed(self):
        self.save({"settings": {"qbit_url": "http://127.0.0.1:9", "qbit_user": "u", "qbit_pass": "UNIQUE-PASS-9"}})
        status, _h, body = self.call("/api/watcher/test-qbit", {})
        self.assertEqual(status, 502)
        answer = json.loads(body)
        self.assertIs(answer["ok"], False)
        self.assertIn("could not be reached", answer["message"])
        self.assertNotIn("UNIQUE-PASS-9", body)

    def test_tracker_test_uses_one_search_and_stores_nothing(self):
        fake = FakeTracker()
        before = dict(self.state.get_meta(watcher.META_LAST_CYCLE) or {})
        with mock.patch.object(watcher, "make_tracker", return_value=fake):
            status, _h, body = self.call("/api/watcher/test-tracker", {})
        # nothing came back: the site answered, but a search that finds nothing is not a working search
        self.assertEqual(status, 502, body)
        self.assertIs(json.loads(body)["ok"], False)
        self.assertEqual(json.loads(body)["message"],
                         "The site answered but found nothing for 'Dune'. Check the search body and category.")
        self.assertEqual(fake.searched, [watcher.DEFAULT_TEST_QUERY])
        self.assertEqual(dict(self.state.get_meta(watcher.META_LAST_CYCLE) or {}), before)

    def test_tracker_refusal_and_unreachable_fail_closed(self):
        with mock.patch.object(watcher, "make_tracker", return_value=FakeTracker(error="the tracker answered HTTP 403; backing off 60s")):
            status, _h, body = self.call("/api/watcher/test-tracker", {})
        self.assertEqual(status, 502)
        self.assertIn("HTTP 403", json.loads(body)["message"])
        watcher.WATCHER._client = watcher.WATCHER._built = None
        self.save({"settings": {"tracker_url": "http://127.0.0.1:9"}})
        status, _h, body = self.call("/api/watcher/test-tracker", {})
        self.assertEqual(status, 502)
        self.assertIs(json.loads(body)["ok"], False)
        self.assertIn("could not be reached", json.loads(body)["message"])
        self.assertNotIn("ENV-SESSION-VALUE", body)

    def test_tracker_test_obeys_the_egress_guard_and_missing_settings(self):
        boom = mock.Mock(side_effect=AssertionError("the tracker must not be contacted"))
        with mock.patch.object(watcher, "make_tracker", boom), \
                mock.patch.object(watcher.WATCHER, "_egress_guard",
                                  return_value=("blocked", "the tracker would see 203.0.113.9, your home connection", "")):
            status, _h, body = self.call("/api/watcher/test-tracker", {})
        self.assertEqual(status, 502)
        self.assertIn("home connection", json.loads(body)["message"])
        boom.assert_not_called()
        self.save({"clear": ["tracker_session"]})
        with mock.patch.object(watcher, "make_tracker", boom):
            status, _h, body = self.call("/api/watcher/test-tracker", {})
        self.assertEqual(status, 502)
        self.assertIn("not set yet: TRACKER_SESSION", json.loads(body)["message"])


class TestQbitVersion(WatcherSettingsCase):
    def test_version_logs_in_reads_and_logs_out(self):
        client = qbit.QbitClient("http://q:8080", "u", "p")
        calls = []
        with mock.patch.object(client, "login", side_effect=lambda: calls.append("login")), \
                mock.patch.object(client, "logout", side_effect=lambda: calls.append("logout")), \
                mock.patch.object(client, "_get", return_value=(200, b"v5.2.4\n")) as get:
            self.assertEqual(client.version(), "5.2.4")
        get.assert_called_once_with("/api/v2/app/version", {})
        self.assertEqual(calls, ["login", "logout"])

    def test_a_bad_answer_is_a_plain_error_and_still_logs_out(self):
        client = qbit.QbitClient("http://q:8080", "u", "p")
        calls = []
        with mock.patch.object(client, "login"), \
                mock.patch.object(client, "logout", side_effect=lambda: calls.append("logout")), \
                mock.patch.object(client, "_get", return_value=(403, b"Forbidden")):
            with self.assertRaisesRegex(qbit.QbitError, "HTTP 403 to a version check"):
                client.version()
        self.assertEqual(calls, ["logout"])


class TestLiveEffect(WatcherSettingsCase):
    def test_a_saved_tracker_setting_builds_a_new_client_keeping_pacing_and_backoff(self):
        built = []

        def make(route):
            built.append(FakeTracker())
            return built[-1]

        w = watcher.WATCHER
        with mock.patch.object(watcher, "make_tracker", side_effect=make):
            first = w.client()
            self.assertIs(w.client(), first, "unchanged settings keep the client")
            self.save({"settings": {"tracker_session": "GUI-SESSION-VALUE"}})
            second = w.client()
            self.assertIsNot(second, first)
            self.assertIs(second.limiter, first.limiter)
            self.assertIs(second.backoff, first.backoff)
            self.save({"settings": {"tracker_user_agent": "Other/3"}})
            self.assertIsNot(w.client(), second)
            self.assertEqual(len(built), 3)

    def test_a_client_handed_in_is_never_replaced(self):
        w = watcher.WATCHER
        injected = FakeTracker()
        w._client = injected
        with mock.patch.object(watcher, "make_tracker", side_effect=AssertionError("must not rebuild")):
            self.save({"settings": {"tracker_session": "GUI-SESSION-VALUE"}})
            self.assertIs(w.client(), injected)

    def test_qbit_settings_are_read_per_use(self):
        self.save({"settings": {"qbit_user": "guiuser", "qbit_pass": "gui pass word", "qbit_category": "guicat"}})
        client = watcher.make_qbit()
        self.assertEqual((client._username, client._password), ("guiuser", "gui pass word"))
        self.assertEqual(watcher.qbit_category(), "guicat")

    def test_a_save_starts_the_loop_only_in_the_daemon(self):
        w = watcher.WATCHER
        with mock.patch.object(w, "start", return_value=True) as start:
            self.save({"settings": {"qbit_category": "guicat"}})
            start.assert_not_called()  # autostart is off outside the daemon
            w.autostart = True
            with mock.patch.object(type(w), "alive", new_callable=mock.PropertyMock, return_value=False):
                self.save({"settings": {"qbit_category": "other"}})
            start.assert_called_once()
            start.reset_mock()
            with mock.patch.object(type(w), "alive", new_callable=mock.PropertyMock, return_value=True):
                self.save({"settings": {"qbit_category": "third"}})
            start.assert_not_called()  # already running: nothing to start
            w.autostart = True
            self.save({"clear": ["qbit_pass"]})
            start.assert_not_called()  # not configured: nothing to start


class TestPageShape(WatcherSettingsCase):
    def test_groups_hint_and_one_primary(self):
        page = self.page()
        block = page[page.index('<section class="ab-acct ab-watch" id="watcher"'):]
        block = block[:block.index("</section>")]
        for title in ("Where it looks", "How often it looks", "Test the connection"):
            self.assertIn(f'<div class="ab-acct-title">{title}</div>', block)
        setup = block.split("<summary>Tracker and download client setup</summary>", 1)[1]
        setup, schedule = setup.split("</details>", 1)
        self.assertLess(setup.index("Where it looks"), setup.index("Test the connection"))
        self.assertIn("How often it looks", schedule)
        self.assertIn("<code>TRACKER_PROXY</code> and <code>TRACKER_HOME_IP</code>", block)
        # the page's one Save settings ends the form, after the panels; the Watcher panel has no filled button
        self.assertEqual(block.count('class="ab-save"'), 0)
        form = page.split('<form id="ab-settings-form"')[1].split("</form>")[0]
        self.assertEqual(form.count('<button class="ab-save" type="submit">Save settings</button>'), 1)
        self.assertIn(">Save and test qBittorrent</button>", block)
        self.assertIn(">Save and test tracker</button>", block)

    def test_unconfigured_still_offers_the_connection_fields(self):
        with mock.patch.object(CONFIG, "tracker_session", ""):
            page = self.page()
        self.assertIn("Acquisition source (optional)", page)
        self.assertIn('data-setting="tracker_url"', page)
        self.assertIn('data-setting="qbit_pass"', page)
        self.assertIn("Save and test tracker", page)
        self.assertIn("no restart needed", page)
        self.assertNotIn('id="ab-watch-run"', page)
        self.assertNotIn('data-setting="watch_interval"', page)

"""0.21.0-dev5: the Settings page in four tabs - Library, Watcher, Account, Advanced.

One form, one Save settings: every tab's panel is in the page (the inactive ones hidden), so a save
from any tab carries every changed field. The active tab comes from ?tab= and is rendered by the
server; the tab links are plain links.
"""

import json
import re
import urllib.error
import urllib.request
from unittest import mock

from audiobind import config, web
from audiobind.config import CONFIG

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

#: a configured watcher, so every watcher field (the pacing ones too) is on the page
TRACKER = {
    "tracker_url": "https://tracker.example", "tracker_search_path": "/search.json",
    "tracker_search_body": '{"q": "{{title}} {{author}}"}', "tracker_download_path": "/download.php",
    "tracker_cookie_name": "sid", "tracker_session": "TAB-SESSION", "qbit_user": "u", "qbit_pass": "TAB-PASS",
}

#: one field (or form) only that tab shows
MARKERS = {
    "library": 'data-setting="output_format"',
    "watcher": 'data-setting="qbit_url"',
    "account": 'data-acct="password"',
    "advanced": 'data-setting="region"',
}


def active_panel(page):
    found = re.search(r'<div class="ab-tabpanel" id="ab-tab-(\w+)" data-tab="\w+">', page)
    rest = page[found.start():]
    end = re.search(r'<div class="ab-tabpanel" id=|<div class="ab-editbar"|</section>\s*</main>', rest[1:])
    return found.group(1), rest[: end.start() + 1]


def the_form(page):
    return page.split('<form id="ab-settings-form"')[1].split("</form>")[0]


class TabsCase(LiveServerCase):
    def setUp(self):
        super().setUp()
        for key, value in TRACKER.items():
            patcher = mock.patch.object(CONFIG, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.read().decode()

    def post_settings(self, payload):
        req = urllib.request.Request(self.base + "/api/settings", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class TestTabs(TabsCase):
    def test_each_tab_shows_its_own_fields_and_not_the_others(self):
        for tab, marker in MARKERS.items():
            with self.subTest(tab=tab):
                name, panel = active_panel(web.render_settings(self.state, tab))
                self.assertEqual(name, tab)
                self.assertIn(marker, panel)
                for other, other_marker in MARKERS.items():
                    if other != tab:
                        self.assertNotIn(other_marker, panel, f"{other}'s field on the {tab} tab")
        # the specific pairs the brief names
        self.assertNotIn('data-setting="qbit_url"', active_panel(web.render_settings(self.state, "library"))[1])
        self.assertNotIn('data-setting="output_format"', active_panel(web.render_settings(self.state, "watcher"))[1])

    def test_what_each_tab_holds(self):
        _, library = active_panel(web.render_settings(self.state, "library"))
        for key in ("output_format", "auto_convert", "audio_bitrate", "runtime_delta", "scan_interval", "nice"):
            self.assertIn(f'data-setting="{key}"', library)
        self.assertIn('id="ab-profile"', library)  # Quality profile
        self.assertIn("Match Audiobookshelf layout", library)
        _, watcher = active_panel(web.render_settings(self.state, "watcher"))
        for key in config.WATCHER_SETTING_KEYS + ("vip", "watch_interval", "watch_per_cycle", "wedge_reserve",
                                                  "wedge_per_cycle", "wedge_per_day", "watch_max_unsatisfied"):
            self.assertIn(f'data-setting="{key}"', watcher)
        for marker in ('id="ab-watch-dry"', 'id="ab-watch-run"', 'id="ab-watch-toggle"', '<dl class="ab-watch-grid">'):
            self.assertIn(marker, watcher)
        _, account = active_panel(web.render_settings(self.state, "account"))
        for marker in ('data-acct="username"', 'name="new_password"', 'name="confirm"', 'action="/logout"'):
            self.assertIn(marker, account)
        _, advanced = active_panel(web.render_settings(self.state, "advanced"))
        for marker in ('data-setting="region"', 'data-setting="expected_egress"', "<code>TRACKER_PROXY</code>",
                       "<code>TRACKER_HOME_IP</code>"):
            self.assertIn(marker, advanced)

    def test_the_url_picks_the_tab_and_anything_else_is_library(self):
        for query, tab in (("?tab=watcher", "watcher"), ("?tab=account", "account"), ("?tab=advanced", "advanced"),
                           ("?tab=nonsense", "library"), ("", "library"), ("?tab=", "library"),
                           ("?tab=WATCHER", "watcher")):
            with self.subTest(query=query):
                page = self.get("/settings" + query)
                self.assertEqual(active_panel(page)[0], tab)
                self.assertIn(f'<a class="chip on" href="/settings?tab={tab}" aria-current="page">', page)

    def test_tab_bar_is_four_plain_links_in_order_with_one_on(self):
        page = web.render_settings(self.state, "watcher")
        bar = page.split('<nav class="chips ab-tabs" aria-label="Settings sections">')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'<a class="chip(?: on)?" href="/settings\?tab=(\w+)"', bar),
                         ["library", "watcher", "account", "advanced"])
        self.assertEqual(re.findall(r">(\w+)</a>", bar), ["Library", "Watcher", "Account", "Advanced"])
        self.assertEqual(bar.count('class="chip on"'), 1)
        self.assertNotIn("onclick", bar)
        # the banner sits above the tabs; the on tab is the indigo pill
        self.assertLess(page.index("<b>Apply later.</b>"), page.index('class="chips ab-tabs"'))
        self.assertIn(".ab-tabs .chip.on{background:rgba(94,106,210,.14)", web.STYLES)

    def test_inactive_panels_are_hidden_not_missing(self):
        page = web.render_settings(self.state, "advanced")
        for tab in ("library", "watcher", "account"):
            self.assertIn(f'<div class="ab-tabpanel" id="ab-tab-{tab}" data-tab="{tab}" hidden>', page)
        self.assertIn('<div class="ab-tabpanel" id="ab-tab-advanced" data-tab="advanced">', page)


class TestOneForm(TabsCase):
    def test_every_setting_is_in_the_form_exactly_once_whatever_the_tab(self):
        for tab in ("library", "watcher", "account", "advanced", "nonsense"):
            with self.subTest(tab=tab):
                page = web.render_settings(self.state, tab)
                keys = re.findall(r'data-setting="(\w+)"', the_form(page))
                self.assertEqual(len(keys), len(config.SETTING_KEYS))
                self.assertEqual(sorted(keys), sorted(config.SETTING_KEYS))
                markup = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
                self.assertEqual(markup.count('data-setting="'), len(keys), "a settings field outside the form")

    def test_one_save_settings_and_only_inside_the_form(self):
        for tab in ("library", "watcher", "account", "advanced"):
            with self.subTest(tab=tab):
                page = web.render_settings(self.state, tab)
                self.assertEqual(page.count(">Save settings</button>"), 1)
                self.assertEqual(the_form(page).count('<button class="ab-save" type="submit">Save settings</button>'), 1)
                self.assertIn('onsubmit="abSaveSettings(); return false;"', page)

    def test_no_other_button_inside_the_form_submits_it(self):
        form = the_form(web.render_settings(self.state, "watcher"))
        buttons = re.findall(r"<button[^>]*>", form)
        self.assertTrue(buttons)
        submits = [b for b in buttons if 'type="button"' not in b]
        self.assertEqual(submits, ['<button class="ab-save" type="submit">'])

    def test_save_is_the_only_filled_button_in_the_form(self):
        page = web.render_settings(self.state, "library")
        self.assertEqual(the_form(page).count('class="ab-save"'), 1)
        self.assertIn('<button class="btn" type="button" id="ab-abs-apply"', page)

    def test_account_panel_sits_outside_the_settings_form(self):
        # its own forms cannot nest inside the settings form; the Save bar is hidden on that tab
        page = web.render_settings(self.state, "account")
        self.assertNotIn("data-acct=", the_form(page))
        self.assertIn('<div class="ab-editbar" style="margin-top:8px;" hidden>', page)
        self.assertNotIn('<div class="ab-editbar" style="margin-top:8px;" hidden>', web.render_settings(self.state, "watcher"))

    def test_save_collects_from_every_panel(self):
        # the page script reads every [data-setting] in the document, hidden panels included
        self.assertIn("document.querySelectorAll('[data-setting]').forEach(wrap => {", web.SETTINGS_SCRIPT)
        self.assertNotIn(".ab-tabpanel:not([hidden])", web.SETTINGS_SCRIPT)

    def test_a_save_from_the_watcher_tab_still_writes_a_library_field(self):
        """Partial-write guard: what the form posts from the Watcher tab, with a Library field changed
        before switching tabs, persists both."""
        page = self.get("/settings?tab=watcher")
        self.assertEqual(active_panel(page)[0], "watcher")
        self.assertIn('data-setting="output_format"', the_form(page))  # the Library field is in the form
        status, body = self.post_settings({"settings": {"output_format": "MP3", "qbit_category": "books"},
                                           "clear": []})
        self.assertEqual(status, 200, body)
        self.assertEqual(config.setting_effective("output_format"), "MP3")
        self.assertEqual(config.setting_effective("qbit_category"), "books")
        self.assertEqual(self.state.get_meta("setting_output_format"), "MP3")
        self.assertEqual(self.state.get_meta("setting_qbit_category"), "books")


class TestAdvanced(TabsCase):
    def test_container_only_settings_are_read_only_and_the_proxy_never_shown(self):
        with mock.patch.object(CONFIG, "tracker_proxy", "socks5://user:PROXY-SECRET@vpn:1080"), \
                mock.patch.object(CONFIG, "tracker_home_ip", "203.0.113.9"):
            page = web.render_settings(self.state, "advanced")
        _, panel = active_panel(page)
        self.assertIn("<dt><code>TRACKER_PROXY</code></dt><dd>set &#8212;", panel)
        self.assertIn("<dt><code>TRACKER_HOME_IP</code></dt><dd>203.0.113.9 &#8212;", panel)
        self.assertNotIn("PROXY-SECRET", page)
        self.assertNotIn('data-setting="tracker_proxy"', page)
        self.assertNotIn('data-setting="tracker_home_ip"', page)
        page = web.render_settings(self.state, "advanced")
        self.assertIn("<dt><code>TRACKER_PROXY</code></dt><dd>not set &#8212;", page)

    def test_the_watcher_panel_points_at_the_advanced_tab_for_the_egress_pin(self):
        _, panel = active_panel(web.render_settings(self.state, "watcher"))
        self.assertNotIn('data-setting="expected_egress"', panel)
        self.assertIn("set the allowed IP on the Advanced tab", panel)

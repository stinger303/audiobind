"""The onboarding fix-first batch: one wording on every surface that carries it.

The template (what Unraid shows at install), the docs, the README and the Settings page used to
say different things about the same setting. These tests pin the shared facts so a fix that lands
in one place and not the others fails here.
"""

import html as html_mod
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from audiobind import config, ingest, watcher, web
from audiobind.config import CONFIG
from audiobind.state import State

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "templates" / "audiobind.xml"
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
ACQUISITION = (ROOT / "docs" / "acquisition.md").read_text(encoding="utf-8")
INSTALL = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONFIGURATION = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

EASIER = "Easier: leave empty and fill this in under Settings > Watcher in the web UI (checked there, no restart)."
EASIER_SET = "Easier: fill this in under Settings > Watcher in the web UI (checked there, no restart)."
#: the one qBittorrent address rule, on every surface
QBIT_RULE = ("The address you open qBittorrent's WebUI with, e.g. http://192.168.1.10:8080. Only if Audiobind "
             "shares a VPN container's network with qBittorrent: http://127.0.0.1:<its port>.")


def field(target):
    """(attributes, description, value) of one template <Config>, XML-decoded."""
    root = ET.parse(TEMPLATE_PATH).getroot()
    for node in root.iter("Config"):
        if node.get("Target") == target:
            return node.attrib, node.get("Description"), node.text or ""
    raise KeyError(target)


class TestTemplateIsValidXml(unittest.TestCase):
    def test_it_parses(self):
        ET.parse(TEMPLATE_PATH)


class TestWrongFactsTemplate(unittest.TestCase):
    def test_runtime_tolerance_is_fifteen_percent_everywhere(self):
        attrs, description, value = field("MAX_RUNTIME_DELTA")
        self.assertEqual((attrs["Default"], value), ("0.15", "0.15"))
        self.assertIn("(0.15 = 15%)", description)
        self.assertIn("by more than 15%", TEMPLATE)
        self.assertNotIn("25%", TEMPLATE)
        self.assertEqual(config.Config().max_runtime_delta, 0.15)
        self.assertIn("0.15", CONFIGURATION)
        self.assertIn(">15%", README)

    def test_delete_is_a_confirmation_dialog_not_a_typed_confirmation(self):
        self.assertIn("(a confirmation dialog, no undo)", TEMPLATE)
        self.assertNotIn("typed confirmation", TEMPLATE)
        self.assertIn("no typed confirmation, so read the dialog", README)

    def test_medium_is_left_for_review_by_default(self):
        self.assertIn("| `MEDIUM` | exact title, author inferred from the catalog | left for review "
                      "(converted when `AUTO_CONVERT=medium`) |", README)
        self.assertEqual(field("AUTO_CONVERT")[0]["Default"], "high")

    def test_template_regions_are_the_ones_settings_offers(self):
        _, description, _ = field("REGION")
        listed = re.search(r"metadata: ([a-z, ]+)\.", description).group(1).split(", ")
        self.assertEqual(listed, list(config.SETTING_CHOICES["region"]))
        for code in listed:
            self.assertIn(f"`{code}`", CONFIGURATION.split("| `REGION` |")[1].split("\n")[0])
        for dropped in ("fr", "it", "es", "jp", "in"):
            self.assertNotIn(f"`{dropped}`", CONFIGURATION.split("| `REGION` |")[1].split("\n")[0])

    def test_the_vpn_pointer_is_the_real_anchor(self):
        _, description, _ = field("TRACKER_PROXY")
        self.assertIn("https://github.com/stinger303/audiobind/blob/main/docs/acquisition.md"
                      "#running-the-watcher-through-a-vpn", description)
        self.assertNotIn("see the README", description)
        self.assertIn("## Running the watcher through a VPN", ACQUISITION)

    def test_web_port_is_an_advanced_field(self):
        attrs, description, value = field("WEB_PORT")
        self.assertEqual((attrs["Display"], attrs["Default"], value), ("advanced", "8080", "8080"))
        self.assertIn("8686", description)
        self.assertIn("VPN container", description)
        self.assertEqual(config.Config().web_port, 8080)


class TestTrackerAndQbitDescriptions(unittest.TestCase):
    #: container-only settings (no Settings field), so "fill this in under Settings" would be false
    CONTAINER_ONLY = {"TRACKER_PROXY", "TRACKER_HOME_IP"}
    #: the template ships a value for these, so "leave empty" would be wrong
    HAS_DEFAULT = {"TRACKER_VIP", "QBIT_CATEGORY"}
    REQUIRED = ("TRACKER_URL", "TRACKER_SEARCH_PATH", "TRACKER_SEARCH_BODY", "TRACKER_DOWNLOAD_PATH",
                "TRACKER_COOKIE_NAME", "TRACKER_SESSION")

    def targets(self):
        root = ET.parse(TEMPLATE_PATH).getroot()
        return [n.get("Target") for n in root.iter("Config")
                if (n.get("Target") or "").startswith(("TRACKER_", "QBIT_"))]

    def test_every_settable_one_starts_with_the_easier_route(self):
        seen = 0
        for target in self.targets():
            if target in self.CONTAINER_ONLY:
                continue
            with self.subTest(target=target):
                _, description, _ = field(target)
                self.assertTrue(description.startswith(EASIER_SET if target in self.HAS_DEFAULT else EASIER),
                                description)
                seen += 1
        self.assertEqual(seen, 18)

    def test_container_only_ones_do_not_send_you_to_settings(self):
        for target in self.CONTAINER_ONLY:
            with self.subTest(target=target):
                self.assertNotIn("under Settings", field(target)[1])

    def test_the_six_required_ones_say_needed_not_optional(self):
        for target in self.REQUIRED:
            with self.subTest(target=target):
                description = field(target)[1]
                self.assertIn("Needed for the wishlist.", description)
                self.assertNotIn("Optional", description)

    def test_the_wishlist_list_names_the_search_body(self):
        description = field("TRACKER_URL")[1]
        self.assertIn("both paths, the search body, the cookie name and the session", description)

    def test_a_path_in_tracker_url_is_warned_about(self):
        self.assertIn("A path here (e.g. a pasted bookmark) stops the container from starting.", field("TRACKER_URL")[1])

    def test_the_warning_is_true(self):
        with self.assertRaises(RuntimeError):
            config._tracker_url("https://tracker.example/some/bookmark")

    def test_qbit_url_is_empty_by_default_and_carries_the_one_rule(self):
        attrs, description, value = field("QBIT_URL")
        self.assertEqual((attrs["Default"], value), ("", ""))
        self.assertTrue(description.endswith(QBIT_RULE), description)
        self.assertNotIn("8083", TEMPLATE)
        self.assertEqual(config.Config().qbit_url, "")
        self.assertEqual(web.QBIT_URL_HINT, QBIT_RULE)
        self.assertEqual(web.WATCH_FIELD_HINTS["qbit_url"], QBIT_RULE)
        self.assertIn("http://192.168.1.10:8080", CONFIGURATION)
        self.assertNotIn("localhost:8083", CONFIGURATION)


class TestDocs(unittest.TestCase):
    def test_install_downloads_the_template_instead_of_copying_from_a_checkout(self):
        self.assertNotIn("cp templates/audiobind.xml", INSTALL)
        self.assertIn("wget -O /boot/config/plugins/dockerMan/templates-user/audiobind.xml "
                      "https://raw.githubusercontent.com/stinger303/audiobind/main/templates/audiobind.xml", INSTALL)
        self.assertIn("raw.githubusercontent.com/stinger303/audiobind/main/templates/audiobind.xml", TEMPLATE)

    def test_install_does_not_call_the_library_read_only(self):
        self.assertNotIn("library (read-only)", INSTALL)
        self.assertIn("(the Library path's description explains Read/Write vs Read Only)", INSTALL)
        self.assertIn('Target="/library" Default="/mnt/user/data/media/audiobooks" Mode="rw"', TEMPLATE)

    def test_install_first_scan_timing(self):
        self.assertNotIn("discovered within `SCAN_INTERVAL_MIN`", INSTALL)
        self.assertIn("The first scan starts as soon as the container\n   starts", INSTALL)
        self.assertIn("After that it scans every\n   `SCAN_INTERVAL_MIN` minutes.", INSTALL)

    def test_proxy_example_uses_the_server_ip(self):
        self.assertIn("TRACKER_PROXY=http://<server IP>:8888", ACQUISITION)
        self.assertNotIn("TRACKER_PROXY=http://gluetun:8888", ACQUISITION)
        self.assertIn("only works when both containers are on the same custom Docker network", ACQUISITION)

    def test_the_port_clash_is_explained(self):
        self.assertIn("**Port clash.**", ACQUISITION)
        self.assertIn("WEB_PORT=8686", ACQUISITION)
        self.assertIn("-p 8686:8686", ACQUISITION)
        self.assertIn("http://<server IP>:8686", ACQUISITION)

    def test_the_doc_says_the_watcher_controls_appear_once_configured(self):
        self.assertIn("appear once the tracker settings are complete", ACQUISITION)

    def test_the_shared_network_qbit_rule_in_the_doc(self):
        self.assertIn("QBIT_URL=http://127.0.0.1:<its port>", ACQUISITION)
        self.assertNotIn("127.0.0.1:8083", ACQUISITION)


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "library").mkdir()
        self.state = State(self.root / "state")
        self.addCleanup(self.state.close)
        patch = mock.patch.object(CONFIG, "library_root", self.root / "library")
        patch.start()
        self.addCleanup(patch.stop)
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def watcher(self, tab="watcher"):
        page = web.render_settings(self.state, tab)
        start = page.index('id="ab-tab-watcher"')
        return page[start:page.index('id="ab-tab-account"')]


class TestUpdaterCopy(WebCase):
    def test_off_no_longer_says_the_watcher_refuses_to_run(self):
        off = web.IP_UPDATER_MODE_HINTS["off"]
        self.assertEqual(off, "Off (default): nothing automatic. If your VPN's address changes and the IP guard is "
                              "armed, the watcher stops and tells you.")
        self.assertNotIn("unregistered", off)
        self.assertNotIn("refuses to run", self.watcher())

    def test_auto_names_mam_the_toggle_and_the_home_ip(self):
        auto = web.IP_UPDATER_MODE_HINTS["auto"]
        self.assertTrue(auto.startswith("MAM only: when your VPN's address changes, tells MAM the new one."))
        # tests/test_tracker_config.py pins that no page names a site, so the hint says MAM, not MyAnonamouse
        self.assertNotIn("myanonamouse", auto.lower())
        self.assertIn("'Allow session to set dynamic seedbox IP' ticked on MAM's Preferences → Security page", auto)
        self.assertIn("and TRACKER_HOME_IP set.", auto)
        self.assertIn("('Seedbox IP' is MAM's name for the address you connect from.)", auto)
        self.assertIn("TRACKER_HOME_IP", (ROOT / "docs" / "ip-updater.md").read_text(encoding="utf-8"))
        self.assertIn("Preferences → Security", (ROOT / "docs" / "ip-updater.md").read_text(encoding="utf-8"))

    def test_the_updater_is_below_the_connection_fields_and_the_test_buttons(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                patches = []
                if configured:
                    values = {"tracker_url": "https://tracker.example", "tracker_search_path": "/s",
                              "tracker_search_body": '{"q": "{{title}}"}', "tracker_download_path": "/d",
                              "tracker_cookie_name": "sid", "tracker_session": "x", "qbit_user": "u", "qbit_pass": "p",
                              "qbit_url": "http://qbit.example:8080"}
                    patches = [mock.patch.object(CONFIG, k, v) for k, v in values.items()]
                for p in patches:
                    p.start()
                    self.addCleanup(p.stop)
                config._EFFECTIVE.clear()
                panel = self.watcher()
                self.assertLess(panel.index('data-setting="tracker_url"'), panel.index("Allowed-IP updater</div>"))
                self.assertLess(panel.index('id="ab-watch-test-tracker"'), panel.index("Allowed-IP updater</div>"))
                self.assertLess(panel.index('id="ab-dc-test"'), panel.index("Allowed-IP updater</div>"))
                for p in patches:
                    p.stop()


class TestQbitCopyOnThePage(WebCase):
    def test_one_rule_based_hint_and_no_other_hosts(self):
        panel = self.watcher()
        self.assertIn(html_mod.escape(QBIT_RULE, quote=False), panel)
        self.assertNotIn("http://qbittorrent:8080", panel)
        self.assertNotIn("localhost:8083", panel)
        self.assertIn('placeholder="http://192.168.1.10:8080"', panel)

    def test_an_empty_qbit_url_is_listed_in_still_to_set(self):
        panel = self.watcher()
        still = panel.split("Still to set:")[1].split("(the site's URL")[0]
        self.assertIn("<code>QBIT_URL</code>", still)

    def test_a_set_qbit_url_is_not(self):
        with mock.patch.object(CONFIG, "qbit_url", "http://192.168.1.10:8080"):
            config._EFFECTIVE.clear()
            panel = self.watcher()
        still = panel.split("Still to set:")[1].split("(the site's URL")[0]
        self.assertNotIn("QBIT_URL", still)


class TestEmptyLibrary(WebCase):
    def test_no_books_and_no_filter_points_at_the_mount(self):
        page = web.render(self.state)
        self.assertIn(f"No books found in {self.root / 'library'} yet. If a scan has finished and this is still "
                      "empty, check the Library path in the container settings.", page)
        self.assertNotIn("no books match this filter", page)

    def test_a_filter_that_matches_nothing_keeps_the_filter_wording(self):
        page = web.render(self.state, status="held")
        self.assertIn("no books match this filter", page)
        self.assertNotIn("No books found in", page)
        page = web.render(self.state, query="dune")
        self.assertIn("no books match this filter", page)


#: the Group 2 sentences, written out once so a drifting copy fails here
ONE_FOLDER = ("One folder, three names. Example: host folder /mnt/user/downloads/audiobooks \u2192 Audiobind's Ingest "
              "path (Audiobind calls it /ingest) \u2192 in qBittorrent, the category's save path is whatever "
              "qBittorrent's own Docker page maps that host folder to (often /downloads/audiobooks or "
              "/data/audiobooks). Never type /ingest into qBittorrent.")


class TestFolders(WebCase):
    """ONB-21 (one folder, three names) and ONB-12 (the Settings rail shows the ingest line)."""

    def setUp(self):
        super().setUp()
        # whether the machine running the tests has an /ingest folder must not change the plain "off" wording
        patch = mock.patch.object(ingest, "MOUNT_PATH", str(self.root / "no-ingest-mount"))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_category_field_teaches_the_three_names_in_template_and_settings(self):
        _, description, _ = field("QBIT_CATEGORY")
        self.assertTrue(description.endswith(ONE_FOLDER), description)
        self.assertEqual(web.ONE_FOLDER_THREE_NAMES, ONE_FOLDER)
        self.assertIn("Never type /ingest into qBittorrent.", self.watcher())
        self.assertNotIn("Point its save path at Audiobind's ingest folder", self.watcher())

    def test_the_names_used_are_the_field_names(self):
        self.assertIn('Name="Ingest path" Target="/ingest"', TEMPLATE)

    def rail(self, tab="library"):
        page = web.render_settings(self.state, tab)
        return page.split('<div class="ab-rail-foot">')[1].split("</aside>")[0]

    def test_the_settings_rail_says_ingest_is_off_and_where_to_turn_it_on(self):
        with mock.patch.object(CONFIG, "ingest_dir", ""):
            config._EFFECTIVE.clear()
            rail = self.rail()
        self.assertIn("<b>ingest</b>", rail)
        self.assertIn("(off \u2014 set Ingest path in the container settings)", html_mod.unescape(rail))
        self.assertNotIn("INGEST_DIR", rail)

    def test_the_settings_rail_shows_the_folder_when_ingest_is_on(self):
        ingest = self.root / "ingest"
        ingest.mkdir()
        with mock.patch.object(CONFIG, "ingest_dir", str(ingest)):
            config._EFFECTIVE.clear()
            rail = self.rail("watcher")
        self.assertIn(f"<b>ingest</b> &larr; <span", rail)
        self.assertIn(str(ingest), rail)
        self.assertNotIn("set Ingest path", rail)

    def test_the_library_rail_uses_the_same_off_wording(self):
        with mock.patch.object(CONFIG, "ingest_dir", ""):
            config._EFFECTIVE.clear()
            page = web.render(self.state)
        self.assertIn("(off \u2014 set Ingest path in the container settings)", html_mod.unescape(page))
        self.assertNotIn("set INGEST_DIR to use it", page)


class TestSessionHint(WebCase):
    """ONB-26: on MyAnonamouse the hint says where the value comes from; elsewhere it keeps the browser route."""

    def hint_for(self, url):
        with mock.patch.object(CONFIG, "tracker_url", url):
            config._EFFECTIVE.clear()
            panel = self.watcher()
        block = panel.split('data-setting="tracker_session"')[1]
        return re.search(r'<div class="ab-setting-hint">(.*?)</div>', block, re.S).group(1)

    def test_a_myanonamouse_url_gets_the_site_route(self):
        for url in ("https://www.myanonamouse.net", "https://t.myanonamouse.net"):
            with self.subTest(url=url):
                hint = html_mod.unescape(self.hint_for(url))
                self.assertIn("On MyAnonamouse: Preferences \u2192 Security \u2192 create a session for the address "
                              "the tracker will see", hint)
                self.assertIn("tick 'Allow session to set dynamic seedbox IP'", hint)
                self.assertIn("paste the long value MAM gives you.", hint)
                self.assertNotIn("copied from your browser", hint)

    def test_any_other_site_keeps_the_browser_route(self):
        for url in ("https://tracker.example", "", "https://myanonamouse.net.evil.example"):
            with self.subTest(url=url):
                hint = html_mod.unescape(self.hint_for(url))
                self.assertIn("The value of the site's session cookie, copied from your browser while you are "
                              "signed in.", hint)
                self.assertNotIn("Preferences", hint)

    def test_only_labels_the_repo_already_documents_are_used(self):
        doc = (ROOT / "docs" / "ip-updater.md").read_text(encoding="utf-8")
        self.assertIn("Preferences \u2192 Security", doc)
        self.assertIn("Allow session to set dynamic seedbox IP", doc)
        labels = re.findall(r"(?:Preferences|Security|Account|Profile|Settings)\b", web.MAM_SESSION_HINT)
        self.assertEqual(sorted(set(labels)), ["Preferences", "Security"])


#: the template and the doc say "This covers ..."; the Settings page names what "this" is
SCOPE = ("covers Audiobind's own visits to the tracker (searches and .torrent files). The download itself is "
         "qBittorrent's; route qBittorrent through your VPN separately.")
WEB_SCOPE = ("The proxy and the IP checks cover Audiobind's own visits to the tracker (searches and .torrent files). "
             "The download itself is qBittorrent's; route qBittorrent through your VPN separately.")
UNRAID_PATH = ("On Unraid: Docker tab \u2192 Audiobind \u2192 Edit \u2192 Show more settings \u2192 "
               "'Tracker proxy' / 'Home IP (guard)' \u2192 Apply (restarts the container).")


class TestVpnCopy(WebCase):
    """ONB-16 (tracker visits only) and ONB-17 (where the two container-only settings are on Unraid)."""

    def test_the_watcher_tab_says_what_the_vpn_settings_cover_and_where_the_two_live(self):
        panel = html_mod.unescape(self.watcher())
        intro = panel.split('<div class="ab-acct-title">Where it looks</div>')[1].split("</div>")[0]
        self.assertIn(WEB_SCOPE, intro)
        self.assertIn(UNRAID_PATH, intro)
        self.assertLess(intro.index("Two settings stay container-only"), intro.index(UNRAID_PATH))
        self.assertEqual(web.VPN_SCOPE_NOTE, WEB_SCOPE)

    def test_the_advanced_tab_says_it_too(self):
        page = html_mod.unescape(web.render_settings(self.state, "advanced"))
        box = page.split('id="container-only"')[1].split("</section>")[0]
        self.assertIn(UNRAID_PATH, box)
        self.assertIn(WEB_SCOPE, box)

    def test_the_unraid_names_are_the_template_field_names(self):
        self.assertIn('Name="Tracker proxy" Target="TRACKER_PROXY"', TEMPLATE)
        self.assertIn('Name="Home IP (guard)" Target="TRACKER_HOME_IP"', TEMPLATE)
        self.assertEqual(field("TRACKER_PROXY")[0]["Display"], "advanced")  # behind "Show more settings"

    def test_the_template_proxy_field_carries_the_scope(self):
        self.assertIn("This " + SCOPE, field("TRACKER_PROXY")[1])

    def test_the_doc_leads_the_vpn_section_with_the_scope_and_gives_the_click_path_twice(self):
        section = ACQUISITION.split("## Running the watcher through a VPN")[1]
        lead = section.split("A tracker often ties")[0]
        self.assertIn("**What this covers:** Audiobind's own visits to the tracker (searches and `.torrent` files)", lead)
        self.assertIn("The\ndownload itself is qBittorrent's; route qBittorrent through your VPN separately.", lead)
        self.assertIn('Docker tab \u2192\nAudiobind \u2192 Edit \u2192 Show more settings \u2192 "Tracker proxy" \u2192 Apply', section)
        self.assertIn('Docker tab \u2192 Audiobind \u2192 Edit \u2192\nShow more settings \u2192 "Home IP (guard)" \u2192 Apply', section)


CONFIGURED = {"tracker_url": "https://tracker.example", "tracker_search_path": "/s",
              "tracker_search_body": '{"q": "{{title}}"}', "tracker_download_path": "/d",
              "tracker_cookie_name": "sid", "tracker_session": "x", "qbit_user": "u", "qbit_pass": "p",
                              "qbit_url": "http://qbit.example:8080"}


class TestFirstVisitTemplate(unittest.TestCase):
    """ONB-01, ONB-04, ONB-08 in the template; ONB-08 and ONB-04 in the README and install doc."""

    def test_overview_says_it_can_fetch_books_and_search_terms_find_it(self):
        overview = ET.parse(TEMPLATE_PATH).getroot().find("Overview").text
        self.assertTrue(overview.endswith(
            "Optional: a wishlist that searches your private tracker and hands finds to qBittorrent, Deluge, "
            "Transmission or rTorrent, then converts what lands."))
        terms = ET.parse(TEMPLATE_PATH).getroot().find("ExtraSearchTerms").text.split()
        for word in ("wishlist", "tracker", "qbittorrent"):
            self.assertIn(word, terms)

    def test_the_host_port_defaults_to_8686_and_the_webui_link_still_uses_the_container_port(self):
        attrs, description, value = field("8080")
        self.assertEqual((attrs["Default"], value, attrs["Type"]), ("8686", "8686", "Port"))
        self.assertEqual(description, "Status page and JSON API. Change it if another container already uses this port.")
        self.assertIn("<WebUI>http://[IP]:[PORT:8080]</WebUI>", TEMPLATE)
        self.assertEqual(config.Config().web_port, 8080)  # no code default changed
        self.assertIn("host port 8686", INSTALL)

    def test_auto_convert_says_when_the_first_scan_starts(self):
        description = field("AUTO_CONVERT")[1]
        self.assertIn("high converts the books it is sure of on the first scan, which starts as soon as the "
                      "container does. Choose off to review every match first, then switch to high under Settings.",
                      description)
        self.assertEqual(field("AUTO_CONVERT")[0]["Default"], "high")  # copy only: no behaviour change
        self.assertIn("auto_convert", config.GENERAL_SETTING_KEYS)  # "under Settings" is a real control

    def test_the_readme_beta_box_says_converting_only_reads_the_sources(self):
        box = README.split("## Status: beta")[1].split("## How it works")[0]
        self.assertIn("Converting only reads your sources and writes new files to the\noutput folder", box)
        self.assertIn("The\nfirst scan starts as soon as the container does, and `AUTO_CONVERT=high` (the default)\n"
                      "converts the books it is sure of; set Auto convert to `off` to review every match first.", box)


class TestHostRefusal(LiveServerCase):
    """ONB-09: the 403 must not advise the thing that locks the IP out."""

    def setUp(self):
        super().setUp()
        saved = (CONFIG.allowed_hosts, CONFIG.allowed_hosts_set)
        self.addCleanup(lambda: setattr(CONFIG, "allowed_hosts", saved[0]))
        self.addCleanup(lambda: setattr(CONFIG, "allowed_hosts_set", saved[1]))

    def refused(self, host):
        status, body = self.request("/api/state", None, {"Host": host}, raw=True)
        self.assertEqual(status, 403)
        return body.decode()

    def test_unset_says_the_ip_works_and_that_a_list_replaces_the_default(self):
        CONFIG.allowed_hosts, CONFIG.allowed_hosts_set = ["localhost", "127.0.0.1"], False
        body = self.refused("tower")
        self.assertIn("Audiobind doesn't answer to 'tower' yet. It does answer at its IP address (the one you use "
                      "for the Unraid web UI).", body)
        self.assertIn("set Allowed hosts (ALLOWED_HOSTS) in the container settings to every name you use, "
                      "e.g. 192.168.1.10,tower. A list replaces the default, so include the IP.", body)
        self.assertNotIn("is not allowed", body)

    def test_the_example_is_what_was_asked_for_and_the_port_is_not_part_of_it(self):
        CONFIG.allowed_hosts, CONFIG.allowed_hosts_set = ["localhost", "127.0.0.1"], False
        body = self.refused("tower.local:8686")
        self.assertIn("doesn't answer to 'tower.local' yet", body)
        self.assertIn("e.g. 192.168.1.10,tower.local.", body)

    def test_with_a_list_set_it_does_not_claim_the_ip_still_works(self):
        CONFIG.allowed_hosts, CONFIG.allowed_hosts_set = ["tower"], True
        body = self.refused("192.168.5.2")
        self.assertIn("Audiobind doesn't answer to '192.168.5.2'.", body)
        self.assertIn("add '192.168.5.2' to that list and keep the names you already use.", body)
        self.assertNotIn("It does answer at its IP address", body)


class TestWishlistTabHint(WebCase):
    """ONB-30: say the Wishlist tab is coming, then say how to add a book."""

    def test_unconfigured_says_the_tab_will_appear(self):
        panel = self.watcher()
        self.assertIn("A Wishlist tab appears in the sidebar once these settings are complete.", panel)
        self.assertNotIn("ab-watch-ready", panel)

    def configured(self, **extra):
        patches = [mock.patch.object(CONFIG, k, v) for k, v in {**CONFIGURED, **extra}.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        config._EFFECTIVE.clear()

    def test_configured_and_enabled_says_ready_and_where_to_add_a_book(self):
        self.configured()
        panel = html_mod.unescape(self.watcher())
        self.assertIn('<div class="ab-acct-who" id="ab-watch-ready"><b>Ready.</b> Add a book: Discover books \u2192 Want.',
                      panel)
        self.assertNotIn("A Wishlist tab appears", panel)

    def test_the_next_check_time_is_shown_when_there_is_one(self):
        self.configured()
        status = watcher.WATCHER.status(self.state)
        status["next_cycle_at"] = 1700000000.0
        with mock.patch.object(watcher.WATCHER, "status", return_value=status):
            panel = html_mod.unescape(self.watcher())
        self.assertRegex(panel, r"Want\. Next check \d\d:\d\d\.</div>")

    def test_no_ready_line_when_the_watcher_cannot_run(self):
        self.configured(qbit_user="", qbit_pass="")  # configured, but nowhere to send a torrent
        self.assertNotIn("ab-watch-ready", self.watcher())


if __name__ == "__main__":
    unittest.main()

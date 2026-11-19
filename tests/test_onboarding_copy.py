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

from audiobind import config, web
from audiobind.config import CONFIG
from audiobind.state import State

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
                              "tracker_cookie_name": "sid", "tracker_session": "x", "qbit_user": "u", "qbit_pass": "p"}
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


if __name__ == "__main__":
    unittest.main()

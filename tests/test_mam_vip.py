"""0.18.0-dev5: VIP-only torrents are skipped unless MAM_VIP / Settings > Watcher says you are a VIP.

MAM is a real MamClient over a fake transport; nothing here can reach the network.
"""

import unittest
from unittest import mock

from audiobind import config, mam, watcher, web
from audiobind.config import CONFIG, Config

try:
    from test_watcher import WatcherCase, answer, entry, TORRENT
except ImportError:  # run from the repo root
    from tests.test_watcher import WatcherCase, answer, entry, TORRENT

ROW = {"title": "Project Hail Mary", "authors": ["Andy Weir"]}


def vip_entry(tid, name="Project Hail Mary", **kw):
    """A VIP-only row as MAM sends it: vip=1, free=0, fl_vip=1."""
    row = entry(tid, name, fl_vip=True, **kw)
    row["vip"] = "1"
    return row


def torrents(*entries):
    return mam.parse_search({"data": list(entries)})


class TestSelectVip(unittest.TestCase):
    def test_non_vip_skips_a_vip_only_torrent_and_says_why(self):
        pick, why = mam.select_candidate(ROW, torrents(vip_entry(1)))
        self.assertIsNone(pick)
        self.assertEqual(why, "only match(es) need VIP membership (1) - skipped")

    def test_default_is_non_vip(self):
        self.assertEqual(mam.select_candidate(ROW, torrents(vip_entry(1)))[0], None)

    def test_vip_member_selects_it_and_no_wedge_is_spent(self):
        found = torrents(vip_entry(1))
        pick, _ = mam.select_candidate(ROW, found, vip=True)
        self.assertEqual(pick.tid, "1")
        use_wedge, why = mam.wedge_decision(pick.raw)
        self.assertFalse(use_wedge)
        self.assertIn("VIP", why)

    def test_mixed_results_non_vip_picks_the_best_non_vip(self):
        found = torrents(vip_entry(1), entry(2, "Project Hail Mary", numfiles=1), vip_entry(3))
        self.assertEqual(mam.select_candidate(ROW, found)[0].tid, "2")

    def test_skips_are_counted(self):
        pick, why = mam.select_candidate(ROW, torrents(vip_entry(1), vip_entry(2), vip_entry(3)))
        self.assertIsNone(pick)
        self.assertIn("(3)", why)

    def test_vip_flag_forms_and_absence(self):
        plain = entry(1, "Project Hail Mary")
        self.assertEqual(mam.select_candidate(ROW, torrents(plain))[0].tid, "1", "no vip field = not VIP-only")
        plain["vip"] = "0"
        self.assertEqual(mam.select_candidate(ROW, torrents(plain))[0].tid, "1")

    def test_snatched_and_closest_miss_reasons_still_work(self):
        snatched = entry(1, "Project Hail Mary", snatched=1)
        self.assertIn("already snatched", mam.select_candidate(ROW, torrents(snatched))[1])
        near = entry(2, "Project Hail")
        self.assertIn("closest was", mam.select_candidate(ROW, torrents(near))[1])


class TestWatcherVip(WatcherCase):
    def test_non_vip_install_never_attempts_a_vip_only_torrent(self):
        self.want()
        self.mam(answer(vip_entry(1001)))  # a second request would raise in the transport
        with mock.patch.object(CONFIG, "mam_vip", False):
            summary = self.cycle()
        self.assertEqual(len(self.transport.calls), 1, "one search, zero download calls")
        self.assertFalse(any("download.php" in url for _, url in self.transport.calls))
        row = self.row()
        self.assertEqual(row["status"], "wanted")
        self.assertIn("need VIP membership (1) - skipped", row["note"])
        self.assertEqual(summary["not_found"], 1)

    def test_vip_install_downloads_it_without_a_wedge(self):
        self.want()
        self.mam(answer(vip_entry(1001)), (200, {}, TORRENT))
        with mock.patch.object(CONFIG, "mam_vip", True):
            self.cycle()
        self.assertEqual(self.transport.calls[1][1], "https://www.myanonamouse.net/tor/download.php?tid=1001")
        self.assertEqual(self.row()["status"], "downloading")

    def test_the_saved_setting_is_what_the_watcher_reads(self):
        config._EFFECTIVE["vip"] = "YES"
        self.addCleanup(config._EFFECTIVE.clear)
        with mock.patch.object(CONFIG, "mam_vip", False):
            self.assertTrue(config.effective_val("mam_vip"))
            self.assertTrue(bool(watcher._setting("mam_vip")))


class TestVipSetting(unittest.TestCase):
    def setUp(self):
        config._EFFECTIVE.clear()
        self.addCleanup(config._EFFECTIVE.clear)

    def test_round_trip(self):
        with mock.patch.object(CONFIG, "mam_vip", False):
            self.assertIs(config.effective_val("mam_vip"), False)
            config._EFFECTIVE["vip"] = "YES"
            self.assertIs(config.effective_val("mam_vip"), True)
            self.assertTrue(config.is_custom("mam_vip"))
            for other in ("NO", "", "maybe"):
                config._EFFECTIVE["vip"] = other
                self.assertIs(config.effective_val("mam_vip"), False, other)
            config._EFFECTIVE.pop("vip")
            self.assertIs(config.effective_val("mam_vip"), False)

    def test_env_default_shows_in_setting_env_defaults(self):
        with mock.patch.object(CONFIG, "mam_vip", True):
            self.assertEqual(config.setting_env_defaults()["vip"], "YES")
            self.assertIs(config.effective_val("mam_vip"), True)
        with mock.patch.object(CONFIG, "mam_vip", False):
            self.assertEqual(config.setting_env_defaults()["vip"], "NO")

    def test_env_parsing(self):
        for raw, want in (("true", True), ("1", True), ("false", False), ("", False)):
            with mock.patch.dict("os.environ", {"MAM_VIP": raw}):
                self.assertIs(Config.from_env().mam_vip, want, raw)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MAM_VIP", None)
            self.assertIs(Config.from_env().mam_vip, False)

    def test_setting_tables(self):
        self.assertIn("vip", config.SETTING_KEYS)
        self.assertEqual(config.SETTING_MAP["vip"], "mam_vip")
        self.assertIs(config.SETTING_TYPES["vip"], str)
        self.assertEqual(config.SETTING_CHOICES["vip"], ("NO", "YES"))

    def test_settings_page_has_the_select(self):
        html = web._setting_select("vip", "VIP member", "NO", config.setting_choices("vip"), False, "NO",
                                   hint=web.VIP_HINT, titlecase=True)
        self.assertIn('data-setting="vip"', html)
        self.assertIn("VIP member", html)
        self.assertIn(">No</option>", html)
        self.assertIn(">Yes</option>", html)
        self.assertIn("skipped instead of attempted", html)


if __name__ == "__main__":
    unittest.main()

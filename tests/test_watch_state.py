"""0.18.0-dev1: watcher config, the wishlist state machine and the wedge decision.

Nothing here touches the network.
"""

import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from audiobind import config, mam
from audiobind.config import CONFIG, Config
from audiobind.state import (
    WISHLIST_DOWNLOADING,
    WISHLIST_ERROR,
    WISHLIST_IN_LIBRARY,
    WISHLIST_NOT_FOUND,
    WISHLIST_STATUSES,
    WISHLIST_WANTED,
    State,
    wishlist_transition_ok,
)


def item(asin, title="A Book", authors=("Someone",)):
    return {"asin": asin, "title": title, "authors": list(authors)}


class TestWatcherConfig(unittest.TestCase):
    def test_defaults_leave_the_watcher_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.mam_id, "")
        self.assertEqual(cfg.watch_interval_min, 60)
        self.assertEqual(cfg.watch_max_per_cycle, 5)
        self.assertEqual(cfg.qbit_url, "http://localhost:8083")
        self.assertEqual((cfg.qbit_user, cfg.qbit_pass), ("", ""))

    def test_env_is_read(self):
        env = {"MAM_ID": " abc123 ", "WATCH_INTERVAL_MIN": "90", "WATCH_MAX_PER_CYCLE": "3",
               "QBIT_URL": "http://qbt:8083/", "QBIT_USER": "admin", "QBIT_PASS": "p w"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.mam_id, "abc123")
        self.assertEqual((cfg.watch_interval_min, cfg.watch_max_per_cycle), (90, 3))
        self.assertEqual(cfg.qbit_url, "http://qbt:8083")
        self.assertEqual((cfg.qbit_user, cfg.qbit_pass), ("admin", "p w"))

    def test_secrets_never_reach_repr(self):
        cfg = Config(mam_id="SECRET-MAM-COOKIE", qbit_pass="SECRET-QBIT-PASS")
        self.assertNotIn("SECRET-MAM-COOKIE", repr(cfg))
        self.assertNotIn("SECRET-QBIT-PASS", repr(cfg))

    def test_effective_getters_clamp_to_the_politeness_bounds(self):
        saved = (CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle)
        try:
            CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = 60, 5
            self.assertEqual((config.watch_interval_min(), config.watch_max_per_cycle()), (60, 5))
            CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = 1, 500
            self.assertEqual(config.watch_interval_min(), config.WATCH_INTERVAL_BOUNDS[0])
            self.assertEqual(config.watch_max_per_cycle(), config.WATCH_MAX_PER_CYCLE_BOUNDS[1])
            CONFIG.watch_max_per_cycle = 0
            self.assertEqual(config.watch_max_per_cycle(), 1)
        finally:
            CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = saved


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()


class TestTransitions(unittest.TestCase):
    def test_the_documented_graph(self):
        allowed = {
            (WISHLIST_WANTED, WISHLIST_DOWNLOADING), (WISHLIST_WANTED, WISHLIST_NOT_FOUND),
            (WISHLIST_WANTED, WISHLIST_ERROR), (WISHLIST_WANTED, WISHLIST_IN_LIBRARY),
            (WISHLIST_DOWNLOADING, WISHLIST_IN_LIBRARY), (WISHLIST_DOWNLOADING, WISHLIST_ERROR),
            (WISHLIST_NOT_FOUND, WISHLIST_WANTED), (WISHLIST_NOT_FOUND, WISHLIST_IN_LIBRARY),
            (WISHLIST_ERROR, WISHLIST_WANTED), (WISHLIST_ERROR, WISHLIST_IN_LIBRARY),
        }
        for a in WISHLIST_STATUSES:
            for b in WISHLIST_STATUSES:
                self.assertEqual(wishlist_transition_ok(a, b), (a, b) in allowed, (a, b))

    def test_in_library_is_final_and_downloading_never_goes_back_to_wanted(self):
        self.assertFalse(wishlist_transition_ok(WISHLIST_IN_LIBRARY, WISHLIST_WANTED))
        self.assertFalse(wishlist_transition_ok(WISHLIST_DOWNLOADING, WISHLIST_WANTED))
        self.assertFalse(wishlist_transition_ok(WISHLIST_WANTED, "downloaded"))


class TestWishlistStateMachine(StateCase):
    def test_wanted_downloading_in_library(self):
        self.state.wishlist_add(item("B000000001"))
        self.assertTrue(self.state.wishlist_set_status(
            "B000000001", WISHLIST_DOWNLOADING, expect=WISHLIST_WANTED, note="sent", mam_tid="123",
            torrent_hash="ab" * 20, checked=True))
        row = self.state.wishlist_get("B000000001")
        self.assertEqual(row["status"], WISHLIST_DOWNLOADING)
        self.assertEqual((row["note"], row["mam_tid"], row["torrent_hash"]), ("sent", "123", "ab" * 20))
        self.assertGreater(row["checked_at"], 0)
        self.assertTrue(self.state.wishlist_set_status("B000000001", WISHLIST_IN_LIBRARY))
        self.assertEqual(self.state.wishlist_get("B000000001")["status"], WISHLIST_IN_LIBRARY)

    def test_illegal_moves_are_refused_and_change_nothing(self):
        self.state.wishlist_add(item("B000000001"))
        self.state.wishlist_set_status("B000000001", WISHLIST_IN_LIBRARY)
        before = self.state.wishlist_get("B000000001")
        self.assertFalse(self.state.wishlist_set_status("B000000001", WISHLIST_DOWNLOADING))
        self.assertFalse(self.state.wishlist_set_status("B000000001", "bogus"))
        self.assertFalse(self.state.wishlist_set_status("B0MISSING1", WISHLIST_ERROR))
        self.assertEqual(self.state.wishlist_get("B000000001"), before)

    def test_expect_guards_against_a_row_that_changed_meanwhile(self):
        self.state.wishlist_add(item("B000000001"))
        self.state.wishlist_set_status("B000000001", WISHLIST_ERROR, note="boom")
        self.assertFalse(self.state.wishlist_set_status("B000000001", WISHLIST_IN_LIBRARY,
                                                        expect=WISHLIST_WANTED))
        self.assertEqual(self.state.wishlist_get("B000000001")["status"], WISHLIST_ERROR)

    def test_error_keeps_its_note(self):
        self.state.wishlist_add(item("B000000001"))
        self.state.wishlist_set_status("B000000001", WISHLIST_ERROR, note="qBittorrent refused it")
        row = self.state.wishlist_get("B000000001")
        self.assertEqual((row["status"], row["note"]), (WISHLIST_ERROR, "qBittorrent refused it"))

    def test_retry_resets_not_found_and_error_only(self):
        for asin, status in (("B000000001", WISHLIST_NOT_FOUND), ("B000000002", WISHLIST_ERROR),
                             ("B000000003", WISHLIST_DOWNLOADING)):
            self.state.wishlist_add(item(asin))
            self.state.wishlist_set_status(asin, status, checked=True)
        self.state.wishlist_add(item("B000000004"))
        self.assertTrue(self.state.wishlist_retry("B000000001"))
        self.assertTrue(self.state.wishlist_retry("B000000002"))
        self.assertFalse(self.state.wishlist_retry("B000000003"))
        self.assertFalse(self.state.wishlist_retry("B000000004"), "a wanted row has nothing to retry")
        self.assertFalse(self.state.wishlist_retry("B0MISSING1"))
        for asin in ("B000000001", "B000000002"):
            row = self.state.wishlist_get(asin)
            self.assertEqual((row["status"], row["misses"], row["checked_at"]), (WISHLIST_WANTED, 0, 0.0))
            self.assertIn("retry requested", row["note"])

    def test_record_check_counts_consecutive_misses(self):
        self.state.wishlist_add(item("B000000001"))
        self.assertEqual(self.state.wishlist_record_check("B000000001", "not found on MAM", miss=True), 1)
        self.assertEqual(self.state.wishlist_record_check("B000000001", "not found on MAM", miss=True), 2)
        self.assertEqual(self.state.wishlist_record_check("B000000001", "dry run", miss=False), 0)
        row = self.state.wishlist_get("B000000001")
        self.assertEqual((row["status"], row["note"]), (WISHLIST_WANTED, "dry run"))
        self.assertGreater(row["checked_at"], 0)
        self.state.wishlist_set_status("B000000001", WISHLIST_ERROR)
        self.assertIsNone(self.state.wishlist_record_check("B000000001", "x", miss=True))

    def test_due_is_never_checked_first_then_least_recently_checked(self):
        for n in (1, 2, 3):
            self.state.wishlist_add(item(f"B00000000{n}"))
            time.sleep(0.01)
        self.state.wishlist_record_check("B000000001", "miss", miss=True)
        time.sleep(0.01)
        self.state.wishlist_record_check("B000000002", "miss", miss=True)
        self.state.wishlist_add(item("B000000009"))
        self.state.wishlist_add(item("B000000008"))
        self.state.wishlist_set_status("B000000008", WISHLIST_ERROR)
        due = [r["asin"] for r in self.state.wishlist_due(10)]
        self.assertEqual(due, ["B000000003", "B000000009", "B000000001", "B000000002"])
        self.assertEqual(len(self.state.wishlist_due(2)), 2)

    def test_status_counts(self):
        self.state.wishlist_add(item("B000000001"))
        self.state.wishlist_add(item("B000000002"))
        self.state.wishlist_set_status("B000000002", WISHLIST_ERROR)
        self.assertEqual(self.state.wishlist_status_counts(), {WISHLIST_WANTED: 1, WISHLIST_ERROR: 1})

    def test_a_017_database_is_migrated(self):
        path = pathlib.Path(self.dir.name) / "old"
        path.mkdir()
        conn = sqlite3.connect(path / "audiobind.db")
        conn.executescript(
            "CREATE TABLE wishlist (asin TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL "
            "DEFAULT '[]', added_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'wanted', origin TEXT NOT "
            "NULL DEFAULT '', series TEXT NOT NULL DEFAULT '', sequence TEXT NOT NULL DEFAULT '', hours "
            "REAL NOT NULL DEFAULT 0);"
            "INSERT INTO wishlist (asin, title, added_at) VALUES ('B000000001', 'Old', 1.0);"
        )
        conn.commit()
        conn.close()
        old = State(path)
        try:
            row = old.wishlist_get("B000000001")
            self.assertEqual((row["title"], row["status"], row["note"], row["misses"]), ("Old", "wanted", "", 0))
            self.assertTrue(old.wishlist_set_status("B000000001", WISHLIST_DOWNLOADING))
        finally:
            old.close()


class TestWedgeDecision(unittest.TestCase):
    def test_already_free_never_spends_a_wedge(self):
        for entry in ({"free": True}, {"free": 1}, {"free": "1"}, {"fl_vip": True}, {"fl_vip": "1"},
                      {"personal_freeleech": "2026-11-20 10:00:00"},
                      {"free": False, "fl_vip": False, "personal_freeleech": "2026-11-20 10:00:00"}):
            use, why = mam.wedge_decision(entry)
            self.assertFalse(use, entry)
            self.assertIn("no wedge", why)
            self.assertNotIn("&fl", mam.download_url(42, use))

    def test_not_free_spends_a_wedge(self):
        for entry in ({}, {"free": False, "fl_vip": False, "personal_freeleech": None},
                      {"free": 0, "fl_vip": "0", "personal_freeleech": ""},
                      {"free": "false", "personal_freeleech": "0000-00-00 00:00:00"}):
            use, why = mam.wedge_decision(entry)
            self.assertTrue(use, entry)
            self.assertIn("&fl", why)
            self.assertTrue(mam.download_url(42, use).endswith("?tid=42&fl"))

    def test_one_answer_per_torrent(self):
        """The URL carries &fl for True and only for True - no both-ways logic."""
        self.assertEqual(mam.download_url("42", False), "https://www.myanonamouse.net/tor/download.php?tid=42")
        self.assertEqual(mam.download_url("42", True), "https://www.myanonamouse.net/tor/download.php?tid=42&fl")
        self.assertEqual(mam.download_url(42, True).count("fl"), 1)

    def test_torrent_id_must_be_a_number(self):
        for bad in ("42&fl", "../x", "", "1 2"):
            with self.assertRaises(ValueError):
                mam.download_url(bad, False)


if __name__ == "__main__":
    unittest.main()

"""0.18.0-dev1: the wishlist watcher - cycles, dry run, failures, library match, the off switch.

MAM is a real MamClient over a fake transport (mock clock: no real sleeps); qBittorrent is a
fake object. Nothing here can reach the network.
"""

import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from audiobind import daemon, mam, qbit, watcher
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import State, UnitState

TORRENT = b"d8:announce3:abc4:infod4:name4:book6:lengthi1eee"


def entry(tid, name, author="Andy Weir", free=False, fl_vip=False, pfl=None, numfiles=1, filetype="m4b",
          snatched=0):
    return {"id": tid, "name": name, "author_info": json.dumps({"1": author}), "narrator_info": "{}",
            "series_info": "", "catname": "Audiobooks - Science Fiction", "free": free, "fl_vip": fl_vip,
            "personal_freeleech": pfl, "numfiles": numfiles, "filetype": filetype, "my_snatched": snatched}


def answer(*entries):
    return (200, {}, json.dumps({"data": list(entries)}).encode())


NOTHING = (200, {}, b'{"error": "Nothing returned, out of 0"}')


class FakeClock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Transport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append((method, url))
        if not self.answers:
            raise AssertionError(f"unexpected MAM request: {method} {url}")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class FakeQbit:
    instances = []

    def __init__(self, fail_login=False, fail_add=False):
        self.fail_login, self.fail_add = fail_login, fail_add
        self.added = []
        self.logged_in = self.logged_out = False
        FakeQbit.instances.append(self)

    def login(self):
        if self.fail_login:
            raise qbit.QbitError("connection refused")
        self.logged_in = True

    def add_torrent(self, torrent, filename="x", category=qbit.CATEGORY):
        if self.fail_add:
            raise qbit.QbitError("qBittorrent refused the torrent (HTTP 200, Fails.)")
        self.added.append((torrent, filename, category))
        return "f" * 40

    def logout(self):
        self.logged_out = True


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.patches = [
            mock.patch.object(CONFIG, "mam_id", "test-cookie"),
            mock.patch.object(CONFIG, "qbit_user", "admin"),
            mock.patch.object(CONFIG, "qbit_pass", "pw"),
            mock.patch.object(CONFIG, "watch_max_per_cycle", 5),
            mock.patch.object(CONFIG, "dry_run", False),
        ]
        for p in self.patches:
            p.start()
        self.saved_dry = CONTROL._dry_run
        CONTROL._dry_run = None
        self.w = watcher.Watcher()
        FakeQbit.instances = []
        self.qbit_kwargs = {}

    def tearDown(self):
        self.w.stop()
        CONTROL._dry_run = self.saved_dry
        for p in reversed(self.patches):
            p.stop()
        self.state.close()
        self.dir.cleanup()

    def mam(self, *answers):
        clock = FakeClock()
        self.transport = Transport(answers)
        client = mam.MamClient("test-cookie", transport=self.transport,
                               limiter=mam.RateLimiter(clock=clock, sleep=clock.sleep),
                               backoff=mam.Backoff(clock=time.time))
        self.w._client = client
        return client

    def cycle(self, **qbit_kwargs):
        with mock.patch.object(watcher, "make_qbit", lambda: FakeQbit(**qbit_kwargs)), \
                mock.patch.object(watcher, "make_mam", side_effect=AssertionError("client is preset")):
            return self.w.run_cycle(self.state, trigger="test")

    def want(self, asin="B000000001", title="Project Hail Mary", authors=("Andy Weir",), **extra):
        self.state.wishlist_add({"asin": asin, "title": title, "authors": list(authors), **extra})
        time.sleep(0.002)  # distinct added_at: "oldest first" is observable

    def row(self, asin="B000000001"):
        return self.state.wishlist_get(asin)


class TestCycle(WatcherCase):
    def test_found_not_free_downloads_with_the_wedge_and_goes_downloading(self):
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        summary = self.cycle()
        self.assertEqual(self.transport.calls[1], ("GET", "https://www.myanonamouse.net/tor/download.php?tid=1001&fl"))
        row = self.row()
        self.assertEqual(row["status"], "downloading")
        self.assertEqual((row["mam_tid"], row["torrent_hash"]), ("1001", "f" * 40))
        self.assertIn("freeleech wedge (&fl)", row["note"], "the wedge decision is audited in the note")
        self.assertEqual(FakeQbit.instances[0].added[0][0], TORRENT)
        self.assertEqual(FakeQbit.instances[0].added[0][2], "mam-audiobooks")
        self.assertTrue(FakeQbit.instances[0].logged_out)
        self.assertEqual((summary["searched"], summary["sent"]), (1, 1))

    def test_already_free_downloads_without_the_wedge(self):
        for n, flags in enumerate(({"free": True}, {"fl_vip": True}, {"pfl": "2026-12-01 00:00:00"})):
            asin = f"B00000000{n}"
            self.want(asin)
            self.mam(answer(entry(2000 + n, "Project Hail Mary", **flags)), (200, {}, TORRENT))
            self.cycle()
            self.assertEqual(self.transport.calls[1][1],
                             f"https://www.myanonamouse.net/tor/download.php?tid={2000 + n}", flags)
            self.assertIn("no wedge spent", self.row(asin)["note"])

    def test_not_found_stays_wanted_with_a_note_then_not_found_after_three(self):
        self.want()
        for n in range(1, watcher.MISS_LIMIT + 1):
            self.mam(answer(entry(1, "Something Else Entirely")))
            self.cycle()
            row = self.row()
            if n < watcher.MISS_LIMIT:
                self.assertEqual(row["status"], "wanted")
                self.assertTrue(row["note"].startswith("not found on MAM (checked "), row["note"])
            else:
                self.assertEqual(row["status"], "not_found")
                self.assertIn("press Retry", row["note"])
        self.assertTrue(self.state.wishlist_retry("B000000001"))
        self.assertEqual(self.row()["status"], "wanted")

    def test_no_results_at_all(self):
        self.want()
        self.mam(NOTHING)
        self.cycle()
        self.assertIn("no results", self.row()["note"])
        self.assertEqual(FakeQbit.instances, [], "nothing to send: qBittorrent is never contacted")

    def test_cap_per_cycle_and_oldest_first(self):
        for n in range(7):
            self.want(f"B00000000{n}", title=f"Book Number {n}")
        CONFIG.watch_max_per_cycle = 3
        self.mam(NOTHING, NOTHING, NOTHING)
        summary = self.cycle()
        self.assertEqual(summary["searched"], 3)
        self.assertEqual(len(self.transport.calls), 3)
        checked = [r["asin"] for r in self.state.wishlist_all() if r["checked_at"]]
        self.assertEqual(sorted(checked), ["B000000000", "B000000001", "B000000002"])

    def test_search_failure_ends_the_cycle_and_leaves_rows_untouched(self):
        self.want("B000000001")
        self.want("B000000002")
        self.mam((503, {}, b""))
        summary = self.cycle()
        self.assertIn("HTTP 503", summary["error"])
        self.assertEqual(len(self.transport.calls), 1, "the second row is not searched")
        for asin in ("B000000001", "B000000002"):
            self.assertEqual((self.row(asin)["status"], self.row(asin)["note"]), ("wanted", ""))
        self.assertIn("HTTP 503", self.state.get_meta(watcher.META_LAST_ERROR)["message"])
        self.assertGreater(self.state.get_meta(watcher.META_BACKOFF)["until"], time.time())

    def test_backoff_means_no_request_at_all(self):
        self.want()
        client = self.mam()
        client.backoff.fail()
        summary = self.cycle()
        self.assertIn("backoff", summary["error"])
        self.assertEqual(self.transport.calls, [])

    def test_qbit_down_means_no_download_and_no_wedge(self):
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")))
        summary = self.cycle(fail_login=True)
        self.assertEqual(len(self.transport.calls), 1, "searched, but never downloaded")
        self.assertIn("qBittorrent is not available", summary["error"])
        self.assertEqual(self.row()["status"], "wanted")

    def test_download_failure_is_an_error_that_is_never_retried_on_its_own(self):
        self.want("B000000001")
        self.want("B000000002")
        self.mam(answer(entry(1001, "Project Hail Mary")), (500, {}, b""))
        summary = self.cycle()
        row = self.row("B000000001")
        self.assertEqual(row["status"], "error")
        self.assertIn("a wedge may have been spent", row["note"])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(len(self.transport.calls), 2, "the cycle stops; the second row is not searched")
        # the next cycle never picks an error row up again
        self.assertEqual([r["asin"] for r in self.state.wishlist_due(5)], ["B000000002"])

    def test_qbit_refusal_is_an_error_with_the_reason(self):
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        self.cycle(fail_add=True)
        row = self.row()
        self.assertEqual((row["status"], row["mam_tid"]), ("error", "1001"))
        self.assertIn("qBittorrent did not take it", row["note"])

    def test_a_row_removed_mid_cycle_is_left_alone(self):
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")))
        real = self.state.wishlist_get

        def vanish(asin):
            self.state.wishlist_remove(asin)
            return real(asin)

        with mock.patch.object(self.state, "wishlist_get", side_effect=vanish):
            summary = self.cycle()
        self.assertEqual(len(self.transport.calls), 1, "no download for a row that is gone")
        self.assertEqual(summary.get("skipped"), 1)

    def test_paused_runs_nothing_and_survives_a_restart(self):
        self.want()
        self.mam()
        self.w.set_paused(self.state, True)
        self.assertEqual(self.cycle(), {"skipped": "paused"})
        self.assertEqual(self.transport.calls, [])
        fresh = watcher.Watcher()
        self.assertTrue(fresh.paused(self.state))
        ok, why = fresh.request_cycle(self.state)
        self.assertFalse(ok)
        self.assertIn("paused", why)

    def test_one_cycle_at_a_time(self):
        self.mam()
        self.w._cycle_lock.acquire()
        try:
            self.assertEqual(self.w.run_cycle(self.state), {"skipped": "a cycle is already running"})
            self.assertEqual(self.w.request_cycle(self.state), (False, "a cycle is already running"))
        finally:
            self.w._cycle_lock.release()

    def test_request_cycle_runs_in_a_worker_thread(self):
        self.want()
        self.mam(NOTHING)
        with mock.patch.object(watcher, "make_qbit", lambda: FakeQbit()):
            ok, why = self.w.request_cycle(self.state)
            self.assertTrue(ok, why)
            self.w._worker.join(5)
        self.assertEqual(self.state.get_meta(watcher.META_LAST_CYCLE)["trigger"], "manual")


class TestDryRun(WatcherCase):
    def test_dry_run_logs_the_decision_and_downloads_nothing(self):
        CONTROL.set_dry_run(True)
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")))
        with self.assertLogs("audiobind", level="INFO") as logs:
            summary = self.cycle()
        self.assertEqual(len(self.transport.calls), 1, "searched only - no download request")
        self.assertEqual(FakeQbit.instances, [], "qBittorrent is never contacted in dry run")
        row = self.row()
        self.assertEqual(row["status"], "wanted")
        self.assertIn("dry run", row["note"])
        self.assertIn("would download MAM #1001", row["note"])
        self.assertIn("&fl", row["note"])
        self.assertTrue(any("[dry run]" in line and "would download" in line for line in logs.output))
        self.assertEqual((summary["would_send"], summary["sent"], summary["dry_run"]), (1, 0, True))

    def test_dry_run_from_the_environment_applies_too(self):
        CONFIG.dry_run = True
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")))
        self.cycle()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.row()["status"], "wanted")


class TestLibraryMatch(WatcherCase):
    def convert(self, path, title, author, asin="", status="converted", output="/output/x.m4b"):
        self.state.save(UnitState(path=path, status=status, title=title, author=author, asin=asin, output=output))

    def test_wanted_downloading_in_library(self):
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        self.cycle()
        self.assertEqual(self.row()["status"], "downloading")
        self.convert("ingest:Project Hail Mary [MAM]", "Project Hail Mary", "Andy Weir", asin="B000000001")
        self.assertEqual(watcher.resolve_library(self.state), ["B000000001"])
        row = self.row()
        self.assertEqual(row["status"], "in_library")
        self.assertIn("same ASIN", row["note"])
        self.assertEqual(watcher.resolve_library(self.state), [], "idempotent")

    def test_title_and_author_match_without_the_asin(self):
        self.want("B0OTHERED1", title="The Martian", authors=("Andy Weir",))
        self.convert("Weir/The Martian", "The Martian", "Andy Weir", asin="B0DIFFEREN")
        watcher.resolve_library(self.state)
        self.assertEqual(self.row("B0OTHERED1")["status"], "in_library")
        self.assertIn("same title and author", self.row("B0OTHERED1")["note"])

    def test_only_a_converted_or_published_book_counts(self):
        self.want()
        self.convert("a", "Project Hail Mary", "Andy Weir", asin="B000000001", status="review")
        self.convert("b", "Project Hail Mary", "Andy Weir", asin="B000000001", status="ready", output="")
        self.assertEqual(watcher.resolve_library(self.state), [])
        self.convert("c", "Project Hail Mary", "Andy Weir", asin="B000000001", status="ready",
                     output="/output/phm.m4b")
        self.assertEqual(watcher.resolve_library(self.state), ["B000000001"])

    def test_error_and_not_found_rows_resolve_too(self):
        self.want("B000000001")
        self.want("B000000002", title="The Martian")
        self.state.wishlist_set_status("B000000001", "error", note="x")
        self.state.wishlist_set_status("B000000002", "not_found", note="x")
        self.convert("a", "Project Hail Mary", "Andy Weir", asin="B000000001")
        self.convert("b", "The Martian", "Andy Weir", asin="B000000002")
        self.assertEqual(sorted(watcher.resolve_library(self.state)), ["B000000001", "B000000002"])

    def test_a_scan_resolves_the_wishlist(self):
        self.want()
        self.convert("Weir/PHM", "Project Hail Mary", "Andy Weir", asin="B000000001")
        with mock.patch.object(daemon, "discover_units", return_value=[]), \
                mock.patch.object(daemon.ingest, "discover", return_value=[]), \
                mock.patch.object(CONFIG, "library_root", pathlib.Path(self.dir.name)):
            daemon.scan_once(self.state)
        self.assertEqual(self.row()["status"], "in_library")

    def test_a_cycle_resolves_before_searching(self):
        self.want()
        self.convert("Weir/PHM", "Project Hail Mary", "Andy Weir", asin="B000000001")
        self.mam()
        summary = self.cycle()
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(self.transport.calls, [], "a book you have is never searched for")


class TestOffSwitch(unittest.TestCase):
    """MAM_ID unset: no thread, no network, nothing changes for the rest of the app."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_off_by_default(self):
        self.assertEqual(CONFIG.mam_id, "", "the test environment never sets MAM_ID")
        self.assertFalse(watcher.configured())

    def test_settings_come_through_the_effective_config_getter(self):
        values = {"mam_id": "m", "qbit_user": "u", "qbit_pass": "p", "qbit_url": "http://qbt:9"}
        with mock.patch.object(watcher, "effective_val", side_effect=lambda name: values.get(name, "")) as getter:
            self.assertTrue(watcher.configured())
            self.assertEqual(watcher.disabled_reason(), "")
            self.assertEqual(watcher.make_qbit().url, "http://qbt:9")
        self.assertTrue({"mam_id", "qbit_user", "qbit_pass", "qbit_url"} <= {c.args[0] for c in getter.call_args_list})

    def test_no_thread_and_no_network_when_mam_id_is_unset(self):
        w = watcher.Watcher()
        before = {t.name for t in threading.enumerate()}
        with mock.patch.object(CONFIG, "mam_id", ""), \
                mock.patch.object(watcher, "make_mam", side_effect=AssertionError("no client")), \
                mock.patch.object(watcher, "make_qbit", side_effect=AssertionError("no client")), \
                mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("no network")):
            self.assertFalse(w.start(self.state))
            self.assertEqual(w.run_cycle(self.state), {"skipped": "MAM_ID is not set"})
            ok, why = w.request_cycle(self.state)
            status = w.status(self.state)
        self.assertFalse(ok)
        self.assertIn("MAM_ID", why)
        self.assertFalse(w.alive)
        self.assertNotIn("wishlist-watcher", {t.name for t in threading.enumerate()} - before)
        self.assertEqual((status["configured"], status["enabled"], status["running"]), (False, False, False))

    def test_missing_qbit_credentials_disable_it_with_a_clear_log_line(self):
        w = watcher.Watcher()
        with mock.patch.object(CONFIG, "mam_id", "x"), mock.patch.object(CONFIG, "qbit_user", ""), \
                mock.patch.object(CONFIG, "qbit_pass", ""), \
                mock.patch.object(watcher, "make_mam", side_effect=AssertionError("no client")):
            with self.assertLogs("audiobind", level="WARNING") as logs:
                self.assertFalse(w.start(self.state))
            status = w.status(self.state)
        self.assertTrue(any("DISABLED" in line and "QBIT_USER" in line for line in logs.output))
        self.assertEqual((status["configured"], status["enabled"], status["qbit_credentials"]), (True, False, False))
        self.assertFalse(w.alive)

    def test_start_runs_the_thread_when_configured_and_status_hides_secrets(self):
        w = watcher.Watcher()
        with mock.patch.object(CONFIG, "mam_id", "SECRET-COOKIE"), mock.patch.object(CONFIG, "qbit_user", "a"), \
                mock.patch.object(CONFIG, "qbit_pass", "SECRET-PASS"):
            try:
                self.assertTrue(w.start(self.state))
                self.assertTrue(w.alive)
                self.assertGreater(w.next_at, time.time() + 60, "no cycle right at start")
                status = json.dumps(w.status(self.state))
            finally:
                w.stop()
        self.assertNotIn("SECRET-COOKIE", status)
        self.assertNotIn("SECRET-PASS", status)

    def test_daemon_run_asks_the_watcher_to_start(self):
        class Started(Exception):
            pass

        # the watcher's start() raises to end run() right there, before the endless scan loop
        with mock.patch.object(daemon.watcher_mod.WATCHER, "start", side_effect=Started) as start, \
                mock.patch.object(daemon, "State", return_value=self.state), \
                mock.patch.object(daemon.CONFIG, "ensure_dirs"), \
                mock.patch.object(daemon.CONFIG, "web_enabled", False), \
                mock.patch.object(daemon, "scan_once", side_effect=AssertionError("not reached")):
            with self.assertRaises(Started):
                daemon.run()
        start.assert_called_once_with(self.state)


if __name__ == "__main__":
    unittest.main()

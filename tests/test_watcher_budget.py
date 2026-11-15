"""0.21.0-dev1: the watcher budgets the account - freeleech credits (a wedge is never refunded) and
unsatisfied snatches - and fails closed when qBittorrent cannot be read.

Same fakes as test_watcher.py: a real TrackerClient over a fake transport, a fake qBittorrent. No network.
"""

import pathlib
import tempfile
import time
import unittest
from unittest import mock

from audiobind import qbit, watcher
from audiobind.config import CONFIG
from audiobind.state import State
from tests.test_watcher import FakeQbit, NOTHING, TORRENT, WatcherCase, answer, entry

DAY = 86400


class BudgetCase(WatcherCase):
    def setUp(self):
        super().setUp()
        self.extra = [
            mock.patch.object(CONFIG, "wedge_reserve", 0),
            mock.patch.object(CONFIG, "wedge_per_cycle", 0),
            mock.patch.object(CONFIG, "wedge_per_day", 0),
            mock.patch.object(CONFIG, "watch_max_unsatisfied", 0),
            mock.patch.object(FakeQbit, "snatch_map", {}),
            mock.patch.object(FakeQbit, "fail_snatch", False),
        ]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def set_budget(self, **values):
        for key, value in values.items():
            p = mock.patch.object(CONFIG, key, value)
            p.start()
            self.extra.append(p)

    def spent(self, n, at=None):
        """n rows the watcher already sent with a credit, spent ``at`` (default: now)."""
        for i in range(n):
            asin = f"B0SPENT{i:03d}"
            self.want(asin, title=f"Already Bought {i}")
            self.state.wishlist_set_status(asin, "downloading", tracker_tid=str(900 + i), torrent_hash=f"{i + 1:040x}")
            self.state.mark_wedge_spent(str(900 + i), time.time() if at is None else at)

    def ledger(self):
        return int(self.state.connection.execute("SELECT COALESCE(SUM(wedge_spent), 0) FROM wishlist").fetchone()[0])

    def downloads(self):
        return [c for c in self.transport.calls if "download.php" in c[1]]


class TestWedgeBudget(BudgetCase):
    def test_per_cycle_cap_sends_one_and_skips_the_other(self):
        self.set_budget(wedge_per_cycle=1)
        self.want("B000000001", title="Project Hail Mary")
        self.want("B000000002", title="Artemis")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT),
                          answer(entry(1002, "Artemis")))
        summary = self.cycle()
        self.assertEqual((summary["sent"], summary["skipped_budget"]), (1, 1))
        self.assertEqual(len(self.downloads()), 1, "the second credit is never requested")
        self.assertEqual(self.row("B000000001")["status"], "downloading")
        second = self.row("B000000002")
        self.assertEqual(second["status"], "wanted")
        self.assertIn("1 wedge already spent this cycle", second["note"])
        self.assertEqual(self.ledger(), 1)

    def test_per_day_cap_refuses_the_next_cycle(self):
        self.set_budget(wedge_per_day=2)
        self.spent(2)
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")))
        summary = self.cycle()
        # the search itself is how we learn the torrent needs a credit; no download is ever requested
        self.assertEqual(self.downloads(), [])
        self.assertEqual(summary["skipped_budget"], 1)
        self.assertIn("2 wedges spent in the last 24 hours", self.row("B000000009")["note"])
        self.assertEqual(self.ledger(), 2)

    def test_old_spends_fall_out_of_the_day(self):
        self.set_budget(wedge_per_day=2)
        self.spent(2, at=time.time() - DAY - 60)
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        self.assertEqual(self.cycle()["sent"], 1)

    def test_reserve_floor_refuses_and_names_the_reserve(self):
        self.set_budget(wedge_reserve=1, wedge_per_day=3)
        self.spent(2)
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")))
        summary = self.cycle()
        self.assertEqual(self.downloads(), [])
        self.assertEqual(summary["skipped_budget"], 1)
        self.assertIn("keeping 1 freeleech credit in reserve", self.row("B000000009")["note"])

    def test_reserve_leaves_room_below_the_floor(self):
        self.set_budget(wedge_reserve=1, wedge_per_day=3)
        self.spent(1)
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        self.assertEqual(self.cycle()["sent"], 1)

    def test_already_free_never_consults_the_budget(self):
        self.set_budget(wedge_reserve=1, wedge_per_cycle=1, wedge_per_day=1)  # would refuse any credit
        self.want()
        self.fake_tracker(answer(entry(1001, "Project Hail Mary", free=True)), (200, {}, TORRENT))
        with mock.patch.object(watcher.Budget, "may_spend", side_effect=AssertionError("budget consulted")):
            summary = self.cycle()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(self.downloads()[0][1], "https://tracker.example/download.php?tid=1001")
        self.assertEqual(self.ledger(), 0, "a free torrent costs nothing, so nothing is written")

    def test_unreadable_qbit_blocks_credits_but_not_free_rows(self):
        self.set_budget(wedge_per_day=5)
        FakeQbit.fail_snatch = True
        self.want("B000000001", title="Project Hail Mary")
        self.want("B000000002", title="Artemis")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")),
                          answer(entry(1002, "Artemis", free=True)), (200, {}, TORRENT))
        summary = self.cycle()
        self.assertEqual((summary["sent"], summary["skipped_budget"]), (1, 1))
        self.assertIn("qBittorrent is unreachable, so the wedge budget cannot be checked",
                      self.row("B000000001")["note"])
        self.assertEqual(self.row("B000000001")["status"], "wanted")
        self.assertEqual(self.row("B000000002")["status"], "downloading")
        self.assertEqual(self.ledger(), 0)

    def test_dry_run_never_writes_the_ledger(self):
        self.set_budget(wedge_per_cycle=1)
        self.want("B000000001", title="Project Hail Mary")
        self.want("B000000002", title="Artemis")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), answer(entry(1002, "Artemis")))
        with mock.patch.object(CONFIG, "watch_dry_run", True), self.assertLogs("audiobind", "INFO") as logs:
            summary = self.cycle()
        self.assertEqual(self.ledger(), 0)
        self.assertEqual(self.state.wedges_spent_since(0), 0)
        self.assertEqual(self.downloads(), [])
        self.assertEqual(summary["would_send"], 2, "a dry run reports the decision, it never counts a spend")
        self.assertEqual(summary["skipped_budget"], 0)
        self.assertTrue(any("would spend 1 wedge" in line for line in logs.output))

    def test_log_line_mentions_skips_only_when_there_are_some(self):
        self.set_budget(wedge_per_cycle=1)
        self.want("B000000001", title="Project Hail Mary")
        self.want("B000000002", title="Artemis")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT), answer(entry(1002, "Artemis")))
        with self.assertLogs("audiobind", "INFO") as logs:
            self.cycle()
        self.assertTrue(any(", 1 skipped for the wedge budget, 0 error(s)" in line for line in logs.output))
        self.fake_tracker(NOTHING)
        with self.assertLogs("audiobind", "INFO") as logs:
            self.cycle()
        self.assertFalse(any("wedge budget" in line for line in logs.output))


class TestUnsatisfied(BudgetCase):
    def snatches(self, *pairs):
        """Rows the watcher added, one per (progress, ratio) - None = qBittorrent no longer has it."""
        for i, pair in enumerate(pairs):
            asin = f"B0SNATCH{i:03d}"
            digest = f"{i + 1:040x}"
            self.want(asin, title=f"Snatched {i}")
            self.state.wishlist_set_status(asin, "downloading", tracker_tid=str(800 + i), torrent_hash=digest)
            if pair is not None:
                FakeQbit.snatch_map[digest] = pair

    def budget(self):
        with mock.patch.object(watcher, "make_qbit", lambda: FakeQbit()):
            return self.w._budget(self.state, {})

    def test_hold_at_the_limit_searches_nothing(self):
        self.set_budget(watch_max_unsatisfied=3)
        self.snatches((0.5, 0.0), (1.0, 0.2), (0.1, 0.0))
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker()  # any tracker request raises
        with self.assertLogs("audiobind", "INFO") as logs:
            summary = self.cycle()
        self.assertEqual(summary["searched"], 0)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(summary["held_unsatisfied"], 3)
        self.assertEqual(self.row("B000000009")["status"], "wanted")
        self.assertEqual(sum("holding this cycle" in line for line in logs.output), 1)

    def test_below_the_limit_the_cycle_runs(self):
        self.set_budget(watch_max_unsatisfied=4)
        self.snatches((0.5, 0.0), (1.0, 0.2), (0.1, 0.0))
        self.want("B000000009", title="Project Hail Mary")
        self.fake_tracker(answer(entry(1001, "Project Hail Mary", free=True)), (200, {}, TORRENT))
        self.assertEqual(self.cycle()["sent"], 1)

    def test_a_finished_seeded_torrent_is_satisfied(self):
        self.snatches((1.0, 1.0), (1.0, 2.5))
        self.assertEqual(self.budget().unsatisfied, 0)

    def test_progress_or_ratio_below_one_is_unsatisfied(self):
        self.snatches((1.0, 0.99), (0.99, 3.0), (1.0, 1.0))
        self.assertEqual(self.budget().unsatisfied, 2)

    def test_a_torrent_the_client_forgot_is_satisfied(self):
        self.snatches(None, None, (0.2, 0.0))
        self.assertEqual(self.budget().unsatisfied, 1)

    def test_other_torrents_in_the_category_are_not_ours(self):
        FakeQbit.snatch_map["f" * 40] = (0.1, 0.0)  # in the category, not added by the watcher
        self.assertEqual(self.budget().unsatisfied, 0)


class TestLedger(BudgetCase):
    def test_the_ledger_survives_a_restart(self):
        self.want("B000000001")
        self.state.wishlist_set_status("B000000001", "downloading", tracker_tid="1001", torrent_hash="a" * 40)
        self.state.mark_wedge_spent("1001", time.time())
        self.state.close()
        self.state = State(pathlib.Path(self.dir.name))
        self.assertEqual(self.state.wedges_spent_since(time.time() - DAY), 1)
        self.assertEqual(self.state.wedges_spent_since(time.time() + 60), 0)
        self.assertEqual(self.state.wishlist_snatch_hashes(), ["a" * 40])

    def test_migration_adds_the_columns_to_an_old_database(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(pathlib.Path(d))
            state.connection.execute("ALTER TABLE wishlist DROP COLUMN wedge_spent")
            state.connection.execute("ALTER TABLE wishlist DROP COLUMN wedge_at")
            state.connection.commit()
            state.close()
            state = State(pathlib.Path(d))
            self.assertEqual(state.wedges_spent_since(0), 0)
            state.close()


class TestMaySpend(unittest.TestCase):
    def test_reasons_are_short_operator_sentences(self):
        with mock.patch.object(CONFIG, "wedge_reserve", 0), mock.patch.object(CONFIG, "wedge_per_cycle", 1), \
                mock.patch.object(CONFIG, "wedge_per_day", 3):
            b = watcher.Budget(spent_day=3)
            self.assertEqual(b.may_spend(), (False, "3 wedges spent in the last 24 hours"))
            b = watcher.Budget(spent_day=0)
            self.assertEqual(b.may_spend(), (True, ""))
            b.spent_cycle = 1
            self.assertEqual(b.may_spend(), (False, "1 wedge already spent this cycle"))
            self.assertEqual(watcher.Budget(unknown=True).may_spend(),
                             (False, "qBittorrent is unreachable, so the wedge budget cannot be checked"))

    def test_zero_means_no_limit(self):
        with mock.patch.object(CONFIG, "wedge_reserve", 0), mock.patch.object(CONFIG, "wedge_per_cycle", 0), \
                mock.patch.object(CONFIG, "wedge_per_day", 0):
            b = watcher.Budget(spent_day=500)
            b.spent_cycle = 500
            self.assertEqual(b.may_spend(), (True, ""))


class TestQbitSnatchState(unittest.TestCase):
    def test_one_request_and_defensive_parsing(self):
        client = qbit.QbitClient("http://q.example", "u", "p")
        client.logged_in = True
        data = [{"hash": "ABC", "progress": 1, "ratio": 2.5}, "junk", {"hash": "d", "progress": "x", "ratio": 1},
                {"hash": "e", "progress": 0.5}, {"hash": "F", "progress": 0.5, "ratio": 0}]
        with mock.patch.object(client, "_info", return_value=data) as info:
            out = client.snatch_state("audiobooks")
        info.assert_called_once_with({"category": "audiobooks"})
        self.assertEqual(out, {"abc": (1.0, 2.5), "f": (0.5, 0.0)})


if __name__ == "__main__":
    unittest.main()

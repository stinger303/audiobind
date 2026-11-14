"""0.18.0-dev8: the library Dry run and the watcher's Dry run are two independent switches."""

import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import daemon, watcher, web
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import State

try:
    from live import LiveServerCase
    from test_watcher import TORRENT, FakeQbit, WatcherCase, answer, entry
except ImportError:
    from tests.live import LiveServerCase
    from tests.test_watcher import TORRENT, FakeQbit, WatcherCase, answer, entry

WATCHER_ON = {"mam_id": "x", "qbit_user": "u", "qbit_pass": "p"}


class Switches(unittest.TestCase):
    """Saves and restores both CONTROL overrides and both env floors around each test."""

    def setUp(self):
        for attr in ("_dry_run", "_watcher_dry_run"):
            self.addCleanup(setattr, CONTROL, attr, getattr(CONTROL, attr))
            setattr(CONTROL, attr, None)
        for key in ("dry_run", "watch_dry_run"):
            p = mock.patch.object(CONFIG, key, False)
            p.start()
            self.addCleanup(p.stop)


class TestControlIndependence(Switches):
    def test_each_setter_leaves_the_other_alone(self):
        CONTROL.set_dry_run(True)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (True, False))
        CONTROL.set_watcher_dry_run(True)
        CONTROL.set_dry_run(False)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (False, True))
        CONTROL.set_watcher_dry_run(False)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (False, False))

    def test_each_env_floor_feeds_only_its_own_switch(self):
        with mock.patch.object(CONFIG, "watch_dry_run", True):
            self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (False, True))
        with mock.patch.object(CONFIG, "dry_run", True):
            self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (True, False))

    def test_describe_reports_both(self):
        CONTROL.set_watcher_dry_run(True)
        d = CONTROL.describe()
        self.assertEqual((d["dry_run"], d["watcher_dry_run"]), (False, True))


class TestStartup(Switches):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(pathlib.Path(self.tmp.name))
        self.addCleanup(self.state.close)

    def start(self, **env):
        with mock.patch.object(CONFIG, "dry_run", env.get("dry_run", False)), \
                mock.patch.object(CONFIG, "watch_dry_run", env.get("watch", False)), \
                self.assertLogs("audiobind", "INFO") as logs:
            daemon.restore_controls(self.state)
        return (CONTROL.dry_run, CONTROL.watcher_dry_run), "\n".join(logs.output)

    def test_watch_env_true_starts_the_watcher_dry_whatever_was_saved(self):
        self.state.set_meta("watcher_dry_run", False)
        state, log = self.start(watch=True)
        self.assertEqual(state, (False, True))
        self.assertIn("watcher dry run: on (WATCH_DRY_RUN=true in the environment)", log)

    def test_pipeline_env_true_does_not_dry_the_watcher(self):
        self.state.set_meta("watcher_dry_run", False)
        state, log = self.start(dry_run=True)
        self.assertEqual(state, (True, False))
        self.assertIn("dry run: on (DRY_RUN=true in the environment)", log)
        self.assertIn("watcher dry run: off (saved switch)", log)

    def test_both_survive_a_restart_independently(self):
        for pipeline, watch in ((True, False), (False, True), (True, True), (False, False)):
            self.state.set_meta("dry_run", pipeline)
            self.state.set_meta("watcher_dry_run", watch)
            CONTROL._dry_run = CONTROL._watcher_dry_run = None  # a fresh process
            state, log = self.start()
            self.assertEqual(state, (pipeline, watch))
            self.assertIn("watcher dry run: %s (saved switch)" % ("on" if watch else "off"), log)

    def test_nothing_saved_means_off(self):
        state, log = self.start()
        self.assertEqual(state, (False, False))
        self.assertIn("watcher dry run: off (saved switch)", log)

    def test_ui_can_turn_the_watcher_floor_off_afterwards(self):
        self.start(watch=True)
        CONTROL.set_watcher_dry_run(False)
        self.assertFalse(CONTROL.watcher_dry_run)


class TestCycles(WatcherCase):
    def test_watcher_dry_searches_and_never_downloads_with_pipeline_live(self):
        CONTROL.set_dry_run(False)
        CONTROL.set_watcher_dry_run(True)
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")))
        summary = self.cycle()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertFalse(any("download.php" in url for _, url in self.transport.calls))
        self.assertEqual(FakeQbit.instances, [])
        self.assertEqual((summary["would_send"], summary["sent"], summary["watcher_dry_run"]), (1, 0, True))

    def test_watcher_live_downloads_even_with_the_pipeline_dry(self):
        CONTROL.set_dry_run(True)
        CONTROL.set_watcher_dry_run(False)
        self.want()
        self.mam(answer(entry(1001, "Project Hail Mary")), (200, {}, TORRENT))
        summary = self.cycle()
        self.assertEqual(sum("download.php" in url for _, url in self.transport.calls), 1)
        self.assertEqual((summary["sent"], summary["watcher_dry_run"]), (1, False))
        self.assertEqual(self.row()["status"], "downloading")

    def test_status_reads_the_watcher_switch(self):
        CONTROL.set_dry_run(True)
        self.assertFalse(self.w.status(self.state)["watcher_dry_run"])
        CONTROL.set_dry_run(False)
        CONTROL.set_watcher_dry_run(True)
        self.assertTrue(self.w.status(self.state)["watcher_dry_run"])


class TestControlApiAndPages(LiveServerCase):
    def setUp(self):
        super().setUp()
        for attr in ("_dry_run", "_watcher_dry_run"):
            self.addCleanup(setattr, CONTROL, attr, getattr(CONTROL, attr))
            setattr(CONTROL, attr, None)
        patches = [mock.patch.object(CONFIG, "dry_run", False), mock.patch.object(CONFIG, "watch_dry_run", False)]
        patches += [mock.patch.object(CONFIG, k, v) for k, v in WATCHER_ON.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        watcher.WATCHER._paused = None
        self.addCleanup(setattr, watcher.WATCHER, "_paused", None)

    def control(self, action, value):
        status, body = self.request("/api/control", {"action": action, "value": value})
        self.assertEqual(status, 200, body)

    def metas(self):
        return self.state.get_meta("dry_run"), self.state.get_meta("watcher_dry_run")

    def test_api_toggles_are_independent_in_both_directions(self):
        self.control("dry_run", True)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (True, False))
        self.assertEqual(self.metas(), (True, None))
        self.control("watcher_dry_run", True)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (True, True))
        self.control("dry_run", False)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (False, True))
        self.assertEqual(self.metas(), (False, True))
        self.control("watcher_dry_run", False)
        self.control("dry_run", True)
        self.assertEqual((CONTROL.dry_run, CONTROL.watcher_dry_run), (True, False))
        self.assertEqual(self.metas(), (True, False))

    def test_main_page_button_reflects_only_the_pipeline_switch(self):
        CONTROL.set_watcher_dry_run(True)
        html = web.render(self.state)
        self.assertIn('onclick="dry(true)"', html)
        self.assertNotIn("Dry run is on", html)
        CONTROL.set_watcher_dry_run(False)
        CONTROL.set_dry_run(True)
        html = web.render(self.state)
        self.assertIn('onclick="dry(false)"', html)
        self.assertIn("Dry run is on", html)

    def test_watcher_panel_reflects_only_the_watcher_switch(self):
        CONTROL.set_dry_run(True)
        html = web.render_settings(self.state)
        self.assertIn('id="ab-watch-dry"', html)
        self.assertIn('onclick="abWatcherDry(true)"', html)
        self.assertIn("Watcher dry run is <b>off</b>", html)
        CONTROL.set_dry_run(False)
        CONTROL.set_watcher_dry_run(True)
        html = web.render_settings(self.state)
        self.assertIn('onclick="abWatcherDry(false)"', html)
        self.assertIn('class="btn dry on" id="ab-watch-dry"', html)
        self.assertIn("Watcher dry run is <b>on</b>", html)
        self.assertIn("never downloads", html)

    def test_panel_toggle_is_usable_in_every_combination(self):
        for pipeline in (False, True):
            for watch in (False, True):
                CONTROL.set_dry_run(pipeline)
                CONTROL.set_watcher_dry_run(watch)
                html = web.render_settings(self.state)
                self.assertIn('onclick="abWatcherDry(%s)"' % ("false" if watch else "true"), html)
                self.assertNotIn('id="ab-watch-dry" disabled', html)

    def test_api_watcher_and_wishlist_bar_read_the_watcher_switch(self):
        CONTROL.set_dry_run(True)
        _, body = self.request("/api/watcher", headers={})
        self.assertFalse(body["watcher_dry_run"])
        self.assertNotIn("dry run: decisions are noted", web.render_discover(self.state, "wishlist"))
        CONTROL.set_dry_run(False)
        CONTROL.set_watcher_dry_run(True)
        _, body = self.request("/api/watcher", headers={})
        self.assertTrue(body["watcher_dry_run"])
        self.assertIn("dry run: decisions are noted", web.render_discover(self.state, "wishlist"))


if __name__ == "__main__":
    unittest.main()

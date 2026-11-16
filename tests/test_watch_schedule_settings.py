"""0.18.0-dev11: the watcher interval and books-per-cycle are editable in Settings > Watcher."""

import re
import unittest
from unittest import mock

from audiobind import config, watcher, web
from audiobind.config import CONFIG
from tests.tracker_cfg import tracker_cfg

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class ScheduleCase(LiveServerCase):
    SESSION = "x"

    def setUp(self):
        super().setUp()
        self.saved_watch = (CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle)
        CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = 60, 5
        config._EFFECTIVE.clear()
        watcher.WATCHER._paused = None
        p = tracker_cfg(self.SESSION)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = self.saved_watch
        config._EFFECTIVE.clear()
        super().tearDown()

    def save(self, **kw):
        status, body = self.post({"settings": kw}, path="/api/settings")
        self.assertEqual(status, 200, body)
        return body

    def field(self, key):
        page = web.render_settings(self.state)
        found = re.search(rf'<div class="ab-setting" data-setting="{key}"[^>]*>(.*?)</div></div>', page, re.S)
        self.assertIsNotNone(found, key)
        return found.group(0)

    def schedule_line(self):
        page = web.render_settings(self.state)
        found = re.search(r"<dt>Schedule</dt><dd>(.*?)</dd>", page)
        self.assertIsNotNone(found)
        return found.group(1)


class TestScheduleFields(ScheduleCase):
    def test_fields_sit_in_the_watcher_block_above_run_a_cycle_now(self):
        page = web.render_settings(self.state)
        block = page[page.index('id="watcher"'):]
        for key in ("watch_interval", "watch_per_cycle"):
            self.assertIn(f'data-setting="{key}"', block)
            self.assertLess(block.index(f'data-setting="{key}"'), block.index("Run a cycle now"))
        self.assertIn("Check the wishlist every", block)
        self.assertIn("Books per cycle", block)

    def test_env_default_is_the_value_and_custom_mark_only_after_save(self):
        interval, cap = self.field("watch_interval"), self.field("watch_per_cycle")
        self.assertIn('value="60"', interval)
        self.assertIn('data-initial="60"', interval)
        self.assertIn('value="5"', cap)
        # 0.21.0-dev5: the Watcher section says where a value comes from
        self.assertIn('<span class="ab-src">from the container</span>', interval)
        self.assertIn('<span class="ab-src">from the container</span>', cap)
        self.assertEqual(self.schedule_line(), "every 60 min, at most 5 books per cycle")
        self.save(watch_per_cycle=20)
        self.assertIn('<span class="ab-src ab-src-here">set here</span>', self.field("watch_per_cycle"))
        self.assertIn("abResetSetting('watch_per_cycle')", self.field("watch_per_cycle"))
        self.assertIn('<span class="ab-src">from the container</span>', self.field("watch_interval"))

    def test_ui_bounds_equal_the_clamp_bounds_even_for_an_odd_env_value(self):
        CONFIG.watch_interval_min, CONFIG.watch_max_per_cycle = 5000, 0
        for key, bounds in (("watch_interval", config.WATCH_INTERVAL_BOUNDS),
                            ("watch_per_cycle", config.WATCH_MAX_PER_CYCLE_BOUNDS)):
            self.assertEqual(config.SETTING_BOUNDS[key], bounds)
            self.assertEqual(config.setting_bounds(key), bounds)
            html = self.field(key)
            self.assertIn(f'min="{bounds[0]}" max="{bounds[1]}"', html)
        self.assertIn('value="1440"', self.field("watch_interval"))
        self.assertIn('value="1"', self.field("watch_per_cycle"))


class TestScheduleSave(ScheduleCase):
    def test_per_cycle_save_changes_getter_and_status_line_and_back(self):
        self.assertEqual(config.watch_max_per_cycle(), 5)
        self.save(watch_per_cycle=20)
        self.assertEqual(config.watch_max_per_cycle(), 20)
        self.assertEqual(self.schedule_line(), "every 60 min, at most 20 books per cycle")
        self.save(watch_per_cycle=5)
        self.assertEqual(config.watch_max_per_cycle(), 5)
        self.assertEqual(self.schedule_line(), "every 60 min, at most 5 books per cycle")

    def test_status_reads_the_saved_values_with_no_restart(self):
        self.save(watch_per_cycle=7, watch_interval=30)
        self.assertEqual(self.state.get_meta("setting_watch_per_cycle"), 7)
        st = watcher.WATCHER.status(self.state)
        self.assertEqual((st["max_per_cycle"], st["interval_min"]), (7, 30))

    def test_out_of_bound_values_are_clamped_never_stored_raw(self):
        self.save(watch_per_cycle=0)
        self.assertEqual(self.state.get_meta("setting_watch_per_cycle"), 1)
        self.assertEqual(config.watch_max_per_cycle(), 1)
        self.save(watch_per_cycle=99)
        self.assertEqual(self.state.get_meta("setting_watch_per_cycle"), 20)
        self.assertEqual(config.watch_max_per_cycle(), 20)
        self.save(watch_interval=1)
        self.assertEqual(self.state.get_meta("setting_watch_interval"), 10)
        self.assertEqual(config.watch_interval_min(), 10)
        self.save(watch_interval=99999)
        self.assertEqual(self.state.get_meta("setting_watch_interval"), 1440)

    def test_interval_save(self):
        self.save(watch_interval=30)
        self.assertEqual(config.watch_interval_min(), 30)
        self.assertEqual(self.schedule_line(), "every 30 min, at most 5 books per cycle")

    def test_a_non_number_is_rejected(self):
        status, _ = self.post({"settings": {"watch_interval": "soon"}}, path="/api/settings")
        self.assertEqual(status, 400)

    def test_reset_returns_to_the_env_default(self):
        self.save(watch_per_cycle=20)
        self.post({"reset": True}, path="/api/settings")
        self.assertEqual(config.watch_max_per_cycle(), 5)

    def test_existing_settings_still_save(self):
        self.save(scan_interval=7, nice=3)
        self.assertEqual(config.setting_effective("scan_interval"), 7)


try:
    from test_watcher import WatcherCase, NOTHING
except ImportError:
    from tests.test_watcher import WatcherCase, NOTHING


class TestCycleReadsSavedCap(WatcherCase):
    """A real cycle (fake tracker transport, no network): the cap is re-read every cycle."""

    def setUp(self):
        super().setUp()
        self.saved = config._EFFECTIVE.copy()
        config._EFFECTIVE.clear()
        for n in range(8):
            self.want(f"B00000000{n}", title=f"Book Number {n}")

    def tearDown(self):
        config._EFFECTIVE.clear()
        config._EFFECTIVE.update(self.saved)
        super().tearDown()

    def test_saved_cap_governs_the_next_cycle_then_the_one_after(self):
        self.fake_tracker(*[NOTHING] * 20)
        config._EFFECTIVE["watch_per_cycle"] = 2
        self.assertEqual(self.cycle()["searched"], 2)
        config._EFFECTIVE["watch_per_cycle"] = 4
        self.assertEqual(self.cycle()["searched"], 4)
        config._EFFECTIVE["watch_per_cycle"] = 99  # clamped at use to 20: takes everything that is due
        due = len(self.state.wishlist_due(20))
        self.assertGreater(due, 0)
        self.assertEqual(self.cycle()["searched"], due)


class TestNoMam(ScheduleCase):
    SESSION = ""

    def test_without_a_tracker_the_watcher_block_and_both_fields_are_absent(self):
        page = web.render_settings(self.state)
        self.assertNotIn('data-setting="watch_interval"', page)
        self.assertNotIn('data-setting="watch_per_cycle"', page)
        self.assertNotIn("Run a cycle now</button>", page)
        self.assertIn("Acquisition source (optional)", page)


if __name__ == "__main__":
    unittest.main()

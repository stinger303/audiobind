"""0.18.0-dev7: rotating log file, and no MAM surfaces on a non-MAM install."""

import logging
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import cli, series, watcher, web
from audiobind.config import CONFIG, Config
from audiobind.state import State


def _ours():
    return [h for h in logging.getLogger().handlers if getattr(h, "_audiobind", False)]


def _drop_ours():
    root = logging.getLogger()
    for h in _ours():
        root.removeHandler(h)
        h.close()


class TestLogFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(_drop_ours)
        level = logging.getLogger().level
        self.addCleanup(lambda: logging.getLogger().setLevel(level))

    def test_file_is_created_and_receives_a_line(self):
        path = self.dir / "audiobind.log"
        cli.setup_logging(False, str(path))
        logging.getLogger("audiobind").info("hello file")
        for h in _ours():
            h.flush()
        text = path.read_text(encoding="utf-8")
        self.assertIn("hello file", text)
        self.assertIn("INFO", text)

    def test_both_handlers_active_and_rotation_at_the_limit(self):
        path = self.dir / "audiobind.log"
        cli.setup_logging(False, str(path), max_bytes=300, backups=3)
        self.assertEqual({type(h).__name__ for h in _ours()}, {"StreamHandler", "RotatingFileHandler"})
        for i in range(60):
            logging.getLogger("audiobind").info("line %02d %s", i, "x" * 40)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(names, ["audiobind.log", "audiobind.log.1", "audiobind.log.2", "audiobind.log.3"])

    def test_unwritable_path_degrades_to_console_only(self):
        blocker = self.dir / "afile"
        blocker.write_text("x")
        cli.setup_logging(False, str(blocker / "sub" / "audiobind.log"))  # the parent is a file
        self.assertEqual([type(h).__name__ for h in _ours()], ["StreamHandler"])

    def test_open_failure_warns_once(self):
        with mock.patch("logging.handlers.RotatingFileHandler", side_effect=PermissionError("denied")), \
                self.assertLogs("audiobind", "WARNING") as cm:
            cli.setup_logging(False, str(self.dir / "audiobind.log"))
        self.assertEqual(len(cm.records), 1)
        self.assertEqual([type(h).__name__ for h in _ours()], ["StreamHandler"])

    def test_empty_log_file_writes_nothing(self):
        cli.setup_logging(False, "")
        logging.getLogger("audiobind").info("nothing to disk")
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual([type(h).__name__ for h in _ours()], ["StreamHandler"])

    def test_config_default_and_empty(self):
        with mock.patch.dict(os.environ, {"STATE_DIR": "/st"}):
            os.environ.pop("LOG_FILE", None)
            self.assertEqual(Config.from_env().log_file, "/st/audiobind.log")
            os.environ["LOG_FILE"] = ""
            self.assertEqual(Config.from_env().log_file, "")


ITEM = {"asin": "B000000001", "title": "Some Book", "authors": ["A"], "narrators": [], "hours": 1}
SROW = {"asin": "B000000002", "title": "t"}


class TestNoMam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(pathlib.Path(self.tmp.name))
        self.addCleanup(self.state.close)
        p = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        watcher.WATCHER._paused = None

    def test_unconfigured_has_no_wishlist_surface(self):
        self.assertFalse(watcher.configured())
        for html in (web.render(self.state), web.render_discover(self.state, "search"),
                     web.render_discover(self.state, "wishlist")):
            self.assertNotIn("/discover/wishlist", html)
            self.assertNotIn('id="ab-rail-wanted"', html)
        self.assertNotIn("abWant(", web._disc_row(ITEM, "search"))
        self.assertNotIn("Get this", web._disc_row(ITEM, "search"))
        self.assertNotIn("abSeriesWant", web._series_row(SROW, "1"))
        settings = web.render_settings(self.state)
        self.assertIn("MAM acquisition (optional)", settings)
        for name in ("MAM_ID", "QBIT_URL", "QBIT_USER", "QBIT_PASS"):
            self.assertIn(name, settings)
        self.assertNotIn('id="ab-watch-run"', settings)
        self.assertNotIn('onclick="abWatcher(', settings)
        self.assertIn("whether or not MAM is configured", web.render(self.state))

    def test_wishlist_tab_falls_back_to_search(self):
        self.assertIn('aria-label="Title"', web.render_discover(self.state, "wishlist"))

    def test_configured_shows_everything(self):
        with mock.patch.object(CONFIG, "mam_id", "x"), mock.patch.object(CONFIG, "qbit_user", "u"), \
                mock.patch.object(CONFIG, "qbit_pass", "p"):
            self.assertTrue(watcher.configured())
            self.assertIn('href="/discover/wishlist"', web.render_discover(self.state, "search"))
            self.assertIn('id="ab-rail-wanted"', web.render(self.state))
            self.assertIn("abWant(", web._disc_row(ITEM, "search"))
            self.assertIn("abSeriesWant(", web._series_row(SROW, "1"))
            settings = web.render_settings(self.state)
            self.assertIn('id="ab-watch-run"', settings)
            self.assertNotIn("MAM acquisition (optional)", settings)


if __name__ == "__main__":
    unittest.main()

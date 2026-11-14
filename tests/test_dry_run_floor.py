"""0.18.0-dev6: DRY_RUN=true in the environment is a floor the saved switch cannot lower."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import daemon
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.state import State


class TestDryRunFloor(unittest.TestCase):
    def setUp(self):
        self.saved = CONTROL._dry_run
        self.addCleanup(setattr, CONTROL, "_dry_run", self.saved)
        CONTROL._dry_run = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name))
        self.addCleanup(self.state.close)  # runs before the directory is removed

    def start(self, env, saved):
        if saved is not None:
            self.state.set_meta("dry_run", saved)
        with mock.patch.object(CONFIG, "dry_run", env), self.assertLogs("audiobind", "INFO") as logs:
            daemon.restore_controls(self.state)
            result = CONTROL.dry_run
        return result, "\n".join(logs.output)

    def test_env_true_beats_saved_false(self):
        on, log = self.start(True, False)
        self.assertTrue(on)
        self.assertIn("dry run: on (DRY_RUN=true in the environment)", log)

    def test_env_false_saved_true_is_on(self):
        on, log = self.start(False, True)
        self.assertTrue(on)
        self.assertIn("dry run: on (saved switch)", log)

    def test_env_false_saved_false_is_off(self):
        on, log = self.start(False, False)
        self.assertFalse(on)
        self.assertIn("dry run: off (saved switch)", log)

    def test_ui_can_turn_it_off_afterwards(self):
        self.start(True, None)
        CONTROL.set_dry_run(False)
        self.assertFalse(CONTROL.dry_run)


if __name__ == "__main__":
    unittest.main()

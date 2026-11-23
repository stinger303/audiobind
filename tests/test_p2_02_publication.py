import errno
import os
from pathlib import Path
from unittest import mock

from audiobind import pipeline
from tests.test_d1_data_safety import KEY, PublishCase


class PowerLoss(BaseException):
    pass


class CrossFilesystemPublication(PublishCase):
    def cross_filesystem(self, crash_at=""):
        replace = os.replace

        def move(source, dest):
            source, dest = Path(source), Path(dest)
            if source.parent.name.startswith(".audiobind-publish-") and dest.parent == source.parent.parent:
                raise OSError(errno.EXDEV, "cross-filesystem move")
            replace(source, dest)
            if crash_at and source.suffix == ".part" and dest.name.endswith(crash_at):
                raise PowerLoss

        return mock.patch.object(os, "replace", side_effect=move)

    def test_scratch_unlink_failure_does_not_fail_publication(self):
        unlink = os.unlink

        def remove(path, *args, **kwargs):
            if Path(path).parent.name.startswith(".audiobind-publish-"):
                raise OSError(errno.EACCES, "scratch is busy")
            return unlink(path, *args, **kwargs)

        with self.cross_filesystem(), mock.patch.object(os, "unlink", side_effect=remove):
            with self.assertLogs("audiobind", level="WARNING") as logs:
                self.run_process(KEY, "new")
        self.assertTrue(any("scratch" in line and "scratch is busy" in line for line in logs.output))
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted", row.note)
        self.assertEqual(Path(row.output).read_bytes(), b"new-m4b")

    def test_recovery_rolls_back_first_destination_after_replace(self):
        with self.cross_filesystem(crash_at=".m4b"), self.assertRaises(PowerLoss):
            self.run_process(KEY, "new")
        [(_, _, outcome)] = self.state.pending_publications()
        primary = Path(outcome.output)
        self.assertTrue(primary.exists())
        with mock.patch("audiobind.pipeline.logging.getLogger") as logger:
            pipeline.recover_publications(self.state)
        self.assertFalse(primary.exists(), "interrupted publication orphaned the first destination")
        [aside] = primary.parent.glob(f"{primary.name}.bak-interrupted-*")
        self.assertEqual(aside.read_bytes(), b"new-m4b")
        self.assertIn("publication was incomplete", logger.return_value.warning.call_args.args[0])
        self.assertEqual(self.state.pending_publications(), [])

    def test_recovery_records_complete_set_after_last_replace(self):
        with self.cross_filesystem(crash_at="metadata.json"), self.assertRaises(PowerLoss):
            self.run_process(KEY, "new")
        pipeline.recover_publications(self.state)
        row = self.state.get(KEY)
        self.assertIsNotNone(row, "startup failed to record the verified destination set")
        self.assertEqual(row.status, "converted", row.note)
        self.assertEqual(Path(row.output).read_bytes(), b"new-m4b")
        self.assertEqual(self.state.pending_publications(), [])

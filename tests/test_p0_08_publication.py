"""Interrupt the real file moves, reopen SQLite, and recover before discovery."""
import os
import unittest
from pathlib import Path
from unittest import mock

from audiobind import daemon, pipeline
from audiobind.state import State
from tests.test_d1_data_safety import PublishCase
from tests.test_ingest_pipeline import KEY


class PowerLoss(BaseException):
    pass


class PublicationRecovery(PublishCase):
    def recover(self):
        directory = self.state.path.parent
        self.state.close()
        self.state = State(directory)
        with mock.patch.object(daemon, "discover_units", return_value=[]), \
                mock.patch.object(daemon.ingest, "discover", return_value=[]):
            daemon.scan_once(self.state)

    def interrupted_replace(self, final_name):
        self.run_process(KEY, "original")
        primary = Path(self.state.get(KEY).output)
        paths = [primary, primary.with_suffix(".mp3"), primary.parent / "cover.jpg",
                 primary.parent / "metadata.json"]
        original = {p: p.read_bytes() for p in paths}
        rename, replace = os.rename, os.replace

        def interrupt(move, source, dest):
            move(source, dest)
            if Path(dest).parent == primary.parent and final_name(Path(dest)):
                raise PowerLoss

        with mock.patch.object(os, "rename", side_effect=lambda s, d: interrupt(rename, s, d)), \
                mock.patch.object(os, "replace", side_effect=lambda s, d: interrupt(replace, s, d)), \
                self.assertRaises(PowerLoss):
            self.run_process(KEY, "updated", force=True, manual=True, reencode=True)
        return primary, original

    def test_crash_mid_publication_restores_previous_publication(self):
        checkpoints = {
            "after first backup": lambda p: ".m4b.bak-" in p.name,
            "after first audio placement": lambda p: p.suffix == ".m4b",
        }
        for checkpoint, stop_at in checkpoints.items():
            with self.subTest(checkpoint=checkpoint):
                primary, original = self.interrupted_replace(stop_at)
                self.recover()
                for path, content in original.items():
                    self.assertEqual(path.read_bytes() if path.exists() else None, content, path.name)
                self.assertEqual(self.state.get(KEY).status, "converted")
                self.assertEqual(list(primary.parent.glob("*.bak-*")), [])
                self.assertEqual(self.state.pending_publications(), [])
                self.recover()  # startup can run again after recovery


    def test_crash_after_all_placements_finishes_manifest(self):
        primary, original = self.interrupted_replace(lambda p: p.name == "metadata.json")
        self.recover()
        row = self.state.get(KEY)
        self.assertEqual(primary.read_bytes(), b"updated-m4b")
        self.assertEqual(row.status, "converted")
        self.assertEqual(pipeline.wrote_files(row)[primary.name], pipeline.file_identity(primary))
        self.assertEqual(len(list(primary.parent.glob("*.bak-*"))), len(original))
        self.assertEqual(self.state.pending_publications(), [])


if __name__ == "__main__":
    unittest.main()

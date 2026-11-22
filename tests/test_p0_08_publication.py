"""Interrupt the real file moves, reopen SQLite, and recover before discovery."""
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from audiobind import catalog, daemon, encode, matching, pipeline, web
from audiobind.config import CONFIG
from audiobind.probe import Probe
from audiobind.state import State
from tests.test_d1_data_safety import PublishCase
from tests.test_ingest_pipeline import KEY, PHM


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

    def test_missing_backup_quarantines_without_touching_files(self):
        primary, _ = self.interrupted_replace(lambda p: p.suffix == ".m4b")
        next(primary.parent.glob("metadata.json.bak-*")).unlink()
        before = {p: p.read_bytes() for p in primary.parent.rglob("*") if p.is_file()}

        class ScanReached(BaseException):
            pass

        with mock.patch.object(daemon, "State", return_value=self.state), \
                mock.patch.object(CONFIG, "ensure_dirs"), mock.patch.object(CONFIG, "web_enabled", False), \
                mock.patch.object(daemon, "restore_controls"), mock.patch.object(daemon, "set_effective"), \
                mock.patch.object(daemon.watcher_mod.WATCHER, "start"), \
                mock.patch.object(daemon.watcher_mod.WATCHER, "autostart", False), \
                mock.patch.object(daemon.CONTROL, "_paused"), \
                mock.patch.object(daemon.CONTROL._paused, "is_set", return_value=False), \
                mock.patch.object(daemon, "scan_once", side_effect=ScanReached), \
                self.assertLogs("audiobind", level="WARNING") as logs, self.assertRaises(ScanReached):
            daemon.run()
        self.assertEqual(sum("Interrupted publication quarantined" in line for line in logs.output), 1)
        row = self.state.get(KEY)
        self.assertIn("backup is missing", row.note)
        self.assertEqual(self.state.pending_publications(), [])
        self.recover()

        other = "ingest:" + self.OTHER
        self.BOOK = {**self.BOOK, "title": "Other book"}
        self.tag = "other"
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(catalog, "chapters", return_value=[]))
            stack.enter_context(mock.patch.object(encode, "probe_chapters", return_value=[]))
            probe = Probe(duration=3600.0, codec="mp3", channels="2")
            stack.enter_context(mock.patch.object(pipeline, "probe", return_value=probe))
            match = matching.Match(product=PHM, confidence="HIGH")
            stack.enter_context(mock.patch.object(matching, "match_unit", return_value=match))
            summary = daemon.scan_once(self.state, force=True)
        self.assertEqual(summary["converted"], 1)
        other_output = Path(self.state.get(other).output)
        self.assertEqual(other_output.read_bytes(), b"other-m4b")
        handler = object.__new__(web.Handler)
        handler.state = self.state
        handler._send = mock.Mock()

        def action(name, key, **extra):
            handler._read_json = mock.Mock(return_value={"action": name, "path": key, **extra})
            handler._handle_unit()
            return handler._send.call_args.args[:2]

        self.assertEqual(action("delete", other, confirm=True)[0], 200)
        self.assertFalse(other_output.exists())
        self.assertEqual(action("dismiss_publication_quarantine", KEY)[0], 409)
        status, reply = action("dismiss_publication_quarantine", KEY, confirm=True)
        self.assertEqual(status, 200, reply)
        self.assertIn(b"leaves published files, backups and staged files on disk", reply)
        self.assertEqual(self.state.publication_quarantine(KEY), "")
        self.assertNotIn("quarantined", self.state.get(KEY).note)
        self.assertEqual(before, {p: p.read_bytes() for p in primary.parent.rglob("*") if p.is_file()})

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

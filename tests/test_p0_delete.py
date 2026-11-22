"""Delete must preserve files and rows that it cannot safely remove."""

import json
import os
import sqlite3
import threading
from unittest import mock

from audiobind import pipeline, web
from audiobind.probe import Probe
from audiobind.state import UnitState
from audiobind.units import find_unit
from tests.live import LiveServerCase


class DeleteRegressionTests(LiveServerCase):
    def audited_book(self):
        source = self.lib / "Author" / "Book" / "01.mp3"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        unit = find_unit(self.lib, [], "Author/Book")
        with mock.patch.object(pipeline, "probe", return_value=Probe()), mock.patch.object(
            pipeline.matching, "match_unit", return_value=pipeline.matching.Match()
        ):
            pipeline.record(self.state, pipeline.audit_unit(unit), "review")
        return source

    def delete(self, path="Author/Book", **extra):
        return self.post({"action": "delete", "path": path, "confirm": True, **extra})

    def test_symlinked_cover_is_removed_without_following_it(self):
        source = self.audited_book()
        artifact = self.lib / "other.jpg"
        artifact.write_bytes(b"keep")
        link = source.with_name("cover.jpg")
        link.symlink_to(artifact)
        unit = find_unit(self.lib, [], "Author/Book")
        pipeline.record(self.state, pipeline.Audit(unit, source_ownership=pipeline.source_ownership(unit)),
                        "review")
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertFalse(link.is_symlink())
        self.assertEqual(artifact.read_bytes(), b"keep")

    def test_inventory_accepts_an_internal_parent_alias(self):
        source = self.audited_book()
        moved = self.lib / "Moved"
        source.parent.parent.rename(moved)
        source.parent.parent.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.delete()[0], 409)
        finished = threading.Event()
        run = web.run_manual

        def recheck(*args, **kwargs):
            try:
                return run(*args, **kwargs)
            finally:
                finished.set()

        with mock.patch.object(web, "run_manual", side_effect=recheck), \
                mock.patch.object(pipeline, "probe", return_value=Probe()), \
                mock.patch.object(pipeline.matching, "match_unit", return_value=pipeline.matching.Match()):
            status, body = self.post({"action": "recheck", "path": "Author/Book"})
            self.assertEqual(status, 200, body)
            self.assertTrue(finished.wait(5))
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertFalse((moved / "Book" / source.name).exists())
        self.assertTrue(source.parent.parent.is_symlink())

    def test_fifo_is_left_after_rechecking_changed_inventory(self):
        source = self.audited_book()
        fifo = source.with_name("pipe")
        os.mkfifo(fifo)
        self.assertEqual(self.delete()[0], 409)
        unit = find_unit(self.lib, [], "Author/Book")
        with mock.patch.object(pipeline, "probe", return_value=Probe()), mock.patch.object(
            pipeline.matching, "match_unit", return_value=pipeline.matching.Match()
        ):
            pipeline.process_unit(self.state, unit, manual=True)
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertIn("pipe", body["source_kept_reason"])
        self.assertFalse(source.exists())
        self.assertTrue(fifo.exists())

    def test_escaping_parent_is_a_containment_refusal(self):
        source = self.audited_book()
        moved = self.out / "Author"
        source.parent.parent.rename(moved)
        (self.lib / "Author").symlink_to(moved, target_is_directory=True)
        status, body = self.delete()
        self.assertEqual(status, 400, body)
        self.assertIn("escapes library root", body["error"])
        self.assertTrue((moved / "Book" / source.name).exists())

    def test_p0_01_same_path_replacement_is_kept(self):
        source = self.audited_book()
        source.write_bytes(b"different book")
        status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertIn("re-check", body["error"])
        self.assertEqual(source.read_bytes(), b"different book")
        self.assertIsNotNone(self.state.get("Author/Book"))
    def test_p0_02_audio_arriving_at_rename_survives(self):
        source = self.audited_book()
        late = source.with_name("different.mp3")
        real_move = web.move_aside

        def move(paths, journal):
            if source.parent in paths:
                late.write_bytes(b"different book")
            return real_move(paths, journal)

        with mock.patch.object(web, "move_aside", side_effect=move):
            status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertEqual(late.read_bytes(), b"different book")
        self.assertTrue(source.exists())
        self.assertIsNotNone(self.state.get("Author/Book"))

    def test_p0_03_legacy_confirmation_lists_exact_files_and_can_be_declined(self):
        output = self.out / "Book" / "Book.m4b"
        output.parent.mkdir()
        output.write_bytes(b"replacement")
        cover = output.with_name("cover.jpg")
        cover.write_bytes(b"replacement cover")
        key = "ingest:Book"
        self.state.save(UnitState(path=key, status="converted", output=str(output)))
        status, body = self.delete(key)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["legacy_output_paths"], ["Book/Book.m4b", "Book/cover.jpg"])
        self.assertEqual(output.read_bytes(), b"replacement")
        self.assertEqual(cover.read_bytes(), b"replacement cover")
        status, body = self.delete(key, legacy_output="keep")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "kept")
        self.assertTrue(output.exists())

    def test_p0_09_failed_dismissal_rolls_back_row_removal(self):
        key = "ingest:Book"
        self.state.save(UnitState(path=key, status="review"))
        self.state.connection.execute(
            "CREATE TRIGGER prevent_dismiss BEFORE INSERT ON dismissed "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        handler = object.__new__(web.Handler)
        handler.state = self.state
        handler._read_json = mock.Mock()
        handler._read_json.return_value = {"action": "delete", "path": key, "confirm": True}
        with self.assertRaises(sqlite3.IntegrityError):
            web.Handler._handle_unit(handler)
        self.assertIsNotNone(self.state.get(key))
        self.assertFalse(self.state.is_dismissed(key))

    def test_p0_09_manual_queue_cannot_claim_ingest_during_purge(self):
        key = "ingest:Book"
        output = self.out / "Book" / "Book.m4b"
        output.parent.mkdir()
        output.write_bytes(b"published")
        detail = {"output": {"wrote": [output.name], "wrote_id": {
            output.name: pipeline.file_identity(output),
        }}}
        row = UnitState(path=key, status="converted", output=str(output), detail=json.dumps(detail))
        self.state.save(row)
        real_remove = web.remove_trashed

        def remove(*args):
            queued = web.CONTROL.try_enqueue(key)
            if queued:
                web.CONTROL.dequeue(key)
            self.assertFalse(queued)
            return real_remove(*args)

        with mock.patch.object(web, "remove_trashed", side_effect=remove):
            status, body = self.delete(key)
        self.assertEqual(status, 200, body)
        self.assertTrue(self.state.is_dismissed(key))

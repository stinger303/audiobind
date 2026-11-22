"""0.19.0-dev15 release audit: Delete only removes what Audiobind wrote (H1), a re-check never
re-encodes a converted book (H2), the title score is whole-string (H3), a removed book stays
removed during a scan or a queued action (H4), a failed output removal keeps the row (M1), and
the heartbeat stays fresh while paused or encoding (M2)."""

import contextlib
import json
import time
import unittest
from unittest import mock

from audiobind import catalog, control, daemon, encode, matching, pipeline, web
from audiobind.catalog import Product
from audiobind.control import CONTROL
from audiobind.probe import Probe
from audiobind.state import UnitState
from audiobind.units import BookUnit

try:
    from live import LiveServerCase, audited_row, published_row
    from test_ingest_discovery import touch
    from test_ingest_pipeline import KEY, PHM, TORRENT, PipelineCase
    from test_ingest_web import IngestWebCase
except ImportError:
    from tests.live import LiveServerCase, audited_row, published_row
    from tests.test_ingest_discovery import touch
    from tests.test_ingest_pipeline import KEY, PHM, TORRENT, PipelineCase
    from tests.test_ingest_web import IngestWebCase


# --------------------------------------------------------------------------- H1 / M1
class DeleteOwnershipTests(LiveServerCase):
    def book(self, rel, status, output, detail=None):
        touch(str(self.lib / rel / "01.mp3"))
        row = UnitState(path=rel, status=status, output=str(output), detail=json.dumps(detail or {}))
        self.state.save(audited_row(self.lib, row))

    def test_ready_row_keeps_a_pre_existing_folder(self):
        folder = self.out / "Andy Weir" / "Project Hail Mary"
        existing = folder / "Project Hail Mary.m4b"
        touch(str(existing), b"someone else's m4b")
        touch(str(folder / "cover.jpg"), b"jpg")
        self.book("Andy Weir/Project Hail Mary", "ready", existing)
        status, body = self.post({"action": "delete", "path": "Andy Weir/Project Hail Mary", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["output"], "kept")
        self.assertEqual(body["output_kept_reason"], "that folder was not created by Audiobind")
        self.assertTrue(existing.exists())
        self.assertTrue((folder / "cover.jpg").exists())
        self.assertIsNone(self.state.get("Andy Weir/Project Hail Mary"))

    def test_other_non_converted_statuses_never_remove_the_output(self):
        for status in ("review", "held", "failed", "interrupted"):
            folder = self.out / "A" / status
            touch(str(folder / "B.m4b"), b"m4b")
            self.book(f"A/{status}", status, folder / "B.m4b")
            code, body = self.post({"action": "delete", "path": f"A/{status}", "confirm": True})
            self.assertEqual(code, 200, body)
            self.assertEqual(body["output_kept_reason"], web.OUTPUT_NOT_OURS, status)
            self.assertTrue(folder.exists(), status)

    def test_converted_row_whose_detail_names_another_output_is_kept(self):
        folder = self.out / "A" / "Book"
        touch(str(folder / "Book.m4b"), b"m4b")
        self.book("A/Book", "converted", folder / "Book.m4b",
                  {"output": {"path": str(self.out / "Elsewhere" / "Book.m4b")}})
        status, body = self.post({"action": "delete", "path": "A/Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output_kept_reason"], web.OUTPUT_NOT_OURS)
        self.assertTrue(folder.exists())

    def test_converted_row_whose_detail_agrees_is_removed(self):
        folder = self.out / "A" / "Book"
        touch(str(folder / "Book.m4b"), b"m4b")
        self.book("A/Book", "converted", folder / "Book.m4b", {"output": {"path": str(folder / "Book.m4b")}})
        self.state.save(published_row(self.state.get("A/Book")))
        status, body = self.post({"action": "delete", "path": "A/Book", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "removed")
        self.assertFalse(folder.exists())

    def test_failed_output_removal_keeps_the_row_and_says_so(self):  # M1
        folder = self.out / "A" / "Book"
        touch(str(folder / "Book.m4b"), b"m4b")
        self.book("A/Book", "converted", folder / "Book.m4b")
        self.state.save(published_row(self.state.get("A/Book")))
        with mock.patch.object(web, "remove_trashed", side_effect=PermissionError(13, "Permission denied")):
            status, body = self.post({"action": "delete", "path": "A/Book", "confirm": True})
        self.assertEqual(status, 500, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["output"], "failed")
        self.assertIn("could not be removed", body["error"])
        self.assertIsNotNone(self.state.get("A/Book"))
        self.assertTrue((folder / "Book.m4b").exists())

    def test_the_toast_shows_a_failed_delete(self):  # M1: the page's !ok branch shows the reason
        with open(web.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("if (!result.ok) {\n        abToast(result.error || 'delete failed');", source)


# --------------------------------------------------------------------------- H2 / H4 / M2 pipeline
class PipelineGuards(PipelineCase):
    BOOK = {"title": "Project Hail Mary", "authors": [{"name": "Andy Weir"}]}

    @contextlib.contextmanager
    def patched(self, encode_side_effect=None):
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value=self.BOOK),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=encode_side_effect or self.fake_encode),
        ):
            yield

    def old_layout_row(self):
        old = self.out / "Andy Weir" / "Old Layout Folder" / "Project Hail Mary.m4b"
        touch(str(old), b"old m4b")
        self.state.save(UnitState(path=KEY, status="converted", output=str(old), source="ingest",
                                  title="Project Hail Mary", confidence="HIGH", asin=PHM.asin))
        return old

    @property
    def out(self):
        from audiobind.config import CONFIG
        return CONFIG.output_root

    def unit(self):
        from audiobind import ingest
        return ingest.find_any_unit(KEY)

    # H2 ------------------------------------------------------------------
    def test_forced_recheck_of_a_converted_book_does_not_encode(self):
        self.add_torrent()
        old = self.old_layout_row()
        before = self.state.get(KEY)
        logs = []
        with self.patched(), mock.patch.object(daemon.log, "info", side_effect=lambda m, *a: logs.append(m % a)):
            daemon.scan_once(self.state, force=True)  # Re-check all
        self.assertEqual(self.encode_calls, [])
        row = self.state.get(KEY)
        self.assertEqual((row.status, row.output, row.updated_at), ("converted", str(old), before.updated_at))
        self.assertTrue(any(f"already converted at {old} - run Match Audiobookshelf layout to move it" in m
                            for m in logs), logs)

    def test_manual_retry_of_a_converted_book_does_not_encode(self):
        self.add_torrent()
        self.old_layout_row()
        with self.patched():
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None, manual=True)
        self.assertEqual(self.encode_calls, [])
        self.assertEqual(self.state.get(KEY).status, "converted")

    def test_convert_anyway_re_encodes(self):
        self.add_torrent()
        old = self.old_layout_row()
        with self.patched():
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None, force=True, manual=True,
                                  reencode=True)
        self.assertEqual(len(self.encode_calls), 1)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        self.assertNotEqual(row.output, str(old))

    def test_a_converted_row_whose_file_is_gone_converts_normally(self):
        self.add_torrent()
        self.old_layout_row().unlink()
        with self.patched():
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None)
        self.assertEqual(len(self.encode_calls), 1)

    # H4 ------------------------------------------------------------------
    def test_a_dismissed_key_is_never_audited_or_recorded(self):
        self.add_torrent()
        self.state.dismiss(KEY)
        with mock.patch.object(pipeline, "probe", side_effect=AssertionError("probed")):
            audit = pipeline.process_unit(self.state, self.unit(), log=lambda *_: None, force=True, reencode=True)
        self.assertEqual(audit.action, "dismissed")
        self.assertIsNone(self.state.get(KEY))

    def test_dismissed_during_the_check_is_not_converted_or_recorded(self):
        self.add_torrent()
        real_search = [PHM]

        def search(**_kw):
            self.state.dismiss(KEY)  # Delete lands while the catalog is being asked
            return real_search

        with self.patched(), mock.patch.object(catalog, "search", side_effect=search):
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None)
        self.assertEqual(self.encode_calls, [])
        self.assertIsNone(self.state.get(KEY))

    def test_dismissed_while_waiting_for_the_encoder_is_not_converted(self):
        self.add_torrent()
        audit = pipeline.Audit(unit=self.unit(), match=matching.Match(product=PHM), action="convert")
        with self.patched():
            result = pipeline.convert_unit(audit, log=lambda *_: None, cancelled=lambda: True)
        self.assertIsNone(result)
        self.assertEqual(self.encode_calls, [])

    def test_a_scan_skips_a_key_dismissed_mid_scan(self):
        self.add_torrent()
        self.add_torrent("Second Torrent")
        calls = []
        real = pipeline.process_unit

        def first_dismisses(state, unit, **kw):
            calls.append(unit.state_key)
            if unit.state_key == "ingest:Second Torrent":
                state.dismiss(KEY)  # deleted while the scan was on the other book
            return real(state, unit, **kw)

        with self.patched(), mock.patch.object(daemon, "process_unit", side_effect=first_dismisses), \
                mock.patch.object(daemon, "skip_dismissed", side_effect=lambda s, units: sorted(
                    units, key=lambda u: u.state_key != "ingest:Second Torrent")):
            daemon.scan_once(self.state)
        self.assertIn(KEY, calls)
        self.assertIsNone(self.state.get(KEY))

    # M2 ------------------------------------------------------------------
    def test_encode_progress_keeps_the_heartbeat_fresh(self):
        self.add_torrent()
        beats = []

        def encoding(*args, **kw):
            CONTROL.set_progress(percent=10.0)
            CONTROL.set_progress(percent=20.0)  # throttled: one beat per HEARTBEAT_EVERY_S
            return self.fake_encode(*args, **kw)

        with self.patched(encode_side_effect=encoding), \
                mock.patch.object(self.state, "heartbeat", side_effect=lambda: beats.append("encode")):
            pipeline.process_unit(self.state, self.unit(), log=lambda *_: None)
        # one from the encode's progress, one at the end of process_unit
        self.assertEqual(beats, ["encode", "encode"])
        self.assertIsNone(CONTROL._heartbeat)  # removed once the encode ends


class ControlHeartbeatTests(unittest.TestCase):
    def tearDown(self):
        CONTROL.set_heartbeat(None)
        CONTROL.clear_progress()

    def test_throttled_and_resilient(self):
        calls = []
        CONTROL.set_heartbeat(lambda: calls.append(1))
        with mock.patch.object(control.time, "monotonic", side_effect=[100.0, 110.0, 131.0]):
            CONTROL.set_progress(percent=1)
            CONTROL.set_progress(percent=2)
            CONTROL.set_progress(percent=3)
        self.assertEqual(len(calls), 2)
        CONTROL.set_heartbeat(mock.Mock(side_effect=RuntimeError("db locked")))
        CONTROL.set_progress(percent=4)  # never raises into the encode


class PausedTickTests(unittest.TestCase):
    def test_paused_loop_beats(self):
        state = mock.Mock()
        with mock.patch.object(CONTROL, "wait"), mock.patch.object(daemon, "refresh_series"), \
                mock.patch.object(daemon, "set_effective"):
            daemon.paused_tick(state, wait_s=0)
        state.heartbeat.assert_called_once()


# --------------------------------------------------------------------------- H4 web
class DismissWebTests(IngestWebCase):
    def setUp(self):
        super().setUp()
        touch(str(self.ingest_root / TORRENT / "01.mp3"))
        CONTROL._queue.clear()

    def tearDown(self):
        CONTROL._queue.clear()
        super().tearDown()

    def test_delete_refuses_a_queued_book(self):
        out = self.out / "Andy Weir" / "Project Hail Mary" / "Project Hail Mary.m4b"
        touch(str(out), b"m4b")
        self.state.save(UnitState(path=KEY, status="converted", output=str(out), source="ingest"))
        CONTROL.enqueue(KEY)
        status, body = self.post({"action": "delete", "path": KEY, "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("waiting for a manual action", body["error"])
        self.assertTrue(out.exists())
        self.assertIsNotNone(self.state.get(KEY))
        self.assertFalse(self.state.is_dismissed(KEY))

    def manual(self, payload):
        seen = []
        def fake(state, unit, **kw):
            seen.append(kw)
            CONTROL.dequeue(unit.state_key)
            kw["slot"].release()  # the real run_manual owns and releases the slot

        with mock.patch("audiobind.web.run_manual", side_effect=fake):
            status, body = self.post(payload)
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.02)
        return status, body, seen

    def test_retry_on_a_dismissed_key_brings_it_back(self):
        self.state.dismiss(KEY)
        status, body, seen = self.manual({"action": "retry", "path": KEY})
        self.assertEqual(status, 200, body)
        self.assertFalse(self.state.is_dismissed(KEY))
        self.assertFalse(seen[0]["reencode"])

    def test_set_asin_on_a_dismissed_key_brings_it_back(self):
        self.state.dismiss(KEY)
        status, body, _seen = self.manual({"action": "set_asin", "path": KEY, "asin": "B08G9PRS1K"})
        self.assertEqual(status, 200, body)
        self.assertFalse(self.state.is_dismissed(KEY))

    def test_only_convert_anyway_asks_for_a_reencode(self):  # H2 wiring
        status, body, seen = self.manual({"action": "force", "path": KEY})
        self.assertEqual(status, 200, body)
        self.assertTrue(seen[0]["force"])
        self.assertTrue(seen[0]["reencode"])
        status, body, seen = self.manual({"action": "recheck", "path": KEY})
        self.assertEqual(status, 200, body)
        self.assertFalse(seen[0]["reencode"])


# --------------------------------------------------------------------------- H3
def prod(asin, title, author, hours=10.0):
    return Product(asin=asin, title=title, authors=[author] if author else [], hours=hours, language="english")


def run(title, found, author):
    unit = BookUnit(path="X", name="X", parent="", parts=[], title_guess=title, author_guess=author)
    with mock.patch.object(catalog, "search", lambda **kw: list(found)), \
            mock.patch.object(catalog, "book", lambda asin: {}):
        return matching.match_unit(unit)


HERBERT, KING = "Frank Herbert", "Stephen King"
DUNE = prod("B0DUNE0001", "Dune", HERBERT)
MESSIAH = prod("B0DUNE0002", "Dune Messiah", HERBERT)
CHILDREN = prod("B0DUNE0003", "Children of Dune", HERBERT)
IT = prod("B0KING0001", "It", KING)
BLEEDS = prod("B0KING0002", "If It Bleeds", KING)


class TitleScoreTests(unittest.TestCase):
    def test_dune_messiah_alone_never_grades_dune_high(self):
        m = run("Dune Messiah", [DUNE], HERBERT)
        self.assertNotEqual(m.confidence, "HIGH")
        self.assertLess(m.title_sim, 1.0)

    def test_dune_messiah_beats_dune_when_both_are_returned(self):
        for found in ([DUNE, MESSIAH], [MESSIAH, DUNE]):
            m = run("Dune Messiah", found, HERBERT)
            self.assertEqual(m.asin, MESSIAH.asin)
            self.assertEqual(m.confidence, "HIGH")

    def test_children_of_dune(self):
        self.assertNotEqual(run("Children of Dune", [DUNE], HERBERT).confidence, "HIGH")
        m = run("Children of Dune", [DUNE, CHILDREN], HERBERT)
        self.assertEqual((m.asin, m.confidence), (CHILDREN.asin, "HIGH"))

    def test_if_it_bleeds_is_not_it(self):
        self.assertNotEqual(run("If It Bleeds", [IT], KING).confidence, "HIGH")
        m = run("If It Bleeds", [IT, BLEEDS], KING)
        self.assertEqual((m.asin, m.confidence), (BLEEDS.asin, "HIGH"))

    def test_no_author_window_match_stays_low(self):
        # Leviathan-style: no author in the folder, the catalog title is one window of it
        for wanted in ("Leviathan Wakes", "Leviaithan Wakes"):
            m = run(wanted, [prod("B0LEVI0001", "Leviathan", "Scott Westerfeld")], "")
            self.assertEqual(m.confidence, "LOW", wanted)

    def test_dcc_folder_still_reaches_high(self):
        dcc = prod("B08V8B2CGV", "Dungeon Crawler Carl", "Matt Dinniman", 13.7)
        m = run("Dungeon Crawler Carl 01 - Dungeon Crawler Carl", [dcc], "Matt Dinniman")
        self.assertEqual((m.asin, m.title_sim, m.confidence), (dcc.asin, 1.0, "HIGH"))

    def test_private_cases_still_rejected(self):
        for wanted, wrong in (("Private", "Step on a Crack"),
                              ("Private London", "The Country Road Murders"),
                              ("Private: #1 Suspect", "Judge Stone")):
            m = run(wanted, [prod("B0WRONG001", wrong, "James Patterson")], "James Patterson")
            self.assertIsNone(m.product, wanted)
            self.assertIn("needs 0.60)", m.rejected)

    def test_variants(self):
        self.assertEqual(
            matching.title_variants("Dungeon Crawler Carl 01 - Dungeon Crawler Carl [TRACKER] (Unabridged)"),
            ["Dungeon Crawler Carl 01 - Dungeon Crawler Carl [TRACKER] (Unabridged)",
             "Dungeon Crawler Carl 01 - Dungeon Crawler Carl", "Dungeon Crawler Carl"])
        self.assertIn("Project Hail Mary", matching.title_variants("03 - Project Hail Mary"))
        self.assertIn("Project Hail Mary", matching.title_variants("3. Project Hail Mary"))
        # X and Y unalike: "<X> NN - <Y>" is not collapsed to Y
        self.assertNotIn("Leviathan Wakes", matching.title_variants("The Expanse 01 - Leviathan Wakes"))

    def test_ties_break_on_raw_title_then_runtime(self):
        a = {"score": 0.9, "whole_sim": 0.8, "runtime_delta_pct": 2}
        b = {"score": 0.9, "whole_sim": 0.9, "runtime_delta_pct": 40}
        c = {"score": 0.9, "whole_sim": 0.9, "runtime_delta_pct": -3}
        ranked = sorted([a, b, c], key=matching._rank_key, reverse=True)
        self.assertEqual(ranked, [c, b, a])

    def test_window_only_floor_never_grades_high(self):
        m = matching.Match(product=DUNE, title_sim=0.95, author_sim=1.0, author_known=True, margin=0.5,
                           title_window_coverage=0.5)
        # 0.21.0-dev11: window-only is held at LOW, not MEDIUM (MEDIUM can auto-convert)
        self.assertEqual(matching.grade(m), "LOW")
        m.title_window_coverage = 1.0
        self.assertEqual(matching.grade(m), "HIGH")


# --------------------------------------------------------------------------- M4
class CoverPruneTests(unittest.TestCase):
    def setUp(self):
        import pathlib
        import tempfile

        from audiobind import covers
        from audiobind.config import CONFIG
        from audiobind.state import State

        self.covers = covers
        self.dir = tempfile.TemporaryDirectory()
        self.saved = CONFIG.state_dir
        CONFIG.state_dir = pathlib.Path(self.dir.name)
        self.state = State(pathlib.Path(self.dir.name) / "db")
        covers._writes_since_prune, covers._last_prune = 0, 0.0

    def tearDown(self):
        from audiobind.config import CONFIG

        CONFIG.state_dir = self.saved
        self.state.close()
        self.dir.cleanup()

    def test_library_set_is_built_once_not_per_asin(self):
        self.state.save(UnitState(path="A/B", status="review", asin="B0LIB00001"))
        self.state.save(UnitState(path="A/C", status="review", asin=""))
        self.state.set_asin_override("A/C", "B0PIN00001")
        self.assertEqual(self.state.library_asins(), {"B0LIB00001", "B0PIN00001"})
        self.state.catalog_seen_put([{"asin": f"B0SEEN{i:04d}"} for i in range(50)])
        self.covers.cache_dir().mkdir(parents=True, exist_ok=True)
        with mock.patch.object(self.state, "has_asin", side_effect=AssertionError("per-ASIN lookup")):
            self.covers.prune(self.state)

    def test_downloads_prune_every_n_writes_or_minutes(self):
        c = self.covers
        with mock.patch.object(c, "prune") as prune, \
                mock.patch.object(c.time, "monotonic", side_effect=[1000.0 + i for i in range(c.PRUNE_EVERY_WRITES + 2)]):
            for _ in range(c.PRUNE_EVERY_WRITES + 1):
                c.maybe_prune(self.state)
        # the first write prunes (nothing pruned yet), then once more after PRUNE_EVERY_WRITES
        self.assertEqual(prune.call_count, 2)
        with mock.patch.object(c, "prune") as prune, \
                mock.patch.object(c.time, "monotonic", return_value=c._last_prune + c.PRUNE_EVERY_SECONDS + 1):
            self.assertTrue(c.maybe_prune(self.state))
        prune.assert_called_once()

    def test_indexes_exist(self):
        names = {r[0] for r in self.state.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='units'")}
        self.assertTrue({"idx_units_asin", "idx_units_asin_override"} <= names)


if __name__ == "__main__":
    unittest.main()

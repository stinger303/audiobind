"""0.21.0-dev11 matching fixes: window coverage follows floor_sim, window-only never MEDIUM,
a dead ASIN pin gives up after a bounded number of checks."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import matching, pipeline
from audiobind.catalog import Product
from audiobind.probe import Probe
from audiobind.state import State
from audiobind.units import BookUnit, Part


class TestWindowCoverageFollowsFloorSim(unittest.TestCase):
    WANTED = "Moneyball The Art of Winning an Unfair Game"

    def test_subtitle_behind_the_gate_does_not_widen_coverage(self):
        # the title alone ("Zzyzx") is nowhere in the folder name, so floor_sim never scores the
        # subtitle - and the coverage must not take its 7-word window from it either
        decoy = Product(asin="B0DECOY", title="Zzyzx", subtitle="The Art of Winning an Unfair Game")
        self.assertLess(matching.title_alone(self.WANTED, decoy), matching.SUBTITLE_GATE)
        self.assertEqual(matching.floor_candidates(self.WANTED, decoy), ["Zzyzx"])
        bare = Product(asin="B0DECOY", title="Zzyzx")
        self.assertEqual(matching.window_coverage(self.WANTED, decoy), matching.window_coverage(self.WANTED, bare))
        self.assertLess(matching.window_coverage(self.WANTED, decoy), matching.MIN_HIGH_WINDOW_COVERAGE)
        self.assertEqual(matching.floor_sim(self.WANTED, decoy), matching.floor_sim(self.WANTED, bare))

    def test_subtitle_past_the_gate_still_counts(self):
        # "Moneyball" is a window of the folder name: the subtitle is scored, and covers the rest
        real = Product(asin="B005EHN61O", title="Moneyball", subtitle="The Art of Winning an Unfair Game")
        self.assertGreaterEqual(matching.title_alone(self.WANTED, real), matching.SUBTITLE_GATE)
        self.assertIn("Moneyball The Art of Winning an Unfair Game", matching.floor_candidates(self.WANTED, real))
        self.assertEqual(matching.floor_sim(self.WANTED, real), 1.0)
        self.assertEqual(matching.window_coverage(self.WANTED, real), 1.0)

    def test_coverage_window_is_the_floor_sim_window(self):
        # the window window_coverage picks scores exactly floor_sim
        for title, subtitle in (("Dune", "Dune, Book 1"), ("Zzyzx", "Dune Messiah"), ("Heir of Novron", "")):
            product = Product(asin="B0X", title=title, subtitle=subtitle)
            wanted = "Dune Messiah Heir of Novron"
            tokens = wanted.split()
            n = round(matching.window_coverage(wanted, product) * len(tokens))
            best = max(
                matching.sim(" ".join(tokens[i:i + n]), c)
                for i in range(len(tokens) - n + 1)
                for c in matching.floor_candidates(wanted, product)
            )
            self.assertEqual(best, round(matching.floor_sim(wanted, product), 3), (title, subtitle))


DEAD, LIVE, NEW = "B0DEADPIN1", "B0LIVEPIN1", "B0NEWPIN01"
KEY = "Frank Herbert/Frank Herbert - Dune"
DUNE = Product(asin="B002V1OF70", title="Dune", authors=["Frank Herbert"], hours=21.0, language="english")
LIVE_BOOK = {"asin": LIVE, "title": "Dune", "authors": [{"name": "Frank Herbert"}],
             "runtimeLengthMin": 1263, "language": "english"}


def dune_unit():
    path = "/library/" + KEY
    return BookUnit(path=path, name="Frank Herbert - Dune", parent="/library/Frank Herbert",
                    parts=[Part("Frank Herbert - Dune", [path + "/01.mp3"])], rel_path=KEY,
                    author_guess="Frank Herbert", title_guess="Dune")


class TestDeadPinGivesUp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_dir = pathlib.Path(self.dir.name) / "state"
        self.state = State(self.state_dir)
        self.converted = []
        patches = [
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=21.0 * 3600, codec="mp3", channels="2")),
            mock.patch.object(pipeline.os.path, "getsize", return_value=1),
            mock.patch.object(matching.catalog, "book", lambda asin: dict(LIVE_BOOK) if asin == LIVE else {}),
            mock.patch.object(matching.catalog, "search", lambda *a, **k: [DUNE]),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "effective_auto_tiers", return_value={"HIGH", "MEDIUM"}),
            mock.patch.object(pipeline, "convert_unit", side_effect=self._convert),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def _convert(self, audit, **_kw):
        self.converted.append(audit.match.asin)
        return None

    def check(self):
        audit = pipeline.process_unit(self.state, dune_unit(), log=lambda *_: None)
        row = self.state.get(KEY)
        return audit, row, json.loads(row.detail)["match"]

    def test_match_unit_gives_up_on_the_third_failure(self):
        for before in range(matching.PIN_GIVE_UP_AFTER - 1):
            m = matching.match_unit(dune_unit(), DEAD, pin_failures=before)
            self.assertEqual((m.asin_pin_failed, m.product, m.pin_abandoned), (DEAD, None, ""))
        m = matching.match_unit(dune_unit(), DEAD, pin_failures=matching.PIN_GIVE_UP_AFTER - 1)
        self.assertEqual((m.asin, m.asin_pin_failed, m.pin_abandoned, m.asin_override), (DUNE.asin, "", DEAD, False))
        self.assertEqual(m.confidence, "HIGH")  # graded normally

    def test_three_chained_rechecks_end_in_a_normal_match_with_a_note(self):
        self.state.set_asin_override(KEY, DEAD)
        for n in (1, 2):
            audit, row, match = self.check()
            self.assertEqual(match["asin_pin_failed"], DEAD)
            self.assertIsNone(audit.match.product)
            self.assertEqual(row.status, "review")
            self.assertEqual(self.state.get_pin_failures(KEY), n)
        # a restart in the middle: the count lives in the database
        self.state.close()
        self.state = State(self.state_dir)
        self.assertEqual(self.state.get_pin_failures(KEY), 2)
        audit, row, match = self.check()
        self.assertEqual(audit.match.asin, DUNE.asin)
        self.assertEqual(audit.confidence, "HIGH")
        self.assertEqual(match["asin_pin_abandoned"], DEAD)
        self.assertEqual(match["asin_pin_failed"], "")
        self.assertIn(f"ASIN {DEAD} you set had no catalog data on 3 checks in a row", row.note)
        # HIGH in an auto tier, but never converted unseen: the operator asked for another edition
        self.assertEqual(self.converted, [])
        self.assertEqual(row.status, "review")
        # re-checking again neither resets the count nor brings the dead-pin message back
        audit, row, match = self.check()
        self.assertEqual(self.state.get_pin_failures(KEY), 4)
        self.assertEqual(match["asin_pin_abandoned"], DEAD)
        self.assertEqual(self.state.get_asin_override(KEY), DEAD)  # the pin itself is kept

    def test_a_new_pin_resets_the_count(self):
        self.state.set_asin_override(KEY, DEAD)
        self.check()
        self.check()
        self.assertEqual(self.state.get_pin_failures(KEY), 2)
        self.state.set_asin_override(KEY, NEW)
        self.assertEqual(self.state.get_pin_failures(KEY), 0)
        audit, _row, match = self.check()  # NEW is dead too: back to the first failure
        self.assertEqual(match["asin_pin_failed"], NEW)
        self.assertEqual(self.state.get_pin_failures(KEY), 1)

    def test_unlocking_the_asin_resets_the_count(self):
        self.state.set_asin_override(KEY, DEAD)
        self.state.lock_fields(KEY, {"asin"})
        self.check()
        self.assertTrue(self.state.unlock_field(KEY, "asin"))
        self.assertEqual(self.state.get_pin_failures(KEY), 0)

    def test_a_live_pin_is_unaffected(self):
        self.state.set_asin_override(KEY, LIVE)
        for _ in range(matching.PIN_GIVE_UP_AFTER + 1):
            audit, row, match = self.check()
            self.assertTrue(audit.match.asin_override)
            self.assertEqual(audit.match.asin, LIVE)
            self.assertEqual(match["asin_pin_abandoned"], "")
        self.assertEqual(self.state.get_pin_failures(KEY), 0)
        self.assertEqual(self.converted, [LIVE] * (matching.PIN_GIVE_UP_AFTER + 1))

    def test_a_pin_that_comes_back_starts_over(self):
        self.state.set_asin_override(KEY, LIVE)
        with mock.patch.object(matching.catalog, "book", lambda asin: {}):  # a catalog hiccup
            self.check()
            self.check()
        self.assertEqual(self.state.get_pin_failures(KEY), 2)
        audit, _row, _match = self.check()
        self.assertEqual(audit.match.asin, LIVE)
        self.assertEqual(self.state.get_pin_failures(KEY), 0)

    def test_save_never_touches_the_count(self):
        self.state.set_asin_override(KEY, DEAD)
        self.state.record_pin_failure(KEY)
        row = self.state.get(KEY)
        self.state.save(row)
        self.assertEqual(self.state.get_pin_failures(KEY), 1)


if __name__ == "__main__":
    unittest.main()

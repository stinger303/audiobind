"""0.19.0-dev3/dev4: a catalog candidate whose title does not resemble the wanted one is no match."""

import unittest
from unittest import mock

from audiobind import advice, catalog, matching, pipeline
from audiobind.catalog import Product
from audiobind.units import BookUnit


def prod(asin, title, author="James Patterson", hours=10.0):
    return Product(asin=asin, title=title, authors=[author], hours=hours, language="english")


def run(title, found, author="James Patterson"):
    unit = BookUnit(path="X", name="X", parent="", parts=[], title_guess=title, author_guess=author)
    with mock.patch.object(catalog, "search", lambda **kw: [found]), \
            mock.patch.object(catalog, "book", lambda asin: {}):
        return matching.match_unit(unit)


class TestFloor(unittest.TestCase):
    def test_the_three_real_wrong_matches_are_no_match(self):
        for wanted, wrong in (("Private", "Step on a Crack"),
                              ("Private London", "The Country Road Murders"),
                              ("Private: #1 Suspect", "Judge Stone")):
            m = run(wanted, prod("B0WRONG001", wrong))
            self.assertIsNone(m.product, wanted)
            self.assertEqual(m.confidence, "NONE")
            self.assertIn(f"no catalog match: closest was '{wrong}' (title ", m.rejected)
            self.assertIn("needs 0.60)", m.rejected)

    def test_extra_words_in_the_folder_name_still_match(self):
        m = run("Project Hail Mary [TRACKER]", prod("B08G9PRS1K", "Project Hail Mary", "Andy Weir"), "Andy Weir")
        self.assertEqual(m.asin, "B08G9PRS1K")
        self.assertEqual(m.rejected, "")

    def test_a_near_title_still_matches(self):
        m = run("Count to Ten", prod("B0COUNT001", "Count to Ten aka Private Delhi"))
        self.assertGreaterEqual(m.title_sim, 0.60)
        self.assertIsNotNone(m.product)
        self.assertEqual(m.rejected, "")

    def test_a_long_mangled_folder_name_still_matches_the_right_book(self):
        m = run("Steeleart Reckoners Brandon Sanderson", prod("B0STEEL001", "Steelheart", "Brandon Sanderson"),
                "Brandon Sanderson")
        self.assertEqual(m.asin, "B0STEEL001")
        self.assertEqual(m.rejected, "")

    def test_partials_is_still_refused(self):
        m = run("Partials", prod("B0PARTIAL2", "The Partial Kingdom Part 2", "Dan Wells"), "Dan Wells")
        self.assertIsNone(m.product)
        self.assertIn("closest was 'The Partial Kingdom Part 2'", m.rejected)

    def test_other_patterson_folder_names_are_refused(self):
        for wanted, wrong in (("Private 01 - Private", "Step on a Crack"),
                              ("Private 02 - Private London", "The Country Road Murders")):
            self.assertIsNone(run(wanted, prod("B0WRONG002", wrong)).product, wanted)

    def test_single_token_is_the_whole_string(self):
        for wanted, cand in (("Private", "Step on a Crack"), ("Steelheart", "Steelheart")):
            self.assertEqual(matching.window_sim(wanted, cand), matching.sim(wanted, cand))

    def test_note_quotes_the_window_score(self):
        want = matching.window_sim("Private 01 - Private", "Step on a Crack")
        m = run("Private 01 - Private", prod("B0WRONG003", "Step on a Crack"))
        self.assertIn(f"(title {want:.2f}, needs 0.60)", m.rejected)

    def test_the_floor_is_the_matchers_own_scorer(self):
        self.assertEqual(matching.TITLE_FLOOR, 0.60)


class TestHeld(unittest.TestCase):
    def test_process_unit_holds_with_the_rejected_title_in_the_note(self):
        import pathlib
        import tempfile

        from audiobind.state import State

        unit = BookUnit(path="X", name="X", parent="", parts=[], title_guess="Private",
                        author_guess="James Patterson", rel_path="James Patterson/Private")
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                with mock.patch.object(catalog, "search", lambda **kw: [prod("B0WRONG001", "Step on a Crack")]), \
                        mock.patch.object(pipeline, "probe", side_effect=AssertionError("no files")):
                    pipeline.process_unit(state, unit, log=lambda *_: None)
                row = state.get("James Patterson/Private")
            finally:
                state.close()
        self.assertEqual(row.status, "held")
        self.assertEqual(row.confidence, "NONE")
        self.assertEqual(row.asin, "")
        self.assertNotEqual(row.title, "Step on a Crack")
        self.assertTrue(row.note.startswith("no catalog match: closest was 'Step on a Crack' (title "))

    def test_advice_names_the_rejected_candidate(self):
        why, what = advice.explain("held", "NONE", "no catalog match: closest was 'Judge Stone' (title 0.29, needs 0.60)")
        self.assertIn("Judge Stone", why)
        self.assertIn("ASIN", what)


class TestScoreUsesTheFloorMeasure(unittest.TestCase):
    """0.19.0-dev12: title_sim is floor_sim, so the score and TITLE_FLOOR agree on one measure."""

    DCC_FOLDER_TITLE = "Dungeon Crawler Carl 01 - Dungeon Crawler Carl"

    def dcc(self):
        return Product(asin="B08V8B2CGV", title="Dungeon Crawler Carl", authors=["Matt Dinniman"],
                       narrators=["Jeff Hays"], series="Dungeon Crawler Carl", sequence="1",
                       hours=13.7, language="english")

    def test_folder_baggage_no_longer_drags_a_right_title_to_review(self):
        # the live row: title_sim 0.85, score 0.91 -> MEDIUM -> Review under the old whole-string sim
        m = run(self.DCC_FOLDER_TITLE, self.dcc(), "Matt Dinniman")
        self.assertEqual(m.asin, "B08V8B2CGV")
        self.assertEqual(m.title_sim, 1.0)
        self.assertEqual(m.score, 1.0)
        self.assertEqual(m.confidence, "HIGH")

    def test_it_still_beats_its_own_sequel(self):
        sequel = Product(asin="B08WJ59784", title="Carl's Doomsday Scenario", authors=["Matt Dinniman"],
                         hours=14.0, language="english")
        unit = BookUnit(path="X", name="X", parent="", parts=[], title_guess=self.DCC_FOLDER_TITLE,
                        author_guess="Matt Dinniman")
        with mock.patch.object(catalog, "search", lambda **kw: [self.dcc(), sequel]), \
                mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(unit)
        self.assertEqual(m.asin, "B08V8B2CGV")
        self.assertEqual(m.confidence, "HIGH")
        self.assertGreaterEqual(m.margin, 0.05)

    def test_title_sim_is_floor_sim(self):
        product = self.dcc()
        m = run(self.DCC_FOLDER_TITLE, product, "Matt Dinniman")
        self.assertEqual(m.title_sim, round(matching.floor_sim(self.DCC_FOLDER_TITLE, product), 3))

    def test_the_dev3_wrong_matches_are_still_rejected_by_the_floor(self):
        for wanted, wrong in (("Private", "Step on a Crack"),
                              ("Private London", "The Country Road Murders"),
                              ("Private: #1 Suspect", "Judge Stone")):
            m = run(wanted, prod("B0WRONG001", wrong))
            self.assertIsNone(m.product, wanted)
            self.assertEqual(m.confidence, "NONE", wanted)
            self.assertIn("needs 0.60)", m.rejected)

    def test_a_plain_exact_match_is_unchanged(self):
        m = run("Project Hail Mary", prod("B08G9PRS1K", "Project Hail Mary", "Andy Weir"), "Andy Weir")
        self.assertEqual((m.title_sim, m.score, m.confidence), (1.0, 1.0, "HIGH"))

    def test_the_must_still_match_cases_still_match(self):
        for wanted, title, author in (("Project Hail Mary [TRACKER]", "Project Hail Mary", "Andy Weir"),
                                      ("Count to Ten", "Count to Ten aka Private Delhi", "James Patterson"),
                                      ("Steeleart Reckoners Brandon Sanderson", "Steelheart", "Brandon Sanderson")):
            m = run(wanted, prod("B0MATCH001", title, author), author)
            self.assertEqual(m.asin, "B0MATCH001", wanted)
            self.assertEqual(m.rejected, "", wanted)
            self.assertGreaterEqual(m.title_sim, matching.TITLE_FLOOR, wanted)
        # a near title is not inflated: no window of "Count to Ten" beats the whole string
        self.assertEqual(run("Count to Ten", prod("B0COUNT001", "Count to Ten aka Private Delhi")).title_sim, 0.85)


if __name__ == "__main__":
    unittest.main()

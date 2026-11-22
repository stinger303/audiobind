"""MATCH-03-06: the four wrong-book paths left after MATCHING-01/02.

Each case comes from AUDIT-matching-2026-11-20 (MATCHING-03..06) and fails on cce5be7. Unless a
case says otherwise the folder is "Avery Writer - <title>", every catalog search returns the
case's products, both the files and the catalog say 10 hours, and Audnex knows nothing.
"""

import unittest

from audiobind import matching
from audiobind.matching import Match, grade
from tests.test_match_m1b import AUTHOR, KINGDOM, pipeline_run, product, run


class TestMatching03AdaptationBothWays(unittest.TestCase):
    """A marker the folder asked for and the pick lacks costs what a marker the pick carries unasked
    costs: never HIGH, MEDIUM at most - with one candidate, where ranking cannot help."""

    def test_full_cast_folder_with_a_sole_plain_candidate_is_not_high(self):
        m = run(f"{AUTHOR} - {KINGDOM} (Full Cast)", [product("B0PLAIN001", KINGDOM)])
        self.assertEqual(m.asin, "B0PLAIN001")
        self.assertEqual(m.confidence, "MEDIUM", vars(m))
        self.assertEqual(m.missing_adaptation, "a full-cast production")
        self.assertEqual(m.unasked_adaptation, "")

    def test_the_reverse_direction_is_unchanged_and_stays_its_own_field(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [product("B0FCAST001", f"{KINGDOM} (Full Cast)")])
        self.assertEqual((m.confidence, m.unasked_adaptation, m.missing_adaptation),
                         ("MEDIUM", "a full-cast production", ""))

    def test_both_directions_cost_the_same(self):
        asked = run(f"{AUTHOR} - {KINGDOM} (Full Cast)", [product("B0PLAIN001", KINGDOM)])
        unasked = run(f"{AUTHOR} - {KINGDOM}", [product("B0FCAST001", f"{KINGDOM} (Full Cast)")])
        self.assertEqual((asked.title_sim, asked.margin), (unasked.title_sim, unasked.margin))
        self.assertEqual(asked.confidence, unasked.confidence)

    def test_any_marker_on_either_side_agrees(self):
        # GraphicAudio makes the "[Dramatized Adaptation]" titles: any marker answers any marker
        m = run(f"{AUTHOR} - {KINGDOM} (GraphicAudio)",
                [product("B0DRAMA001", f"{KINGDOM} [Dramatized Adaptation]")])
        self.assertEqual((m.confidence, m.missing_adaptation, m.unasked_adaptation), ("HIGH", "", ""))

    def test_a_plain_folder_and_a_plain_pick_keep_high(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [product("B0PLAIN001", KINGDOM)])
        self.assertEqual((m.confidence, m.missing_adaptation), ("HIGH", ""))

    def test_the_wanted_production_still_wins_when_both_are_offered(self):
        m = run(f"{AUTHOR} - {KINGDOM} (Full Cast)",
                [product("B0PLAIN001", KINGDOM), product("B0FCAST001", f"{KINGDOM} (Full Cast)")])
        self.assertEqual((m.asin, m.missing_adaptation), ("B0FCAST001", ""))

    def test_grade_caps_missing_adaptation_like_unasked(self):
        strong = dict(product=product("B0", KINGDOM), title_sim=1.0, author_sim=1.0, margin=1.0,
                      author_known=True)
        self.assertEqual(grade(Match(**strong)), "HIGH")
        self.assertEqual(grade(Match(**strong, missing_adaptation="a full-cast production")), "MEDIUM")
        self.assertEqual(grade(Match(**strong, unasked_adaptation="a full-cast production")), "MEDIUM")

    def test_helper(self):
        plain, cast = product("B0", KINGDOM), product("B1", f"{KINGDOM} (Full Cast)")
        self.assertEqual(matching.missing_adaptation(plain, (f"{KINGDOM} (Full Cast)",)),
                         "a full-cast production")
        self.assertEqual(matching.missing_adaptation(cast, (f"{KINGDOM} (Full Cast)",)), "")
        self.assertEqual(matching.missing_adaptation(plain, (KINGDOM,)), "")

    def test_note_and_detail_name_it_and_nothing_converts(self):
        key = f"{AUTHOR}/{AUTHOR} - {KINGDOM} (Full Cast)"
        note, detail, status, converted = pipeline_run(key, [product("B0PLAIN001", KINGDOM)])
        self.assertIn("your folder says a full-cast production and the catalog hit is not one", note)
        self.assertEqual(detail["match"]["missing_adaptation"], "a full-cast production")
        self.assertEqual(detail["match"]["confidence"], "MEDIUM")
        self.assertEqual(converted, [])
        self.assertEqual(status, "review")


if __name__ == "__main__":
    unittest.main()

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


ROW = {"title": KINGDOM, "authors": [AUTHOR], "series": "Realm", "sequence": "2"}


def torrent(tid, name, author=AUTHOR):
    from audiobind.tracker import Torrent
    return Torrent(tid=str(tid), name=name, authors=[author], numfiles=1, filetype="m4b", raw={})


class TestMatching06TrackerGate(unittest.TestCase):
    """The tracker pick is the acquisition gate: a summary, another volume or a box set of the
    wanted title is rejected outright - with a reason - whatever its bracket-stripped title scores."""

    def pick(self, row, *names):
        from audiobind import tracker
        return tracker.select_candidate(row, [torrent(i + 1, n) for i, n in enumerate(names)])

    def test_the_audits_three_variants_are_each_rejected(self):
        for name, reason in ((f"{KINGDOM} [Summary]", "summary"), (f"{KINGDOM} (Book 1)", "Book 1"),
                             (f"{KINGDOM} (Box Set)", "box set")):
            pick, why = self.pick(ROW, name)
            self.assertIsNone(pick, name)
            self.assertIn("rejected", why)
            self.assertIn(reason, why)

    def test_all_three_together_are_rejected_and_counted(self):
        pick, why = self.pick(ROW, f"{KINGDOM} [Summary]", f"{KINGDOM} (Book 1)", f"{KINGDOM} (Box Set)")
        self.assertIsNone(pick)
        self.assertIn("(+2 more)", why)

    def test_the_right_book_beside_them_is_picked(self):
        pick, _why = self.pick(ROW, f"{KINGDOM} [Summary]", f"{KINGDOM} (Book 1)", f"{KINGDOM} (Book 2)",
                               f"{KINGDOM} (Box Set)")
        self.assertEqual(pick.name, f"{KINGDOM} (Book 2)")

    def test_markers_that_are_not_another_product_still_pass(self):
        for name in (KINGDOM, f"{KINGDOM} (Unabridged)", f"{KINGDOM} [Book 2]", f"{KINGDOM} (Narrated by X)"):
            self.assertIsNotNone(self.pick(ROW, name)[0], name)

    def test_other_derivative_and_adaptation_markers(self):
        for name in (f"{KINGDOM} (Abridged)", f"{KINGDOM} [Synopsis]", f"{KINGDOM} (Study Guide)",
                     f"{KINGDOM} (Full Cast)", f"{KINGDOM} [GraphicAudio]", f"{KINGDOM} (Books 1-3)"):
            pick, why = self.pick(ROW, name)
            self.assertIsNone(pick, name)
            self.assertIn("rejected", why)

    def test_a_row_that_names_the_marker_takes_it(self):
        row = dict(ROW, title=f"{KINGDOM} (Full Cast)")
        self.assertIsNotNone(self.pick(row, f"{KINGDOM} (Full Cast)")[0])
        pick, why = self.pick(row, KINGDOM)
        self.assertIsNone(pick)
        self.assertIn("the wishlist title is a full-cast production", why)

    def test_a_number_that_is_part_of_the_title_is_not_a_position(self):
        row = {"title": "Fahrenheit 451", "authors": [AUTHOR], "sequence": "1"}
        self.assertIsNotNone(self.pick(row, "Fahrenheit 451")[0])
        self.assertIsNotNone(self.pick({"title": KINGDOM, "authors": [AUTHOR]}, f"{KINGDOM} (Book 1)")[0],
                             "a row with no position vetoes nothing")

    def test_the_watcher_downloads_nothing_and_the_note_says_why(self):
        from tests.test_watcher import FakeQbit, WatcherCase, answer, entry

        class Case(WatcherCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        try:
            case.want(asin="B0KINGDOM2", title=KINGDOM, authors=(AUTHOR,), series="Realm", sequence="2")
            # only the search is answered: a download request would raise "unexpected tracker request"
            case.fake_tracker(answer(entry(1, f"{KINGDOM} [Summary]", author=AUTHOR),
                                     entry(2, f"{KINGDOM} (Book 1)", author=AUTHOR),
                                     entry(3, f"{KINGDOM} (Box Set)", author=AUTHOR)))
            case.cycle()
            row = case.row("B0KINGDOM2")
            self.assertEqual(len(case.transport.calls), 1, "searched only - no download request")
            self.assertFalse(any(q.added for q in FakeQbit.instances))
            self.assertEqual(row["status"], "wanted")
            self.assertIn("rejected", row["note"])
        finally:
            case.tearDown()


def record(asin, title, minutes=600, language="english", authors=(AUTHOR,)):
    """An Audnex record for ``asin`` (language=None leaves the field out)."""
    rec = {"asin": asin, "title": title, "authors": [{"name": a} for a in authors], "runtimeLengthMin": minutes}
    if language is not None:
        rec["language"] = language
    return rec


#: the exact book and a near-titled other work: their scores sit 0.008 apart, inside the corroboration
#: window, and nothing in either title names a series position (MATCHING-01's veto cannot help)
EXACT = product("B0EXACT001", KINGDOM)
NEAR = product("B0NEARX001", f"{KINGDOM}s")


class TestMatching04UnknownIsNotFalse(unittest.TestCase):
    """Audnex has three answers - agrees, disagrees, could not say - and only evidence moves a pick
    or a margin. "I could not check" never reads as "this does not exist"."""

    def test_a_record_for_a_near_rival_never_displaces_an_exact_leader_with_no_record(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [NEAR, EXACT], audnex={NEAR.asin: record(NEAR.asin, NEAR.title)})
        self.assertEqual(m.asin, EXACT.asin, vars(m))
        self.assertNotEqual(m.confidence, "HIGH")
        self.assertEqual(m.margin, 0.008)
        self.assertIsNone(m.corroborated)

    def test_an_unknown_rival_never_widens_a_corroborated_leaders_margin(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [NEAR, EXACT], audnex={EXACT.asin: record(EXACT.asin, EXACT.title)})
        self.assertEqual((m.asin, m.corroborated, m.margin), (EXACT.asin, True, 0.008))
        self.assertEqual(m.confidence, "MEDIUM")
        near = next(a for a in m.alternates if a["asin"] == NEAR.asin)
        self.assertIsNone(near["corroborated"])

    def test_the_audit_case_book_two_keeps_its_pick(self):
        b1, b2 = product("B0SYNK0001", f"{KINGDOM} Book 1"), product("B0SYNK0002", f"{KINGDOM} Book 2")
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [b1, b2], audnex={b1.asin: record(b1.asin, b1.title, minutes=0)})
        self.assertEqual((m.asin, m.confidence, m.margin), (b2.asin, "MEDIUM", 0.014))

    def test_three_states(self):
        from unittest import mock

        from audiobind import catalog

        def answer(rec=None, exc=None):
            def book(_asin):
                if exc:
                    raise exc
                return rec
            return book

        cases = [
            (answer(exc=OSError("timeout")), None),                      # lookup failed
            (answer({}), None),                                          # no record
            (answer({"title": KINGDOM}), None),                          # a record with no ASIN
            (answer(record(EXACT.asin, "Another Book Entirely")), False),  # disagrees on the title
            (answer(record(EXACT.asin, KINGDOM, authors=("Someone Else",))), False),
            (answer(record(EXACT.asin, KINGDOM, minutes=900)), False),   # runtime 50% off
            (answer(record(EXACT.asin, KINGDOM, minutes=0)), True),      # no runtime: not compared
            (answer(record(EXACT.asin, KINGDOM)), True),
        ]
        for book, want in cases:
            with mock.patch.object(catalog, "book", book):
                self.assertIs(matching.corroborated(EXACT), want)

    def test_evidence_still_removes_a_rival(self):
        # the rival HAS a record and it disagrees: that is evidence, so it steps aside
        m = run(f"{AUTHOR} - {KINGDOM}", [NEAR, EXACT],
                audnex={EXACT.asin: record(EXACT.asin, EXACT.title), NEAR.asin: record(NEAR.asin, "Unrelated")})
        self.assertEqual((m.asin, m.corroborated, m.margin, m.confidence), (EXACT.asin, True, 1.0, "HIGH"))

    def test_a_leader_audnex_contradicts_gives_way_to_a_corroborated_contender(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [NEAR, EXACT],
                audnex={EXACT.asin: record(EXACT.asin, "Unrelated"), NEAR.asin: record(NEAR.asin, NEAR.title)})
        self.assertEqual((m.asin, m.corroborated), (NEAR.asin, True))


def hinted(asin):
    return lambda unit: setattr(unit, "asin_hint", asin)


class TestMatching05HintIsEvidence(unittest.TestCase):
    """An ASIN in the name puts its book in the pool and is then scored and graded like any other
    candidate: never 1.0, never ahead of better title/author evidence, language never assumed."""

    def test_a_hint_never_scores_one(self):
        # only the hint names the near book (the search finds the exact one)
        m = run(f"{AUTHOR} - {KINGDOM}", [EXACT], audnex={NEAR.asin: record(NEAR.asin, NEAR.title)},
                unit_hook=hinted(NEAR.asin))
        near = next(a for a in m.alternates if a["asin"] == NEAR.asin)
        self.assertLess(near["score"], 1.0)
        self.assertEqual(near["score"], round(0.6 * near["title_sim"] + 0.4 * near["author_sim"], 3))

    def test_a_hint_naming_another_work_loses_to_the_exact_title(self):
        # the audit's shape without a series position: the hint's own record is the only Audnex
        # answer, so before the fix it supplied the pick AND its corroboration (HIGH, margin 1.0)
        m = run(f"{AUTHOR} - {KINGDOM}", [NEAR, EXACT], audnex={NEAR.asin: record(NEAR.asin, NEAR.title)},
                unit_hook=hinted(NEAR.asin))
        self.assertEqual(m.asin, EXACT.asin, vars(m))
        self.assertEqual(m.confidence, "LOW")
        self.assertIn(NEAR.asin, m.hint_conflict)

    def test_the_audit_case_book_one_hint_against_an_exact_book_two(self):
        b1, b2 = product("B0SYNK0001", f"{KINGDOM} Book 1"), product("B0SYNK0002", f"{KINGDOM} Book 2")
        m = run(f"{AUTHOR} - {KINGDOM} Book 2", [b1, b2], audnex={b1.asin: record(b1.asin, b1.title)},
                unit_hook=hinted(b1.asin))
        self.assertEqual(m.asin, b2.asin)
        self.assertEqual(m.confidence, "LOW")
        self.assertIn(b1.asin, m.hint_conflict)
        self.assertEqual(next(a for a in m.alternates if a["asin"] == b1.asin)["score"] < 1.0, True)

    def test_two_signals_naming_different_books_never_grade_high(self):
        # MATCH-CASE-2b INV2: the same inputs arise when the folder is mistitled and the hint is
        # right, so a person decides - even with both records present and the runtimes agreeing
        other = product("B0OTHER001", f"{KINGDOM} Rising")
        m = run(f"{AUTHOR} - {KINGDOM}", [other, EXACT],
                audnex={EXACT.asin: record(EXACT.asin, EXACT.title), other.asin: record(other.asin, other.title)},
                unit_hook=hinted(other.asin))
        self.assertEqual(m.asin, EXACT.asin)
        self.assertEqual(m.confidence, "LOW")
        self.assertTrue(m.hint_conflict)

    def test_a_hint_with_no_language_is_not_assumed_english(self):
        r = record(EXACT.asin, KINGDOM, language=None)
        hint_only = run(f"{AUTHOR} - {KINGDOM}", [], audnex={EXACT.asin: r}, unit_hook=hinted(EXACT.asin))
        plain = run(f"{AUTHOR} - {KINGDOM}", [product(EXACT.asin, KINGDOM, language="")])
        self.assertEqual((hint_only.asin, hint_only.language, hint_only.language_penalized), (EXACT.asin, "", True))
        self.assertEqual(hint_only.confidence, "MEDIUM")
        self.assertEqual((hint_only.score, hint_only.confidence), (plain.score, plain.confidence))

    def test_the_catalog_entry_replaces_the_hint_entry_for_the_same_asin(self):
        r = record(EXACT.asin, KINGDOM, language=None)
        m = run(f"{AUTHOR} - {KINGDOM}", [EXACT], audnex={EXACT.asin: r}, unit_hook=hinted(EXACT.asin))
        self.assertEqual((m.asin, m.language, m.confidence, m.hint_conflict), (EXACT.asin, "english", "HIGH", ""))

    def test_a_hint_that_agrees_keeps_high(self):
        m = run(f"{AUTHOR} - {KINGDOM}", [], audnex={EXACT.asin: record(EXACT.asin, KINGDOM)},
                unit_hook=hinted(EXACT.asin))
        self.assertEqual((m.asin, m.score, m.confidence, m.hint_conflict), (EXACT.asin, 1.0, "HIGH", ""))

    def test_a_pin_keeps_the_english_default(self):
        self.assertEqual(matching._product_from_book(record(EXACT.asin, KINGDOM, language=None)).language,
                         "english")


if __name__ == "__main__":
    unittest.main()

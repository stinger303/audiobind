"""Knockoff editions (summaries, reviews, zero-runtime stubs) never eat the match margin,
and a second source (Audnex) settles a contest between near-identical candidates."""

import json
import pathlib
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from audiobind import pipeline, web
from audiobind.catalog import Product
from audiobind.matching import Match, corroborated, grade, is_knockoff, match_unit
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part
from tests.test_match_evidence import alt, evidence_detail, text


def product(asin, title, author, hours, subtitle=""):
    return Product(
        asin=asin, title=title, subtitle=subtitle, authors=[author], hours=hours, language="english"
    )


def unit(title, author):
    return BookUnit(
        path="A/B",
        name="B",
        parent="A",
        parts=[Part(name="B", files=["/src/a.mp3"])],
        author_guess=author,
        title_guess=title,
    )


def audnex(asin, title, author, minutes):
    return {"asin": asin, "title": title, "authors": [{"name": author}], "runtimeLengthMin": minutes}


def run(the_unit, products, records=None):
    """match_unit against a fake catalog; ``records`` are the Audnex books by ASIN."""
    records = records or {}
    with patch("audiobind.matching.catalog") as cat:
        cat.search.return_value = products
        cat.book.side_effect = lambda asin: records.get(asin, {})
        match = match_unit(the_unit, local_hours=23.57)
        return match, cat.book


class TestIsKnockoff(unittest.TestCase):
    def test_title_tokens(self):
        for title in (
            "The Butcher's Masquerade Summarized",
            "Summary of Atomic Habits",
            "Atomic Habits: A Study Guide",
            "Atomic Habits  Study   Guide",
            "The Atomic Habits Workbook",
            "[Review] The Butcher's Masquerade (Matt Dinniman) Summarized.",
            "[Review] The Butcher's Masquerade",
            "[ review ] Something",
            "Review: Atomic Habits",
            "review : Atomic Habits",
        ):
            with self.subTest(title=title):
                self.assertTrue(is_knockoff(product("B0", title, "X", 5.0)))

    def test_case_insensitive(self):
        titles = ("SUMMARY OF X", "x SummaRIZED", "x STUDY GUIDE", "x WorkBook", "[REVIEW] x", "REVIEW: x")
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(is_knockoff(product("B0", title, "X", 5.0)))

    def test_marker_in_the_subtitle(self):
        # a knockoff that copies the real title verbatim carries its marker in the subtitle
        knock = product(
            "B0", "The Butcher's Masquerade", "X", 0.5,
            subtitle="[Review] The Butcher's Masquerade (Matt Dinniman) Summarized.",
        )
        self.assertTrue(is_knockoff(knock))

    def test_real_titles_are_not_knockoffs(self):
        for title in (
            "The Butcher's Masquerade",
            "Peer Review: A Novel",  # "review:" only counts as a prefix
            "The Reviewer",
            "Summaryville",  # whole words only
            "Atomic Habits",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_knockoff(product("B0", title, "X", 5.0)))

    def test_zero_runtime_beside_a_real_runtime(self):
        stub = product("B0STUB", "The Butcher's Masquerade", "Matt Dinniman", 0.0)
        real = product("B0REAL", "The Butcher's Masquerade", "Matt Dinniman", 23.55)
        self.assertTrue(is_knockoff(stub, [stub, real]))
        self.assertFalse(is_knockoff(real, [stub, real]))

    def test_zero_runtime_alone_is_not_a_knockoff(self):
        stub = product("B0STUB", "Preorder", "X", 0.0)
        other = product("B0OTHER", "Other", "X", 0.0)
        self.assertFalse(is_knockoff(stub))
        self.assertFalse(is_knockoff(stub, [stub]))
        self.assertFalse(is_knockoff(stub, [stub, other]))  # nobody carries a real runtime

    def test_raw_catalog_dicts(self):
        self.assertTrue(is_knockoff({"title": "Summary of X", "runtime_length_min": 30}))
        stub = {"title": "X", "runtime_length_min": 0}
        real = {"title": "X", "runtime_length_min": 600}
        self.assertTrue(is_knockoff(stub, [stub, real]))
        self.assertTrue(is_knockoff({"title": "X"}, [real]))  # missing runtime counts as 0
        self.assertFalse(is_knockoff(real, [stub, real]))


BUTCHER = "The Butcher's Masquerade"
DINNIMAN = "Matt Dinniman"


def butcher_real():
    return product("B09MZSJPTN", BUTCHER, DINNIMAN, 23.55)


class TestLiveButcherCase(unittest.TestCase):
    """Observed 2026-11-12: the exact edition parked MEDIUM behind a summarized knockoff."""

    def test_knockoff_title_runner_up_grades_high(self):
        title = "[Review] The Butcher's Masquerade (Matt Dinniman) Summarized."
        knock = product("B0KNOCK", title, DINNIMAN, 0.6)
        match, book = run(unit(BUTCHER, DINNIMAN), [butcher_real(), knock])
        self.assertEqual(match.asin, "B09MZSJPTN")
        self.assertEqual(match.score, 1.0)
        self.assertEqual(match.margin, 1.0)  # no real runner-up: decisive
        self.assertEqual(match.confidence, "HIGH")
        self.assertFalse(match.knockoff)
        # still listed (badged) so the other-editions view stays honest
        self.assertEqual([a["asin"] for a in match.alternates], ["B0KNOCK"])
        self.assertTrue(match.alternates[0]["knockoff"])
        book.assert_not_called()  # nothing competed for the margin: no second-source lookup

    def test_knockoff_that_copies_the_title_no_longer_eats_the_margin(self):
        # the shape that lands the old margin between 0 and 0.05: real title, marker in the
        # subtitle, near-identical author
        knock = product(
            "B0KNOCK", BUTCHER, "Matt Dinnman", 0.6,
            subtitle="[Review] The Butcher's Masquerade (Matt Dinniman) Summarized.",
        )
        match, _book = run(unit(BUTCHER, DINNIMAN), [butcher_real(), knock])
        knock_score = match.alternates[0]["score"]
        self.assertGreater(knock_score, 0.95)  # before the fix this left a margin under 0.05
        self.assertEqual(match.asin, "B09MZSJPTN")
        self.assertEqual(match.margin, 1.0)
        self.assertEqual(match.confidence, "HIGH")
        action = pipeline.choose_action(unit(BUTCHER, DINNIMAN), match.confidence, True, {"mp3"})
        self.assertEqual(action, "convert")

    def test_margin_is_against_the_next_real_candidate(self):
        knock = product("B0KNOCK", BUTCHER, "Matt Dinnman", 0.6, subtitle="Summarized")
        rival = product("B0OTHER", "The Butcher's Daughter", DINNIMAN, 11.0)
        match, _book = run(unit(BUTCHER, DINNIMAN), [butcher_real(), knock, rival])
        rival_score = next(a["score"] for a in match.alternates if a["asin"] == "B0OTHER")
        self.assertEqual(match.margin, round(1.0 - rival_score, 3))
        self.assertTrue(next(a for a in match.alternates if a["asin"] == "B0KNOCK")["knockoff"])
        self.assertFalse(next(a for a in match.alternates if a["asin"] == "B0OTHER")["knockoff"])


class TestBestIsKnockoff(unittest.TestCase):
    def test_best_knockoff_is_not_high(self):
        knock = product("B0SUM", "Summary of Atomic Habits", "Readtrepreneur Publishing", 0.9)
        real = product("B0REAL", "Atomic Habits", "James Clear", 5.3)
        match, _book = run(unit("Summary of Atomic Habits", "Readtrepreneur Publishing"), [knock, real])
        self.assertEqual(match.asin, "B0SUM")  # still matched: a person can confirm it
        self.assertTrue(match.knockoff)
        self.assertEqual(match.confidence, "LOW")
        # LOW goes to review under every auto-convert tier, "medium" included
        self.assertEqual(pipeline.choose_action(unit("x", "y"), match.confidence, True, {"mp3"}), "review")

    def test_only_candidate_is_a_knockoff(self):
        knock = product("B0SUM", "Summary of Atomic Habits", "Readtrepreneur Publishing", 0.9)
        match, book = run(unit("Summary of Atomic Habits", "Readtrepreneur Publishing"), [knock])
        self.assertEqual(match.asin, "B0SUM")
        self.assertTrue(match.knockoff)
        self.assertEqual(match.confidence, "LOW")
        self.assertEqual(match.alternates, [])
        book.assert_not_called()

    def test_zero_runtime_best_beside_real_editions(self):
        stub = product("B0STUB", BUTCHER, DINNIMAN, 0.0)
        other = product("B0OTHER", "Carl's Doomsday Scenario", DINNIMAN, 16.0)
        match, _book = run(unit(BUTCHER, DINNIMAN), [stub, other])
        self.assertEqual(match.asin, "B0STUB")
        self.assertTrue(match.knockoff)
        self.assertEqual(match.confidence, "LOW")

    def test_grade_caps_a_knockoff_at_low(self):
        strong = dict(
            product=butcher_real(), score=1.0, margin=1.0, title_sim=1.0, author_sim=1.0, author_known=True
        )
        self.assertEqual(grade(Match(**strong)), "HIGH")
        self.assertEqual(grade(Match(**strong, knockoff=True)), "LOW")

    def test_pipeline_records_the_flags(self):
        knock = product("B0SUM", "Summary of Atomic Habits", "Readtrepreneur Publishing", 0.9)
        match, _book = run(unit("Summary of Atomic Habits", "Readtrepreneur Publishing"), [knock])
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                book = unit("Summary of Atomic Habits", "Readtrepreneur Publishing")
                book.rel_path = "A/B"
                audit = pipeline.Audit(unit=book, hours=0.9, match=match, confidence=match.confidence)
                pipeline.record(state, audit, "review")
                data = json.loads(state.get("A/B").detail)["match"]
            finally:
                state.close()
        self.assertTrue(data["knockoff"])
        self.assertIsNone(data["corroborated"])


MONEYBALL = "Moneyball"
LEWIS = "Michael Lewis"


def moneyball_pair():
    """Two near-identical titles: different works by _same_work, scores within 0.05."""
    exact = product("B0EXACT", MONEYBALL, LEWIS, 10.0)
    near = product("B0NEAR", "Money Ball", LEWIS, 10.0)
    return exact, near


class TestCorroboration(unittest.TestCase):
    def test_without_audnex_the_pair_is_ambiguous(self):
        exact, near = moneyball_pair()
        match, _book = run(unit(MONEYBALL, LEWIS), [exact, near])
        self.assertEqual(match.asin, "B0EXACT")
        self.assertLess(match.margin, 0.05)
        self.assertEqual(match.confidence, "MEDIUM")
        self.assertFalse(match.corroborated)

    def test_corroborated_best_keeps_its_margin(self):
        exact, near = moneyball_pair()
        records = {"B0EXACT": audnex("B0EXACT", MONEYBALL, LEWIS, 600)}
        match, _book = run(unit(MONEYBALL, LEWIS), [exact, near], records)
        self.assertEqual(match.asin, "B0EXACT")
        self.assertTrue(match.corroborated)
        self.assertEqual(match.margin, 1.0)
        self.assertEqual(match.confidence, "HIGH")
        alt = match.alternates[0]
        self.assertEqual(alt["asin"], "B0NEAR")
        self.assertFalse(alt["corroborated"])

    def test_corroborated_runner_up_outranks_an_uncorroborated_best(self):
        exact, near = moneyball_pair()
        records = {"B0NEAR": audnex("B0NEAR", "Money Ball", LEWIS, 600)}
        match, _book = run(unit(MONEYBALL, LEWIS), [exact, near], records)
        self.assertEqual(match.asin, "B0NEAR")
        self.assertTrue(match.corroborated)
        self.assertEqual(match.confidence, "HIGH")
        self.assertEqual([a["asin"] for a in match.alternates], ["B0EXACT"])

    def test_both_corroborated_stays_ambiguous(self):
        exact, near = moneyball_pair()
        records = {
            "B0EXACT": audnex("B0EXACT", MONEYBALL, LEWIS, 600),
            "B0NEAR": audnex("B0NEAR", "Money Ball", LEWIS, 600),
        }
        match, _book = run(unit(MONEYBALL, LEWIS), [exact, near], records)
        self.assertEqual(match.asin, "B0EXACT")
        self.assertEqual(match.confidence, "MEDIUM")

    def test_disagreeing_record_does_not_corroborate(self):
        exact, near = moneyball_pair()
        records = {
            "B0EXACT": audnex("B0EXACT", MONEYBALL, LEWIS, 600),
            # Audnex has a record for the rival, but it is a different length entirely
            "B0NEAR": audnex("B0NEAR", "Money Ball", LEWIS, 60),
        }
        match, _book = run(unit(MONEYBALL, LEWIS), [exact, near], records)
        self.assertEqual(match.confidence, "HIGH")
        self.assertFalse(match.alternates[0]["corroborated"])

    def test_single_candidate_without_an_audnex_record_is_unchanged(self):
        exact, _near = moneyball_pair()
        match, book = run(unit(MONEYBALL, LEWIS), [exact])
        self.assertEqual(match.confidence, "HIGH")
        self.assertEqual(match.margin, 1.0)
        self.assertIsNone(match.corroborated)
        book.assert_not_called()

    def test_clear_winner_is_never_looked_up(self):
        exact = product("B0EXACT", MONEYBALL, LEWIS, 10.0)
        far = product("B0FAR", "The Big Short", LEWIS, 10.0)
        match, book = run(unit(MONEYBALL, LEWIS), [exact, far])
        self.assertEqual(match.confidence, "HIGH")
        book.assert_not_called()


class TestCorroboratedRule(unittest.TestCase):
    def _check(self, record, prod=None):
        prod = prod or product("B0X", MONEYBALL, LEWIS, 10.0)
        with patch("audiobind.matching.catalog") as cat:
            if isinstance(record, Exception):
                cat.book.side_effect = record
            else:
                cat.book.return_value = record
            return corroborated(prod)

    def test_agreeing_record(self):
        self.assertTrue(self._check(audnex("B0X", MONEYBALL, LEWIS, 600)))
        self.assertTrue(self._check(audnex("B0X", MONEYBALL, LEWIS, 620)))  # within 5%

    def test_missing_or_broken_record(self):
        self.assertFalse(self._check({}))
        self.assertFalse(self._check(None))
        self.assertFalse(self._check(MagicMock()))
        self.assertFalse(self._check(OSError("network")))

    def test_disagreements(self):
        self.assertFalse(self._check(audnex("B0X", "The Big Short", LEWIS, 600)))
        self.assertFalse(self._check(audnex("B0X", MONEYBALL, "Someone Else", 600)))
        self.assertFalse(self._check(audnex("B0X", MONEYBALL, LEWIS, 300)))

    def test_unknown_runtime_on_either_side_is_not_a_disagreement(self):
        self.assertTrue(self._check(audnex("B0X", MONEYBALL, LEWIS, 0)))
        no_runtime = product("B0X", MONEYBALL, LEWIS, 0)
        self.assertTrue(self._check(audnex("B0X", MONEYBALL, LEWIS, 600), no_runtime))


class TestPanelBadges(unittest.TestCase):
    """The detail panel badges knockoffs so the other-editions view stays honest."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def panel(self, detail, alternates, start_at='<div class="ab-ev">', end_at='<div class="ab-edit"'):
        self.state.save(
            UnitState(
                path="Duhigg/Habit",
                status="review",
                confidence="HIGH",
                asin="B0FD4CV4BF",
                title="The Power of Habit",
                detail=detail,
                alternates=json.dumps(alternates),
                hours=10.5,
            )
        )
        page = web.render(self.state)
        start = page.index(start_at)
        return page[start : page.index(end_at, start)]

    def rival_rows(self, panel):
        return re.findall(r'<div class="ab-evrival">.*?</button></div>', panel)

    def test_knockoff_rival_is_badged_with_its_reason(self):
        knock = alt(asin="B0KO", title="Summary of The Power of Habit", score=0.91, knockoff=True)
        real = alt(asin="B0SFB", title="Smarter Faster Better", score=0.65, title_sim=0.41)
        rows = self.rival_rows(self.panel(evidence_detail(), [knock, real]))
        self.assertEqual(len(rows), 2)
        self.assertIn('class="ab-ko"', rows[0])
        self.assertIn("Knockoff", text(rows[0]))
        self.assertIn("never counts against the pick", text(rows[0]))
        self.assertIn('data-asin="B0KO"', rows[0])  # still one click to use
        self.assertNotIn('class="ab-ko"', rows[1])

    def test_rows_without_the_flag_are_not_badged(self):
        panel = self.panel(evidence_detail(), [alt(asin="B0SFB", title="Smarter Faster Better", score=0.65)])
        self.assertNotIn('class="ab-ko"', panel)
        self.assertNotIn("Audnex agrees", panel)

    def test_knockoff_winner_is_badged(self):
        panel = self.panel(evidence_detail(knockoff=True), [])
        win = panel[panel.index('<div class="ab-evwin">') : panel.index('<div class="ab-evbars">')]
        self.assertIn('class="ab-ko"', win)

    def test_corroboration_is_shown(self):
        loser = alt(asin="B0NEAR", title="Money Ball", score=0.97, title_sim=0.95, corroborated=False)
        panel = self.panel(evidence_detail(corroborated=True), [loser])
        self.assertIn("Audnex agrees", panel)
        self.assertIn("Audnex doesn&#8217;t back this entry", self.rival_rows(panel)[0])

    def test_legacy_editions_list_badges_too(self):
        legacy = json.dumps({"match": {"asin": "B0FD4CV4BF", "title": "The Power of Habit"}})
        alts = [{"asin": "B0KO", "title": "Summary of Habit", "authors": ["X"], "knockoff": True}]
        panel = self.panel(legacy, alts, start_at='<div class="ab-editions">', end_at="</p></div>")
        self.assertIn("Other editions seen", panel)
        self.assertIn('class="ab-ko"', panel)


if __name__ == "__main__":
    unittest.main()

"""Table-driven matching fixtures: folder name + a frozen catalog -> the tier (and book) we expect.

Each unit is named by the scanner's own code (``units._name_units``), so a case exercises the
same name parsing a real library folder gets. Every catalog search in a case returns that case's
products (tests/fixtures/match_cases.json); Audnex knows nothing, so corroboration never decides.

A case states either an exact ``tier`` or a ``max_tier`` (the grade must not be above it), and
either the ``asin`` it must pick or an ``asin_not`` it must not pick at MEDIUM or above.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from audiobind import catalog, matching, units
from audiobind.catalog import Product
from audiobind.matching import Match, grade
from audiobind.units import BookUnit, Part

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "match_cases.json").read_text())["products"]
RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

#: (name, library folder, catalog product keys, expectation)
CASES = [
    # the folder must not beat the real book: "Dune" is one window of "Dune Messiah"
    ("dune", "Frank Herbert/Frank Herbert - Dune",
     ["dune", "dune_messiah", "children_of_dune"], {"tier": "HIGH", "asin": "B002V1OF70"}),
    ("dune_messiah", "Frank Herbert/Frank Herbert - Dune Messiah",
     ["dune", "dune_messiah", "children_of_dune"], {"tier": "HIGH", "asin": "B002V8H660"}),
    ("dune_messiah_not_in_catalog", "Frank Herbert/Frank Herbert - Dune Messiah",
     ["dune", "children_of_dune"], {"max_tier": "LOW"}),
    # a knockoff that copies the exact title never reaches HIGH (nor MEDIUM: LOW is its cap)
    ("summarized_knockoff", "James Clear - Atomic Habits",
     ["atomic_summarized"], {"max_tier": "LOW"}),
    ("study_guide_with_real_author", "James Clear - Atomic Habits",
     ["atomic_study_guide"], {"max_tier": "LOW"}),
    ("real_book_beats_knockoffs", "James Clear - Atomic Habits",
     ["atomic_summarized", "atomic_study_guide", "atomic_habits"], {"tier": "HIGH", "asin": "B07RFSSYBH"}),
    # a German edition of an English book: capped at MEDIUM, never HIGH
    ("german_edition_only", "Michael Lewis - Moneyball",
     ["moneyball_de"], {"tier": "MEDIUM", "asin": "B0DEMONEYB"}),
    ("english_beats_german", "Michael Lewis - Moneyball",
     ["moneyball_de", "moneyball_en"], {"tier": "HIGH", "asin": "B005EHN61O"}),
    # a box set named for its series must not be graded as book 1
    ("series_named_box_set", "James Dashner - The Maze Runner Series",
     ["maze_runner", "scorch_trials", "death_cure"], {"max_tier": "LOW"}),
    ("series_named_box_set_no_author", "The Maze Runner Series",
     ["maze_runner", "scorch_trials", "death_cure"], {"max_tier": "LOW"}),
    # "<Series> 01 - <Series>": the echo is stripped and the real title matches
    ("series_echo", "Matt Dinniman - Dungeon Crawler Carl 01 - Dungeon Crawler Carl",
     ["dcc_1", "dcc_2", "dcc_4"], {"tier": "HIGH", "asin": "B08V8B2CGV"}),
    ("series_echo_author_in_parent", "Matt Dinniman/Dungeon Crawler Carl 01 - Dungeon Crawler Carl",
     ["dcc_1", "dcc_2", "dcc_4"], {"tier": "HIGH", "asin": "B08V8B2CGV"}),
    # an author-less folder: an exact title with a clear margin is MEDIUM, never HIGH
    ("authorless", "Hollow World",
     ["hollow_world", "emilie_hollow"], {"tier": "MEDIUM", "asin": "B00IFL59DO"}),
    # from the live library: the Dramatized Adaptation's title is one window of the folder name
    # ("Heir of Novron" of six words) and only its subtitle carries the rest - window-only, so it
    # must not reach MEDIUM (one config change from auto-converting)
    ("window_only_dramatization", "Michael J. Sullivan/Michael J. Sullivan - Heir of Novron - Riyira Revelations Book 03",
     ["heir_dramatized", "heir_novron"], {"asin_not": "B09P4BYYX5"}),
]

#: grade() alone: (name, Match kwargs, expected tier)
GRADE_CASES = [
    ("window_only_clears_medium_numbers", dict(title_sim=0.90, author_sim=0.70, margin=0.10,
                                               author_known=True, title_window_coverage=0.50), "LOW"),
    ("full_coverage_same_numbers", dict(title_sim=0.90, author_sim=0.70, margin=0.10,
                                        author_known=True, title_window_coverage=1.0), "MEDIUM"),
    ("window_only_foreign_held_low", dict(title_sim=0.95, author_sim=0.95, margin=0.20, author_known=True,
                                          title_window_coverage=0.50, language="german"), "LOW"),
    ("foreign_full_coverage_capped_medium", dict(title_sim=0.95, author_sim=0.95, margin=0.20,
                                                 author_known=True, language="german"), "MEDIUM"),
]


#: cases that fail today, and why. The test asserts they STILL fail, so a fix has to move the
#: case out of here (and say so) rather than pass unnoticed.
KNOWN_GAPS = {
    # ranking: a knockoff that ties the real edition at score 1.0 (exact title, real author) wins
    # on search order; _rank_key never prefers a non-knockoff. Fails safe: graded LOW.
    "real_book_beats_knockoffs": "knockoff wins a score tie",
    # "series" is a title_sim stopword (weight 0.2): "The Maze Runner Series" ~ "The Maze Runner"
    # at 0.957, so a series-named box set is graded HIGH as book 1. The runtime check holds it.
    "series_named_box_set": "box set graded HIGH as book 1",
    # units._split reads "Dungeon Crawler Carl 01 - Dungeon Crawler Carl" as "<title> - <author>"
    # (the right side looks like a name, the left has a digit), so the echo never reaches the
    # matcher; the parent folder's real author is ignored. Fails safe: graded LOW.
    "series_echo_author_in_parent": "scanner takes the echoed series for the author",
    # window-only match at MEDIUM - matching.grade's MEDIUM branch skips the window_only check
    "window_only_dramatization": "MEDIUM branch skips window_only",
    "grade:window_only_clears_medium_numbers": "MEDIUM branch skips window_only",
    "grade:window_only_foreign_held_low": "MEDIUM branch skips window_only",
}


def library_unit(rel: str) -> BookUnit:
    """The unit the scanner names for library folder ``rel`` (nothing is read from disk)."""
    root = Path("/library")
    path = root / rel
    unit = BookUnit(path=str(path), name=path.name, parent=str(path.parent), parts=[Part(path.name, [])])
    units._name_units([unit], root)
    return unit


def run_case(rel: str, keys: list[str]) -> Match:
    products = [Product.from_audible(FIXTURES[k]) for k in keys]
    with mock.patch.object(catalog, "search", lambda *a, **k: list(products)), \
            mock.patch.object(catalog, "book", lambda asin: {}):
        return matching.match_unit(library_unit(rel))


def case_problems(match: Match, want: dict) -> list[str]:
    """What is wrong with ``match`` against expectation ``want`` ([] = as expected)."""
    problems = []
    if "tier" in want and match.confidence != want["tier"]:
        problems.append(f"tier {match.confidence}, want {want['tier']}")
    if "max_tier" in want and RANK[match.confidence] > RANK[want["max_tier"]]:
        problems.append(f"tier {match.confidence}, want at most {want['max_tier']}")
    if "asin" in want and match.asin != want["asin"]:
        problems.append(f"asin {match.asin or '-'}, want {want['asin']}")
    if "asin_not" in want and match.asin == want["asin_not"] and RANK[match.confidence] >= RANK["MEDIUM"]:
        problems.append(f"{match.asin} at {match.confidence}")
    return problems


class TestMatchFixtures(unittest.TestCase):
    def check(self, name: str, problems: list[str], got: str) -> None:
        if name in KNOWN_GAPS:
            self.assertTrue(problems, f"{name} now passes ({got}): move it out of KNOWN_GAPS")
        else:
            self.assertFalse(problems, f"{problems}: {got}")

    def test_cases(self):
        for name, rel, keys, want in CASES:
            with self.subTest(name):
                match = run_case(rel, keys)
                got = (f"{match.confidence} {match.asin or '-'} t={match.title_sim} a={match.author_sim} "
                       f"margin={match.margin} cov={match.title_window_coverage}")
                self.check(name, case_problems(match, want), got)

    def test_grade_cases(self):
        product = Product(asin="B0TEST", title="Anything", authors=["Someone"], hours=10.0, language="english")
        for name, kwargs, want in GRADE_CASES:
            with self.subTest(name):
                tier = grade(Match(product=product, **kwargs))
                self.check(f"grade:{name}", [] if tier == want else [f"{tier}, want {want}"], tier)


if __name__ == "__main__":
    unittest.main()

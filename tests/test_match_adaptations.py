"""An adaptation (Dramatized Adaptation / GraphicAudio / full cast / audio drama) the folder did
not ask for must never grade HIGH; one the folder does ask for must not be punished.

From the live library: three Riyria Revelations folders converted from the plain Tim Gerard
Reynolds editions now pick the GraphicAudio dramatizations, and "Rise of Empire" did so at HIGH
(its catalog search returned the dramatization alone, so the margin was 0.978).
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from audiobind import catalog, matching
from tests.test_match_fixtures import RANK, case_problems, library_unit, run_case

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "docs" / "match-catalog-cache.json"
S = "Michael J. Sullivan/"

#: (name, library folder, catalog product keys, expectation) - see test_match_fixtures.case_problems
CASES = [
    # 1. a plain folder whose search finds only the dramatization: never HIGH
    ("plain_folder_dramatized_only", S + "Micheal J. Sullivan - Rise of Empire - Riyria Revelations, Book 2",
     ["rise_dramatized"], {"max_tier": "MEDIUM", "asin": "B09P49F6J7"}),
    ("plain_short_folder_dramatized_only", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_dramatized"], {"max_tier": "MEDIUM", "asin": "B09P49F6J7"}),
    # ... and MEDIUM, not LOW: title and author genuinely match, so it stays a suggestion
    ("unasked_adaptation_is_still_medium", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_dramatized"], {"tier": "MEDIUM", "asin": "B09P49F6J7"}),
    # 2. a folder that says it is the dramatization keeps HIGH
    ("folder_says_dramatized", S + "Michael J. Sullivan - Rise of Empire [Dramatized Adaptation]",
     ["rise_dramatized"], {"tier": "HIGH", "asin": "B09P49F6J7"}),
    # 3. GraphicAudio is who makes the [Dramatized Adaptation] titles: any marker asks for one
    ("folder_says_graphicaudio", S + "Michael J. Sullivan - Rise of Empire (GraphicAudio)",
     ["rise_dramatized"], {"tier": "HIGH", "asin": "B09P49F6J7"}),
    # 4. the marker may sit in the subtitle or a parenthetical
    ("full_cast_subtitle", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_full_cast"], {"max_tier": "MEDIUM", "asin": "B0TESTFULC"}),
    ("audio_drama_title", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_audio_drama"], {"max_tier": "MEDIUM", "asin": "B0TESTDRAM"}),
    ("folder_says_full_cast", S + "Michael J. Sullivan - Rise of Empire (Full Cast)",
     ["rise_full_cast"], {"tier": "HIGH", "asin": "B0TESTFULC"}),
    # 5. no regression where a plain edition is the pick
    ("plain_only", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_plain"], {"tier": "HIGH", "asin": "B0081IIZ6Y"}),
    ("theft_plain_only", S + "Michael J. Sullivan - Theft of Swords",
     ["theft_plain"], {"tier": "HIGH", "asin": "B007KSJKI8"}),
]

#: 5. the same title/author with a plain candidate present: the numbers must not move. Pinned
#: from 0.21.0-dev11. (name, folder, keys, (tier, asin, score, title_sim, author_sim, margin))
UNCHANGED = [
    # the dramatization ties the plain edition at 1.0 and, being a different "work" by title,
    # eats its margin: LOW. A pre-existing gap, fails safe; left alone here on purpose
    ("plain_and_dramatized", S + "Michael J. Sullivan - Rise of Empire",
     ["rise_dramatized", "rise_plain"], ("LOW", "B0081IIZ6Y", 1.0, 1.0, 1.0, 0.0)),
    # the live Theft of Swords shape: the dramatization's "Book 1" subtitle out-scores the plain
    # edition's "Volume 1", by 0.022 - MEDIUM before and after
    ("theft_live_shape", S + "Michael J. Sullivan - Theft of Swords - Riyria Revelations - Book 1",
     ["theft_dramatized", "theft_plain"], ("MEDIUM", "B09PQ951KX", 1.0, 1.0, 1.0, 0.022)),
]


#: cases that fail today, and why. The test asserts they STILL fail, so a fix has to move the
#: case out of here (and say so) rather than pass unnoticed.
KNOWN_GAPS = {
    "plain_folder_dramatized_only": "unasked dramatization graded HIGH",
    "plain_short_folder_dramatized_only": "unasked dramatization graded HIGH",
    "unasked_adaptation_is_still_medium": "unasked dramatization graded HIGH",
    "full_cast_subtitle": "unasked full-cast production graded HIGH",
    "audio_drama_title": "unasked audio drama graded HIGH",
    "live_rise_of_empire": "unasked dramatization graded HIGH",
}


def frozen_match(rel: str) -> matching.Match:
    """match_unit for library folder ``rel`` against the committed catalog cache, offline."""
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    with mock.patch.object(catalog, "_get_json", lambda url, *a, **k: data.get(url)):
        return matching.match_unit(library_unit(rel))


class TestAdaptationFixtures(unittest.TestCase):
    def check(self, name: str, problems: list[str], got: str) -> None:
        if name in KNOWN_GAPS:
            self.assertTrue(problems, f"{name} now passes ({got}): move it out of KNOWN_GAPS")
        else:
            self.assertFalse(problems, f"{problems}: {got}")

    def test_cases(self):
        for name, rel, keys, want in CASES:
            with self.subTest(name):
                match = run_case(rel, keys)
                got = f"{match.confidence} {match.asin or '-'} t={match.title_sim} margin={match.margin}"
                self.check(name, case_problems(match, want), got)

    def test_plain_candidate_present_unchanged(self):
        for name, rel, keys, want in UNCHANGED:
            with self.subTest(name):
                m = run_case(rel, keys)
                self.assertEqual((m.confidence, m.asin, m.score, m.title_sim, m.author_sim, m.margin), want)

    def test_live_rise_of_empire_from_frozen_cache(self):
        # the real catalog response: the dramatization alone, margin 0.978. It was HIGH
        match = frozen_match(S + "Micheal J. Sullivan - Rise of Empire - Riyria Revelations, Book 2")
        want = {"max_tier": "MEDIUM", "asin": "B09P49F6J7"}
        self.check("live_rise_of_empire", case_problems(match, want), f"{match.confidence} {match.asin}")


if __name__ == "__main__":
    unittest.main()

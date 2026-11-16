"""An adaptation (Dramatized Adaptation / GraphicAudio / full cast / audio drama) the folder did
not ask for must never grade HIGH; one the folder does ask for must not be punished.

From the live library: three Riyria Revelations folders converted from the plain Tim Gerard
Reynolds editions now pick the GraphicAudio dramatizations, and "Rise of Empire" did so at HIGH
(its catalog search returned the dramatization alone, so the margin was 0.978).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import catalog, matching, pipeline
from audiobind.catalog import Product
from audiobind.matching import Match, grade
from audiobind.probe import Probe
from audiobind.state import State
from audiobind.units import BookUnit, Part
from tests.test_match_fixtures import FIXTURES, RANK, case_problems, library_unit, run_case

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
#: (0.21.0-dev12 moved every Step-1 case out: an unasked adaptation is capped at MEDIUM)
KNOWN_GAPS: dict[str, str] = {}


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


class TestAdaptationLabel(unittest.TestCase):
    def test_markers(self):
        for text, label in [
            ("Rise of Empire [Dramatized Adaptation]", "a Dramatized Adaptation"),
            ("Rise of Empire (dramatised adaptation)", "a Dramatized Adaptation"),
            ("Rise of Empire GraphicAudio", "a GraphicAudio production"),
            ("Rise.of.Empire.Graphic_Audio", "a GraphicAudio production"),
            ("A Full-Cast Production", "a full-cast production"),
            ("Full.Cast", "a full-cast production"),
            ("The BBC Audio Drama", "an audio drama"),
            ("Audio Dramas Collection", "an audio drama"),
        ]:
            with self.subTest(text):
                self.assertEqual(matching.adaptation_label(text), label)

    def test_not_markers(self):
        for text in ["Rise of Empire", "The Graphic Novel", "Castaway", "Broadcast News",
                     "Dramatic Audio", "Full Castle", "The Adaptation", "Audio Dramatics"]:
            with self.subTest(text):
                self.assertEqual(matching.adaptation_label(text), "")

    def test_subtitle_counts_and_the_folder_cancels(self):
        product = Product(asin="B0X", title="Rise of Empire", subtitle="A Full Cast Production")
        self.assertEqual(matching.unasked_adaptation(product, ("Rise of Empire",)), "a full-cast production")
        # any marker on the folder side asks for an adaptation, not only the same one
        self.assertEqual(matching.unasked_adaptation(product, ("Rise of Empire (GraphicAudio)",)), "")
        self.assertEqual(matching.unasked_adaptation(Product(asin="B0Y", title="Rise of Empire"), ("x",)), "")


class TestGradeCap(unittest.TestCase):
    PRODUCT = Product(asin="B0TEST", title="Anything", authors=["Someone"], hours=10.0, language="english")

    def grade(self, **kwargs):
        return grade(Match(product=self.PRODUCT, **kwargs))

    def test_high_numbers_capped_at_medium(self):
        strong = dict(title_sim=1.0, author_sim=1.0, margin=0.9, author_known=True)
        self.assertEqual(self.grade(**strong), "HIGH")
        self.assertEqual(self.grade(**strong, unasked_adaptation="a Dramatized Adaptation"), "MEDIUM")

    def test_cap_never_lifts_or_drops_a_lower_grade(self):
        weak = dict(title_sim=0.86, author_sim=0.7, margin=0.01, author_known=True)
        self.assertEqual(self.grade(**weak), self.grade(**weak, unasked_adaptation="an audio drama"))
        low = dict(title_sim=0.5, author_sim=1.0, margin=0.5, author_known=True)
        self.assertEqual(self.grade(**low, unasked_adaptation="an audio drama"), "LOW")
        title_only = dict(title_sim=1.0, margin=0.5, author_known=False)
        self.assertEqual(self.grade(**title_only, unasked_adaptation="an audio drama"), "MEDIUM")


RISE_BOOK = {"asin": "B09P49F6J7", "title": "Rise of Empire [Dramatized Adaptation]",
             "authors": [{"name": "Michael J. Sullivan"}], "runtimeLengthMin": 1225, "language": "english"}


class TestWantedSide(unittest.TestCase):
    def test_a_pinned_adaptation_stays_high(self):
        # the operator chose it: a pin is never searched and never flagged
        with mock.patch.object(catalog, "book", lambda asin: dict(RISE_BOOK)):
            m = matching.match_unit(library_unit(S + "Michael J. Sullivan - Rise of Empire"), "B09P49F6J7")
        self.assertEqual((m.asin, m.confidence, m.unasked_adaptation), ("B09P49F6J7", "HIGH", ""))

    def test_embedded_tag_title_asks_for_it(self):
        # the ingest retry puts the tag title in title_guess while name stays the folder's
        unit = library_unit(S + "Michael J. Sullivan - Rise of Empire")
        products = [Product.from_audible(FIXTURES["rise_dramatized"])]
        with mock.patch.object(catalog, "search", lambda *a, **k: list(products)), \
                mock.patch.object(catalog, "book", lambda asin: {}):
            plain = matching.match_unit(unit)
            unit.title_guess = "Rise of Empire [Dramatized Adaptation]"
            tagged = matching.match_unit(unit)
        self.assertEqual((plain.confidence, plain.unasked_adaptation), ("MEDIUM", "a Dramatized Adaptation"))
        self.assertEqual((tagged.confidence, tagged.unasked_adaptation), ("HIGH", ""))


KEY = S + "Michael J. Sullivan - Rise of Empire"


class TestPipelineNote(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(Path(self.dir.name) / "state")
        self.converted = []
        products = [Product.from_audible(FIXTURES["rise_dramatized"])]
        patches = [
            # the catalog's own runtime, so the runtime hold cannot be what stops the conversion
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=1225 * 60, codec="mp3", channels="2")),
            mock.patch.object(pipeline.os.path, "getsize", return_value=1),
            mock.patch.object(matching.catalog, "book", lambda asin: {}),
            mock.patch.object(matching.catalog, "search", lambda *a, **k: list(products)),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "effective_auto_tiers", return_value={"HIGH"}),
            mock.patch.object(pipeline, "convert_unit", side_effect=lambda audit, **_k: self.converted.append(1)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_note_evidence_and_no_auto_convert(self):
        path = "/library/" + KEY
        unit = BookUnit(path=path, name="Michael J. Sullivan - Rise of Empire", parent=str(Path(path).parent),
                        parts=[Part("x", [path + "/01.mp3"])], rel_path=KEY,
                        author_guess="Michael J. Sullivan", title_guess="Rise of Empire")
        audit = pipeline.process_unit(self.state, unit, log=lambda *_: None)
        row = self.state.get(KEY)
        self.assertEqual(audit.confidence, "MEDIUM")
        self.assertIn("catalog hit is a Dramatized Adaptation and your folder does not say so", row.note)
        self.assertEqual(json.loads(row.detail)["match"]["unasked_adaptation"], "a Dramatized Adaptation")
        self.assertEqual(self.converted, [])
        self.assertEqual(row.status, "review")


if __name__ == "__main__":
    unittest.main()

""""Why this match?" evidence: the matcher records it, the pipeline keeps it, the panel shows it.

Display data only - none of this may change a score, a grade or a hold.
"""

import json
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

from audiobind import config, matching, pipeline, web
from audiobind.catalog import Product
from audiobind.matching import language_penalized, match_unit, runtime_delta_pct
from audiobind.state import State, UnitState
from audiobind.units import BookUnit, Part


def product(asin, title, author="Charles Duhigg", hours=9.3, language="english"):
    return Product(asin=asin, title=title, authors=[author], hours=hours, language=language)


def unit(title="The Power of Habit", author="Charles Duhigg"):
    return BookUnit(
        path="Duhigg/Habit",
        name="Habit",
        parent="Duhigg",
        parts=[Part(name="Habit", files=["/src/a.mp3"])],
        author_guess=author,
        title_guess=title,
    )


def text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


class TestRuntimeDelta(unittest.TestCase):
    def test_inside_tolerance(self):
        # the review unit: your files 10.5 hrs, catalog 9.3 hrs
        self.assertEqual(runtime_delta_pct(10.5, 9.3), 13)
        self.assertTrue(matching.runtime_ok(10.5, 9.3, 0.25))

    def test_over_tolerance(self):
        self.assertEqual(runtime_delta_pct(12.21, 5.98), 104)
        self.assertFalse(matching.runtime_ok(12.21, 5.98, 0.25))

    def test_shorter_files_are_negative(self):
        self.assertEqual(runtime_delta_pct(10.5, 30.0), -65)

    def test_none_when_either_side_unknown(self):
        self.assertIsNone(runtime_delta_pct(0, 9.3))
        self.assertIsNone(runtime_delta_pct(10.5, 0))
        self.assertIsNone(runtime_delta_pct(None, 9.3))
        self.assertIsNone(runtime_delta_pct(10.5, None))
        self.assertIsNone(runtime_delta_pct("x", 9.3))

    def test_rounds_to_whole_percent(self):
        self.assertIsInstance(runtime_delta_pct(10.0, 9.0), int)
        self.assertEqual(runtime_delta_pct(10.0, 9.0), 11)  # 11.1%


class TestLanguageRule(unittest.TestCase):
    def test_english_is_not_penalized(self):
        self.assertFalse(language_penalized("english"))
        self.assertFalse(language_penalized("English"))

    def test_other_or_blank_is_penalized(self):
        # mirrors the scorer exactly: a blank language is penalized too
        self.assertTrue(language_penalized("spanish"))
        self.assertTrue(language_penalized(""))
        self.assertTrue(language_penalized(None))


class TestMatcherRecordsEvidence(unittest.TestCase):
    PRODUCTS = [
        product("B0FD4CV4BF", "The Power of Habit", hours=9.3),
        product("B0ES", "El poder de los habitos", hours=11.0, language="spanish"),
        product("B0SFB", "Smarter Faster Better", hours=10.4),
        product("B0NOHRS", "The Power of Habit Workbook", hours=0),
    ]

    def _match(self, local_hours=10.5):
        with patch("audiobind.matching.catalog") as cat:
            cat.search.return_value = self.PRODUCTS
            return match_unit(unit(), local_hours=local_hours)

    def test_winner_evidence(self):
        match = self._match()
        self.assertEqual(match.asin, "B0FD4CV4BF")
        self.assertFalse(match.language_penalized)
        self.assertEqual(match.runtime_delta_pct, 13)
        self.assertEqual(match.query_title, "The Power of Habit")
        self.assertEqual(match.query_author, "Charles Duhigg")

    def test_rivals_carry_evidence(self):
        alts = {a["asin"]: a for a in self._match().alternates}
        spanish = alts["B0ES"]
        keys = ("title_sim", "author_sim", "language", "language_penalized", "runtime_delta_pct", "hours")
        for key in keys:
            self.assertIn(key, spanish)
        self.assertEqual(spanish["language"], "spanish")
        self.assertTrue(spanish["language_penalized"])
        self.assertEqual(spanish["runtime_delta_pct"], runtime_delta_pct(10.5, 11.0))
        self.assertFalse(alts["B0SFB"]["language_penalized"])
        self.assertIsNone(alts["B0NOHRS"]["runtime_delta_pct"])

    def test_no_local_hours_means_no_delta(self):
        match = self._match(local_hours=0)
        self.assertIsNone(match.runtime_delta_pct)
        self.assertTrue(all(a["runtime_delta_pct"] is None for a in match.alternates))

    def test_scores_are_unchanged(self):
        """Evidence is additive: the 60/40 blend and the x0.85 penalty are untouched."""
        alts = {a["asin"]: a for a in self._match().alternates}
        spanish = alts["B0ES"]
        expected = round((0.60 * spanish["title_sim"] + 0.40 * spanish["author_sim"]) * 0.85, 3)
        self.assertAlmostEqual(spanish["score"], expected, places=2)
        # the local runtime never moves a score or the grade
        a, b = self._match(local_hours=10.5), self._match(local_hours=99.0)
        self.assertEqual((a.score, a.margin, a.confidence), (b.score, b.margin, b.confidence))
        self.assertEqual([x["score"] for x in a.alternates], [x["score"] for x in b.alternates])

    def test_pin_records_runtime_but_no_penalty(self):
        with patch("audiobind.matching.catalog.book") as book:
            book.return_value = {
                "asin": "B0PIN",
                "title": "El poder de los habitos",
                "authors": [{"name": "Charles Duhigg"}],
                "runtimeLengthMin": 600,
                "language": "spanish",
            }
            match = match_unit(unit(), "B0PIN", local_hours=12.5)
        self.assertTrue(match.asin_override)
        self.assertFalse(match.language_penalized)
        self.assertEqual(match.runtime_delta_pct, 25)


class TestPipelinePersistsEvidence(unittest.TestCase):
    def test_record_writes_evidence_into_the_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                with patch("audiobind.matching.catalog") as cat:
                    cat.search.return_value = TestMatcherRecordsEvidence.PRODUCTS
                    match = match_unit(unit(), local_hours=10.5)
                book = unit()
                book.rel_path = "Duhigg/Habit"
                audit = pipeline.Audit(unit=book, hours=10.5, match=match, confidence=match.confidence)
                pipeline.record(state, audit, "review")
                saved = state.get("Duhigg/Habit")
                data = json.loads(saved.detail)["match"]
                self.assertEqual(data["runtime_delta_pct"], 13)
                self.assertEqual(data["language"], "english")
                self.assertFalse(data["language_penalized"])
                self.assertEqual(data["title_sim"], 1.0)
                self.assertEqual(data["score"], match.score)
                self.assertFalse(data["pinned"])
                alts = json.loads(saved.alternates)
                self.assertTrue(all("language_penalized" in a for a in alts))
            finally:
                state.close()


def alt(**kw):
    base = {
        "asin": "B0X",
        "title": "X",
        "authors": ["Charles Duhigg"],
        "score": 0.7,
        "hours": 10.4,
        "title_sim": 0.9,
        "author_sim": 1.0,
        "language": "english",
        "language_penalized": False,
        "runtime_delta_pct": 1,
    }
    base.update(kw)
    return base


class TestRivalReasons(unittest.TestCase):
    def reason(self, a, local=10.5, tol=0.25, winner=1.0, author_known=True):
        return web.rival_reason(a, local, tol, winner_score=winner, author_known=author_known)

    def test_different_book_by_the_same_author(self):
        line = self.reason(alt(title_sim=0.41, author_sim=1.0))
        self.assertEqual(text(line), "Title match only 41% — a different book by the same author")
        self.assertIn("<b>41%</b>", line)

    def test_wrong_language(self):
        line = self.reason(alt(title_sim=0.686, language="spanish", language_penalized=True))
        self.assertEqual(text(line), "Right title, wrong language — −15% penalty")

    def test_wrong_language_and_weak_title(self):
        line = self.reason(alt(title_sim=0.3, author_sim=0.5, language_penalized=True))
        self.assertTrue(text(line).startswith("Wrong language —"))

    def test_runtime_out_of_tolerance(self):
        line = self.reason(alt(hours=30.0, runtime_delta_pct=-65))
        self.assertEqual(
            text(line), "Runtime 30.0 hrs — your files are 65% shorter, over the 25% tolerance"
        )
        line = self.reason(alt(hours=5.0, runtime_delta_pct=110))
        self.assertIn("110% longer", text(line))

    def test_runtime_inside_tolerance_is_not_a_reason(self):
        # the real rule (|files - catalog| / catalog) puts 10.5 vs 13.4 hrs inside 25%
        line = self.reason(alt(hours=13.4, runtime_delta_pct=-22, language_penalized=True))
        self.assertNotIn("Runtime", text(line))
        self.assertNotIn("runtime", text(line))

    def test_tolerance_comes_from_the_setting(self):
        forty = alt(hours=7.5, runtime_delta_pct=40)  # 10.5 vs 7.5 hrs
        self.assertTrue(self.reason(forty, tol=0.45).startswith("Weaker"))
        self.assertIn("over the 30% tolerance", text(self.reason(forty, tol=0.30)))

    def test_language_and_runtime_combine(self):
        line = text(self.reason(alt(hours=30.0, runtime_delta_pct=-65, language_penalized=True)))
        self.assertTrue(line.startswith("Right title, wrong language"))
        self.assertIn(", runtime 30.0 hrs", line)

    def test_generic(self):
        line = self.reason(alt(title_sim=0.72, author_sim=0.5))
        self.assertEqual(text(line), "Weaker title and author match — title 72%, author 50%")

    def test_generic_without_an_author(self):
        line = self.reason(alt(title_sim=0.72, author_sim=0.0), author_known=False)
        self.assertEqual(text(line), "Weaker title match — title 72%")

    def test_tie_is_not_called_weaker(self):
        line = self.reason(alt(score=1.0), winner=1.0)
        self.assertIn("Same score as the pick", line)

    def test_legacy_row_has_no_reason(self):
        self.assertEqual(self.reason({"asin": "B0X", "title": "X", "score": 0.7}), "")

    def test_no_developer_shorthand(self):
        for a in (
            alt(title_sim=0.41),
            alt(language_penalized=True),
            alt(hours=30.0, runtime_delta_pct=-65),
            alt(title_sim=0.7, author_sim=0.5),
        ):
            line = text(self.reason(a))
            for bad in ("sim", "_", "delta", "penalized", "None"):
                self.assertNotIn(bad, line)


def evidence_detail(**match_kw):
    match = {
        "asin": "B0FD4CV4BF",
        "title": "The Power of Habit",
        "authors": ["Charles Duhigg"],
        "narrators": ["Charles Duhigg"],
        "hours": 9.3,
        "confidence": "LOW",
        "image": "",
        "asin_pin_failed": "",
        "score": 1.0,
        "title_sim": 1.0,
        "author_sim": 1.0,
        "author_known": True,
        "pinned": False,
        "language": "english",
        "language_penalized": False,
        "runtime_delta_pct": 13,
        "query_title": "The Power of Habit",
        "query_author": "Charles Duhigg",
    }
    match.update(match_kw)
    return json.dumps(
        {
            "reopen": False,
            "action": "review",
            "hours": 10.5,
            "match": match,
            "source": {"parts": 1, "files": 3, "mb": 300, "hours": 10.5, "codecs": [], "channels": []},
            "output": None,
        }
    )


RIVALS = [
    alt(asin="B0HI", title="The Power of Habit (Hindi Edition)", score=0.77, hours=13.4, title_sim=0.85,
        language="hindi", language_penalized=True, runtime_delta_pct=-22),
    alt(asin="B0ES", title="El poder de los hábitos", score=0.69, hours=11.0, title_sim=0.686,
        language="spanish", language_penalized=True, runtime_delta_pct=-5),
    alt(asin="B0SFB", title="Smarter Faster Better", score=0.65, title_sim=0.41),
]


class TestEvidencePanel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def save(self, detail=None, alternates=RIVALS, **kw):
        kw.setdefault("path", "Duhigg/Habit")
        kw.setdefault("status", "review")
        kw.setdefault("confidence", "LOW")
        kw.setdefault("asin", "B0FD4CV4BF")
        kw.setdefault("title", "The Power of Habit")
        self.state.save(
            UnitState(detail=detail or evidence_detail(), alternates=json.dumps(alternates), hours=10.5, **kw)
        )

    def panel(self):
        page = web.render(self.state)
        start = page.index('<div class="ab-ev">')
        return page[start : page.index('<div class="ab-edit"', start)]

    def test_mockup_copy_with_the_review_unit_data(self):
        self.save()
        panel = text(self.panel())
        for phrase in (
            "The Audible match",
            "Charles Duhigg &middot; Narrated by Charles Duhigg",
            "9.3 hrs",
            "B0FD4CV4BF",
            "1.00",
            "match score",
            "Title match 100%",
            "&ldquo;The Power of Habit&rdquo; vs &ldquo;The Power of Habit&rdquo;",
            "Author match 100%",
            "&ldquo;Charles Duhigg&rdquo; listed as the author",
            "Runtime +13%",
            "Your files 10.5 hrs vs catalog 9.3 hrs &mdash; inside the 25% tolerance",
            "Language English",
            "No language penalty applied",
            "The other candidates &mdash; and why they lost",
            "Right title, wrong language — −15% penalty",
            "Title match only 41% — a different book by the same author",
            "0.77",
            "0.69",
            "0.65",
        ):
            self.assertIn(phrase, panel)
        # replaces the old list and the old editions block, keeps one-click pins
        self.assertNotIn("Other editions seen", self.panel())
        self.assertEqual(self.panel().count("abUseEdition(this)"), 3)

    def test_runtime_bar_inside_uses_accent_and_scales_to_tolerance(self):
        self.save()
        self.assertIn('<i class="ab-acc" style="width:52%">', self.panel())

    def test_runtime_bar_over_tolerance_is_amber(self):
        self.save(detail=evidence_detail(hours=5.0, runtime_delta_pct=110))
        panel = self.panel()
        self.assertIn('<i class="ab-mid" style="width:100%">', panel)
        self.assertIn("outside the 25% tolerance", panel)
        self.assertIn("Runtime <em>+110%</em>", panel)

    def test_runtime_tolerance_follows_the_setting(self):
        self.state.set_meta("setting_runtime_delta", 40)
        config.set_effective(self.state)  # what every GET does before it renders
        try:
            self.save()
            self.assertIn("inside the 40% tolerance", self.panel())
        finally:
            config._EFFECTIVE.pop("runtime_delta", None)

    def test_runtime_unknown(self):
        self.save(detail=evidence_detail(hours=0, runtime_delta_pct=None))
        panel = self.panel()
        self.assertIn("Runtime <em>&mdash;</em>", panel)
        self.assertIn("catalog lists no runtime", panel)

    def test_language_penalty_is_amber_with_note(self):
        self.save(detail=evidence_detail(language="spanish", language_penalized=True))
        panel = self.panel()
        self.assertIn("Language <em>Spanish</em>", panel)
        self.assertIn("&minus;15% penalty", panel)
        self.assertNotIn("No language penalty applied", panel)

    def test_pinned_match_is_not_presented_as_scored(self):
        self.save(detail=evidence_detail(pinned=True), alternates=[])
        panel = self.panel()
        self.assertIn("pinned by you", panel)
        self.assertNotIn("match score", panel)
        self.assertNotIn("why they lost", panel)

    def test_user_text_is_escaped(self):
        self.save(
            detail=evidence_detail(query_title="<script>x</script> {0}", title="Dune {Unabridged}"),
            alternates=[alt(asin="B0EV", title="<img src=x>")],
        )
        panel = self.panel()
        self.assertNotIn("<script>x", panel)
        self.assertNotIn("<img src=x>", panel)
        self.assertIn("Dune {Unabridged}", panel)

    def test_legacy_row_keeps_the_old_panel(self):
        legacy = json.loads(evidence_detail())
        for key in ("score", "title_sim", "author_sim", "language_penalized", "runtime_delta_pct"):
            legacy["match"].pop(key)
        self.save(detail=json.dumps(legacy), alternates=[{"asin": "B0OLD", "title": "Old", "score": 0.9}])
        page = web.render(self.state)
        self.assertNotIn('class="ab-ev"', page)
        self.assertIn("Other editions seen", page)

    def test_page_script_still_valid_javascript(self):
        import shutil
        import subprocess

        self.save()
        page = web.render(self.state)
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertEqual(len(scripts), 1)
        if shutil.which("node"):
            js = pathlib.Path(self.dir.name) / "page.js"
            js.write_text(scripts[0], encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[:400])


if __name__ == "__main__":
    unittest.main()

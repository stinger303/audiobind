"""The match-test programme, run as part of the suite (see docs/match-testing.md).

``test_gate`` is the gate itself: the bench run offline against the frozen catalog must pass
every stage and match the accepted baseline; its scoreboard is printed to the log.

The rest test the instrument, not the matcher: it must be deterministic, offline, and it must
actually catch the failures it claims to catch - each mutation below breaks the matcher on
purpose (inside the test only) and the gate has to notice.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import catalog, matching
from audiobind.catalog import Product
from audiobind.matching import Match, grade
from tests.matchbench import bench

REPO = Path(__file__).resolve().parent.parent


def _run():
    run, frozen = bench.run_bench()
    return run, bench.gate(run, bench.load_baseline()), frozen


class TestGate(unittest.TestCase):
    def test_gate(self):
        run, g, _ = _run()
        board = bench.scoreboard(run, g)
        print("\n" + board, file=sys.stderr)
        self.assertTrue(g.ok, "match bench gate failed:\n  " + "\n  ".join(g.failures))

    def test_every_lookup_is_frozen_and_nothing_reaches_the_network(self):
        run, _, frozen = _run()
        self.assertEqual(run.misses, [])
        self.assertGreater(len(frozen.used), 100)
        import urllib.request

        with bench.sealed(frozen), self.assertRaises(RuntimeError):
            urllib.request.urlopen("https://api.audible.com/")

    def test_an_empty_cache_fails_admission_without_fetching(self):
        run, _ = bench.run_bench(catalog_data={})
        g = bench.gate(run, bench.load_baseline())
        self.assertTrue(run.misses)
        self.assertTrue(any("not in the frozen cache" in f for f in g.failures))


class TestDeterminism(unittest.TestCase):
    def results(self, seed: str) -> str:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        return subprocess.run(
            [sys.executable, "-m", "tests.matchbench", "results", "--all"],
            cwd=REPO, env=env, capture_output=True, text=True, check=True, timeout=300,
        ).stdout

    def test_same_output_under_different_hash_seeds(self):
        first = self.results("0")
        self.assertGreater(first.count("\n"), 150)
        self.assertEqual(first, self.results("4242"))

    def test_two_runs_in_one_process_agree(self):
        a, _ = bench.run_bench()
        b, _ = bench.run_bench()
        self.assertEqual(a.rows(), b.rows())


class TestTheInstrumentCatchesFailures(unittest.TestCase):
    """Break the matcher on purpose; the gate must say which stage broke."""

    def test_dropping_query_variants_is_a_retrieval_regression(self):
        real = matching.search_queries

        def only_raw_title(title, author):
            queries, n, cleaned = real(title, author)
            return queries[:1], min(n, 1), cleaned

        with mock.patch.object(matching, "search_queries", only_raw_title):
            run, g, _ = _run()
        self.assertTrue(any("left the candidate pool" in r for r in g.regressions), g.regressions[:5])
        self.assertTrue(any(f.startswith("B/C recall fell") for f in g.failures), g.failures[-5:])

    def test_a_grade_that_says_high_too_easily_is_caught(self):
        def reckless(match):
            return "HIGH" if match.product else "NONE"

        with mock.patch.object(matching, "grade", reckless):
            run, g, _ = _run()
        self.assertTrue(any(f.startswith("D wrong at HIGH") for f in g.failures))
        self.assertTrue(any(f.startswith("E ") and "HIGH despite" in f for f in g.failures))

    def test_a_tie_break_change_is_a_decision_change(self):
        real = matching._rank_key

        def reversed_title_tiebreak(entry):
            key = real(entry)
            return (key[0], key[1], -key[2], key[3])

        with mock.patch.object(matching, "_rank_key", reversed_title_tiebreak):
            run, g, _ = _run()
        self.assertTrue(g.changes)
        self.assertFalse(g.ok)

    def test_accept_refuses_a_new_wrong_at_high(self):
        with mock.patch.object(matching, "grade", lambda m: "HIGH" if m.product else "NONE"):
            run, _ = bench.run_bench()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "baseline.csv"
            ok, message = bench.accept(run, "should be refused", path=target)
            self.assertFalse(ok)
            self.assertIn("wrong at HIGH", message)
            self.assertFalse(target.exists())

    def test_accept_refuses_a_regression_without_the_flag(self):
        real = matching.search_queries
        with mock.patch.object(matching, "search_queries", lambda t, a: (real(t, a)[0][:1], 1, real(t, a)[2])):
            run, _ = bench.run_bench()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "baseline.csv"
            target.write_bytes(bench.BASELINE.read_bytes())
            ok, message = bench.accept(run, "fewer queries", path=target)
            self.assertFalse(ok, message)
            self.assertTrue("regression" in message or "cannot waive" in message, message)

    def test_a_fixed_known_defect_must_leave_the_list(self):
        run, _ = bench.run_bench()
        right = next(o for o in run.corpus if o.verdict == "right")
        run.known_defects = dict(run.known_defects, **{right.key: {"tier": "LOW", "symptom": "x"}})
        g = bench.gate(run, bench.load_baseline())
        self.assertTrue(any("no longer reproduces" in f for f in g.failures))

    def test_a_fixed_known_gap_must_leave_the_list(self):
        run, _ = bench.run_bench()
        passing = next(o for o in run.adversarial if not o.expect_problems)
        run.known_gaps = dict(run.known_gaps, **{passing.key: "pretend"})
        g = bench.gate(run, bench.load_baseline())
        self.assertTrue(any("now passes" in f for f in g.failures))


class TestLabels(unittest.TestCase):
    CORPUS = [
        {"key": "A", "status": "converted", "db_tier": "HIGH", "db_asin": "B1", "pin": ""},
        {"key": "B", "status": "converted", "db_tier": "MEDIUM", "db_asin": "B2", "pin": ""},
        {"key": "C", "status": "held", "db_tier": "MEDIUM", "db_asin": "B3", "pin": ""},
        {"key": "D", "status": "converted", "db_tier": "HIGH", "db_asin": "B4", "pin": "B5"},
    ]

    def test_a_row_without_a_label_is_refused(self):
        labels, problems = bench.resolve_labels(self.CORPUS, [])
        self.assertEqual([p for p in problems if "'C'" in p and "no label" in p].__len__(), 1)
        self.assertEqual((labels["A"].src, labels["B"].src, labels["D"].src), ("db-auto", "db-approved", "pin"))
        self.assertEqual(labels["D"].asins, ("B5",))

    def test_orphans_conflicts_and_precedence(self):
        rows = [
            {"key": "C", "expect": "unlabelled", "source": "", "classes": "", "note": "owner"},
            {"key": "A", "expect": "not:B1", "source": "review", "classes": "x", "note": ""},
            {"key": "D", "expect": "B9", "source": "review", "classes": "", "note": ""},
            {"key": "B", "expect": "unlabelled", "source": "", "classes": "", "note": ""},
            {"key": "Z", "expect": "B1", "source": "owner", "classes": "", "note": ""},
        ]
        labels, problems = bench.resolve_labels(self.CORPUS, rows)
        self.assertEqual(labels["A"].text(), "not:B1")  # review outranks db-auto
        self.assertEqual(labels["D"].text(), "B5")  # a pin outranks review
        self.assertTrue(any("'Z'" in p and "orphan" in p for p in problems))
        self.assertTrue(any("'B'" in p and "declared unlabelled" in p for p in problems))
        self.assertFalse(any("'C'" in p for p in problems))

    def test_verdicts(self):
        product = Product(asin="B1", title="T")
        cases = [
            (bench.Label("asin", ("B1",)), "HIGH", "right"),
            (bench.Label("asin", ("B2",)), "HIGH", "wrong"),
            (bench.Label("none"), "LOW", "right"),
            (bench.Label("none"), "MEDIUM", "wrong"),
            (bench.Label("not", ("B1",)), "LOW", "wrong"),
            (bench.Label("not", ("B2",)), "HIGH", "unknown"),
        ]
        for label, tier, want in cases:
            out = bench.Outcome(key="k", label=label, match=Match(product=product), tier=tier)
            self.assertEqual(out.verdict, want, (label, tier))
        self.assertEqual(bench.Outcome(key="k", label=bench.Label("asin", ("B1",)), match=Match()).verdict, "missed")


class TestExplanationMirror(unittest.TestCase):
    def test_limiting_reasons_agree_with_grade(self):
        """Stage E's reasons mirror grade(): no reason <=> HIGH, on a grid of evidence."""
        product = Product(asin="B0", title="T", authors=["A"], hours=10.0)
        n = 0
        for title in (0.80, 0.87, 0.88, 0.95, 1.0):
            for author in (0.0, 0.84, 0.85, 1.0):
                for margin in (0.0, 0.04, 0.05, 0.3):
                    for coverage in (0.5, 1.0):
                        for language in ("english", "german"):
                            for known in (True, False):
                                for knockoff in (False, True):
                                    for adaptation in ("", "a Dramatized Adaptation"):
                                        m = Match(product=product, title_sim=title, author_sim=author,
                                                  margin=margin, title_window_coverage=coverage,
                                                  language=language, author_known=known, knockoff=knockoff,
                                                  unasked_adaptation=adaptation)
                                        tier, reasons = grade(m), bench.limiting_reasons(m)
                                        self.assertEqual(tier == "HIGH", not reasons, (vars(m), tier, reasons))
                                        n += 1
        self.assertGreater(n, 1000)


class TestModelCatalog(unittest.TestCase):
    def test_strict_filters(self):
        raw = [{"asin": "B1", "title": "Rise of Empire [Dramatized Adaptation]", "authors": [{"name": "Michael J. Sullivan"}]},
               {"asin": "B2", "title": "Rise of Empire", "subtitle": "Riyria Revelations, Book 2",
                "authors": [{"name": "Michael J. Sullivan"}]}]
        model = bench.ModelCatalog(raw)
        self.assertEqual([p.asin for p in model.search(title="Rise of Empire")], ["B1", "B2"])
        self.assertEqual(model.search(title="Rise of Empire - Riyria Revelations, Book 2"), [])
        self.assertEqual(model.search(author="Micheal J. Sullivan"), [])
        self.assertEqual([p.asin for p in model.search(keywords="riyria empire")], ["B2"])
        pinned = bench.ModelCatalog(raw, {bench.ModelCatalog.query_key(title="X"): [raw[1]]})
        self.assertEqual([p.asin for p in pinned.search(title="X")], ["B2"])


if __name__ == "__main__":
    unittest.main()

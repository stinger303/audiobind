"""The match-test programme, run as part of the suite (see docs/match-testing.md).

``test_gate`` is the gate itself: the bench run offline against the frozen catalog must pass
every stage and match the accepted baseline; its scoreboard is printed to the log.

The rest test the instrument, not the matcher: it must be deterministic, offline, and it must
actually catch the failures it claims to catch - each mutation below breaks the matcher, a label,
the baseline or the cache on purpose (inside the test only) and the gate has to notice.
``python -m tests.matchbench mutate`` (the constant-by-constant sweep) is a pre-release check and
deliberately not run here; only its machinery is tested.
"""

import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import config, matching
from audiobind.catalog import Product
from audiobind.matching import Match, grade
from audiobind.units import BookUnit, Part
from tests.matchbench import bench

REPO = Path(__file__).resolve().parent.parent


def _run(**kwargs):
    run, frozen = bench.run_bench(**kwargs)
    return run, bench.gate(run, bench.load_baseline()), frozen


def _label_lines():
    return bench.read_csv(bench.LABELS)


def _write_labels(tmp: str, lines: list[dict]) -> Path:
    path = Path(tmp) / "labels.csv"
    bench.write_csv(path, bench.LABEL_FIELDS, lines)
    return path


def _edit(lines, key, **changes):
    out = [dict(r) for r in lines]
    for r in out:
        if r["key"] == key:
            r.update(changes)
            return out
    raise KeyError(key)


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

    def test_every_corpus_row_has_exactly_one_label_line_and_no_row_is_unlabelled(self):
        run, _, _ = _run()
        self.assertEqual(run.admission, [])
        self.assertEqual(sum(o.label.kind == "unlabelled" for o in run.corpus), 0)
        self.assertEqual(len(_label_lines()), len(run.corpus))


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
        # a tier-only change writes two wrong books on this corpus: `ingest:17 Private Beijing`
        # (picks Private Monaco, -2% runtime) and `ingest:In Too Deep` (+3%). Both are the wrong
        # ASIN with a near-identical runtime, so no threshold can catch them - only the grade can.
        # The other five wrong picks a reckless grade promotes are off on runtime and held.
        self.assertEqual(bench.score(run.corpus).convert_wrong, 2)
        self.assertGreater(bench.score(run.corpus).hold_saved, 2)

    def test_a_pick_bug_that_writes_the_wrong_edition_fails_stage_f(self):
        """Take the runner-up instead of the best: same title, another edition, inside the runtime
        tolerance - converted unseen. Only the decision stage measures that."""
        real = matching._pick_and_margin

        def runner_up(ranked):
            best, margin = real(ranked)
            others = [e for e in ranked if e is not best]
            return (others[0], margin) if others else (best, margin)

        with mock.patch.object(matching, "_pick_and_margin", runner_up):
            run, g, _ = _run()
        damage = [f for f in g.failures if f.startswith("F would_convert_wrong:")]
        self.assertTrue(any("Three Body Problem" in f for f in damage), damage)
        self.assertGreater(bench.score(run.corpus).hold_saved, 0)  # the runtime check held the rest

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

    def test_an_embedded_retry_that_always_wins_is_caught(self):
        """The same-tier branch of _embedded_is_better: a tags match that is worse, or better by less
        than EMBEDDED_SCORE_EDGE, must not replace the folder's. Make it always win: both cases fail."""
        from audiobind import pipeline

        with mock.patch.object(pipeline, "_embedded_is_better", lambda e, f: True):
            run, g, _ = _run()
        for case in ("adv:embedded_edge_just_under", "adv:embedded_worse_never_replaces"):
            self.assertTrue(any(f.startswith(f"adversarial {case}:") and "replaced the folder's" in f
                                for f in g.failures), (case, g.failures))
        with mock.patch.object(pipeline, "_embedded_is_better", lambda e, f: False):
            run, g, _ = _run()
        self.assertTrue(any(f.startswith("adversarial adv:embedded_edge_just_over:") and "did not replace" in f
                            for f in g.failures), g.failures)

    def test_a_search_cost_regression_is_shown_but_never_fails_the_gate(self):
        """Every catalog search made twice: no decision moves, so the gate must still pass - but the
        cost must be visible (score-only drift and the search-cost line), not '0 drift'."""
        from audiobind import catalog

        real = matching._score_reading

        def doubled(*args, **kwargs):
            counted = catalog.search

            def twice(*a, **k):
                counted(*a, **k)
                return counted(*a, **k)

            with bench.patched(catalog, search=twice):
                return real(*args, **kwargs)

        base_run, base_g, _ = _run()
        with mock.patch.object(matching, "_score_reading", doubled):
            run, g, _ = _run()
        self.assertTrue(g.ok, g.failures)
        self.assertEqual(g.changes, [])
        self.assertEqual(base_g.searches, base_g.searches_base)
        self.assertGreater(g.searches, 1.5 * g.searches_base)
        self.assertGreater(len(g.search_moves), 100)
        self.assertGreater(g.drift, 100)
        self.assertIn("search cost", bench.scoreboard(run, g))

    # -- the newer expectations must be able to fail: an instrument nobody has watched fail is not one
    @staticmethod
    def _case(name):
        spec = bench.load_adversarial()
        return next(dict(c) for c in spec["cases"] if c["name"] == name), spec["products"]

    def _evaluate(self, case, products):
        with bench.sealed(bench.FrozenCatalog({})):
            return bench.evaluate_case(case, products)

    def test_no_pick_fails_when_a_pick_exists(self):
        # the dead-pin case as written passes: nothing is picked
        case, products = self._case("pin_dead_blocks_the_search")
        self.assertEqual(self._evaluate(case, products).expect_problems, [])
        # the same expectation on the abandoned pin (third check: the search runs and finds Past Tense)
        case["pin_failures"] = 2
        out = self._evaluate(case, products)
        self.assertEqual(out.pick, "B07DK1BBS6")
        self.assertTrue(any("while the pinned ASIN was unresolved" in p for p in out.expect_problems),
                        out.expect_problems)

    def test_no_pick_fails_the_gate_when_a_dead_pin_falls_through_to_a_search(self):
        knob = next(k for k in bench.KNOBS if k.name == "PIN_GIVE_UP_AFTER")
        with knob.set(1):  # give up on the first check: the search runs at once
            run, g, _ = _run()
        for case in ("adv:pin_dead_blocks_the_search", "adv:pin_dead_blocks_the_second_check"):
            self.assertTrue(any(f.startswith(f"adversarial {case}:") and "while the pinned ASIN was unresolved" in f
                                for f in g.failures), (case, g.failures))

    def test_decision_review_fails_on_a_convert(self):
        # a plain HIGH pick whose runtime agrees converts; asserting review on it must fail
        case, products = self._case("same_title_two_authors")
        case["hours"] = 13.17  # legend_gemmell: 790 min
        case["expect"] = dict(case["expect"], decision="review")
        out = self._evaluate(case, products)
        self.assertEqual((out.tier, out.decision), ("HIGH", "convert"))
        self.assertIn("decision convert, want review", out.expect_problems)

    def test_no_write_fails_on_an_unseen_write(self):
        # the regional-edition known gap: a lone other production inside the tolerance converts
        case, products = self._case("regional_edition_unreachable_runtime_agrees")
        out = self._evaluate(case, products)
        self.assertEqual((out.tier, out.decision, out.pick), ("HIGH", "convert", "B0REGION01"))
        self.assertIn("would convert B0REGION01 unseen", out.expect_problems)
        # the held twin meets the same expectation: it is the write that fails, not the wrong pick
        case, products = self._case("regional_edition_unreachable_runtime_holds")
        case["expect"] = dict(case["expect"], no_write=True)
        self.assertEqual(self._evaluate(case, products).expect_problems, [])

    def test_decision_review_fails_the_gate_when_the_abandoned_pin_rule_is_dropped(self):
        """pipeline.py never auto-converts a match made after giving up on a dead pin. Drop that
        rule (clear pin_abandoned) and adv:pin_abandoned_never_converts must fail on its decision."""
        real = matching.match_unit

        def forgetful(*args, **kwargs):
            m = real(*args, **kwargs)
            m.pin_abandoned = ""
            return m

        with mock.patch.object(matching, "match_unit", forgetful):
            run, g, _ = _run()
        self.assertTrue(any(f.startswith("adversarial adv:pin_abandoned_never_converts:")
                            and "decision convert, want review" in f for f in g.failures), g.failures)

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

    @staticmethod
    def line(key, expect, src, note="n"):
        return {"key": key, "expect": expect, "label_source": src, "classes": "", "note": note}

    def test_a_row_without_a_line_is_refused(self):
        labels, problems = bench.resolve_labels(self.CORPUS, [self.line("A", "B1", "db-auto")])
        self.assertTrue(any("'C'" in p and "no line" in p for p in problems))
        self.assertEqual(labels["C"].kind, "unlabelled")
        self.assertEqual(labels["A"].src, "db-auto")

    def test_db_sources_must_agree_with_the_db(self):
        rows = [self.line("A", "B1", "db-approved"),  # the DB says db-auto
                self.line("B", "B9", "db-approved"),  # the DB says B2
                self.line("C", "B3", "db-auto"),      # the DB labels nothing
                self.line("D", "B5", "pin")]
        _, problems = bench.resolve_labels(self.CORPUS, rows)
        for key in "ABC":
            self.assertTrue(any(f"'{key}'" in p and "the DB says" in p for p in problems), (key, problems))
        self.assertFalse(any("'D'" in p for p in problems))

    def test_sources_out_of_scope_orphans_and_pins(self):
        rows = [self.line("A", "not:B1", "inferred"),
                self.line("B", "B2", "review"),             # round-1 name, no longer a source
                self.line("C", "", bench.OUT_OF_SCOPE, note=""),
                self.line("D", "B9", "inferred"),           # overrides the owner's pin
                self.line("Z", "B1", "owner")]
        labels, problems = bench.resolve_labels(self.CORPUS, rows)
        self.assertEqual(labels["A"].text(), "not:B1")
        self.assertTrue(any("'B'" in p and "label_source 'review'" in p for p in problems))
        self.assertTrue(any("'C'" in p and "without a reason" in p for p in problems))
        self.assertTrue(any("'D'" in p and "only the owner" in p for p in problems))
        self.assertTrue(any("'Z'" in p and "orphan" in p for p in problems))
        self.assertEqual(labels["C"].kind, "out")

    def test_verdicts(self):
        product = Product(asin="B1", title="T")
        cases = [
            (bench.Label("asin", ("B1",)), "HIGH", "right"),
            (bench.Label("asin", ("B2",)), "HIGH", "wrong"),
            (bench.Label("none"), "LOW", "right"),
            (bench.Label("none"), "MEDIUM", "wrong"),
            (bench.Label("not", ("B1",)), "LOW", "wrong"),
            (bench.Label("not", ("B2",)), "HIGH", "unknown"),
            (bench.Label("out", src=bench.OUT_OF_SCOPE), "HIGH", "out-of-scope"),
        ]
        for label, tier, want in cases:
            out = bench.Outcome(key="k", label=label, match=Match(product=product), tier=tier)
            self.assertEqual(out.verdict, want, (label, tier))
        self.assertEqual(bench.Outcome(key="k", label=bench.Label("asin", ("B1",)), match=Match()).verdict, "missed")
        errored = bench.Outcome(key="k", label=bench.Label("asin", ("B1",)), match=Match(product=product),
                                error="1 catalog lookup(s) missing")
        self.assertEqual(errored.verdict, "error")


class TestEvidence(unittest.TestCase):
    """Stage L: every label's evidence is recomputed, and a contradicted label fails the gate."""

    def unit(self, name, author="", title=""):
        return BookUnit(path="/library/" + name, name=name, parent="/library", parts=[Part(name=name, files=["/x.mp3"])],
                        author_guess=author, title_guess=title or name)

    def test_containment_tolerates_misspelling_order_stopwords_and_zero_padding(self):
        self.assertEqual(bench.containment("David Baldacci", "David Bldacci"), 1.0)
        self.assertEqual(bench.containment("Liu Cixin", "Cixin Liu"), 1.0)
        self.assertEqual(bench.containment("The God Delusion", "God Delusion"), 1.0)
        self.assertEqual(bench.containment("Private: #1 Suspect", "Jack Morgan 02 - Private No 01 Suspect"), 1.0)
        self.assertEqual(bench.containment("The Missing", "The Guardians; 3 Island of Glass"), 0.0)
        self.assertIsNone(bench.containment("The", "anything"))

    def evidence(self, label, product, hours="10.00", author="Lee Child", title="Make Me"):
        row = {"key": "k", "hours": hours}
        unit = self.unit(f"{author} - {title}", author, title)
        return bench.label_evidence(row, unit, label, {product.asin: product}, 0.25)

    def test_a_label_off_by_more_than_the_tolerance_is_contradicted(self):
        p = Product(asin="B1", title="Make Me", authors=["Lee Child"], hours=14.0)
        ev = self.evidence(bench.Label("asin", ("B1",), "inferred"), p, hours="10.00")
        self.assertEqual(ev.verdict, "contradicted")
        self.assertIn("runtime", ev.contradictions[0])
        self.assertIn("-29%", ev.contradictions[0])

    def test_another_author_or_another_title_is_contradicted(self):
        p = Product(asin="B1", title="Make Me", authors=["Somebody Else"], hours=10.0)
        ev = self.evidence(bench.Label("asin", ("B1",), "inferred"), p)
        self.assertEqual(ev.verdict, "contradicted")
        self.assertTrue(ev.contradictions[0].startswith("author"))
        p = Product(asin="B1", title="The Missing", authors=["Lee Child"], hours=10.0)
        ev = self.evidence(bench.Label("asin", ("B1",), "inferred"), p)
        self.assertTrue(any(c.startswith("title") for c in ev.contradictions))

    def test_corroborated_consistent_and_unverifiable(self):
        p = Product(asin="B1", title="Make Me", authors=["Lee Child"], hours=10.2)
        self.assertEqual(self.evidence(bench.Label("asin", ("B1",), "inferred"), p).verdict, "corroborated")
        p = Product(asin="B1", title="Make Me", authors=["Lee Child"], hours=12.0)
        ev = self.evidence(bench.Label("asin", ("B1",), "inferred"), p)
        self.assertEqual(ev.verdict, "consistent")
        self.assertFalse(ev.runtime_corroborates)
        ev = self.evidence(bench.Label("asin", ("B404",), "inferred"), p)
        self.assertEqual(ev.verdict, "unverifiable")

    def test_a_not_label_whose_excluded_edition_agrees_everywhere_is_contradicted(self):
        p = Product(asin="B1", title="Make Me", authors=["Lee Child"], hours=10.0)
        self.assertEqual(self.evidence(bench.Label("not", ("B1",), "inferred"), p).verdict, "contradicted")
        p = Product(asin="B1", title="Make Me", authors=["Lee Child"], hours=5.0)
        self.assertEqual(self.evidence(bench.Label("not", ("B1",), "inferred"), p).verdict, "consistent")

    def gate_with(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = bench.run_bench(labels_path=_write_labels(tmp, lines))
        return bench.gate(run, bench.load_baseline())

    def test_a_contradicted_label_in_labels_csv_fails_the_gate_with_the_numbers(self):
        # point Private Beijing's label at The Hard Way (14.23h) for 8.84h files: wrong title and
        # a -38% runtime gap, which is outside the 35% a label is allowed before it contradicts
        lines = _edit(_label_lines(), "ingest:17 Private Beijing", expect="B00LI7VIRS")
        g = self.gate_with(lines)
        hits = [f for f in g.failures if f.startswith("L contradiction: ingest:17 Private Beijing")]
        self.assertTrue(any("runtime" in f and "-38%" in f for f in hits), hits)
        self.assertTrue(any("title" in f for f in hits), hits)

    def test_one_asin_on_two_different_books_fails(self):
        lines = _edit(_label_lines(), "ingest:24 Blue Moon", expect="B004PKD9MM")  # The Hard Way's ASIN
        g = self.gate_with(lines)
        self.assertTrue(any("B004PKD9MM labels two different books" in f for f in g.failures))

    def test_the_same_book_in_two_places_may_share_an_asin(self):
        run, _, _ = _run()
        self.assertFalse(any("labels two different books" in c for c in run.labels.contradictions))
        shared = [o.key for o in run.corpus if o.label.kind == "asin" and "B0FD4CV4BF" in o.label.asins]
        self.assertEqual(len(shared), 3)

    def test_an_uncorroborated_inferred_label_fails(self):
        # Make Me: the only US edition is -17% - inside tolerance, outside the inferred band
        lines = _edit(_label_lines(), "ingest:20 Make Me", expect="B011PVUGUG", label_source="inferred")
        g = self.gate_with(lines)
        self.assertTrue(any("ingest:20 Make Me" in f and "not corroborated" in f for f in g.failures), g.failures)

    def test_an_inferred_label_may_not_override_a_db_label_its_evidence_supports(self):
        lines = _edit(_label_lines(), "Marie Lu/Legend", expect="not:B006BCXJ90", label_source="inferred", note="x")
        g = self.gate_with(lines)
        self.assertTrue(any("Marie Lu/Legend" in f and "overrides the DB's" in f for f in g.failures), g.failures)

    def test_stale_evidence_columns_fail(self):
        lines = _edit(_label_lines(), "Marie Lu/Legend", label_h="9.99")
        g = self.gate_with(lines)
        self.assertTrue(any(f.startswith("L stale evidence: Marie Lu/Legend") for f in g.failures))

    def test_inferred_labels_all_carry_their_evidence_tuple(self):
        for line in _label_lines():
            if line["label_source"] == "inferred" and line["expect"] not in ("none",):
                for col in ("local_h", "label_asin", "label_h", "delta_pct", "label_title", "title_match", "evidence"):
                    self.assertTrue(line[col], (line["key"], col))


class TestDecision(unittest.TestCase):
    """Stage F: what the pipeline would do, with the configured tolerance."""

    def test_the_tolerance_comes_from_the_settings_path(self):
        base, _ = bench.run_bench()
        holds = bench.score(base.corpus).decisions["hold"]
        had = "runtime_delta" in config._EFFECTIVE
        config._EFFECTIVE["runtime_delta"] = 5  # what the settings page stores: a percent
        try:
            tight, _ = bench.run_bench()
        finally:
            if not had:
                config._EFFECTIVE.pop("runtime_delta", None)
        self.assertEqual(tight.tolerance, 0.05)
        self.assertEqual(tight.tolerance_source, "settings override")
        self.assertGreater(bench.score(tight.corpus).decisions["hold"], holds)
        self.assertGreater(bench.score(tight.corpus).hold_wrong, 0)

    def test_decide_follows_choose_action_and_lists_unknown_runtime(self):
        unit = BookUnit(path="/library/x", name="x", parent="/library", parts=[Part(name="x", files=["/a.mp3"])])
        m4b = BookUnit(path="/ingest/x.m4b", name="x", parent="/ingest", parts=[Part(name="x", files=["/x.m4b"])])
        p = Product(asin="B1", title="T", hours=10.0)
        self.assertEqual(bench.decide(unit, Match(product=p), "HIGH", 10.1)[:3], ("convert", "convert", "agree"))
        self.assertEqual(bench.decide(m4b, Match(product=p), "HIGH", 10.1)[0], "remux")
        self.assertEqual(bench.decide(unit, Match(product=p), "HIGH", 5.0)[0], "hold")
        self.assertEqual(bench.decide(unit, Match(product=p), "MEDIUM", 10.0)[0], "review")  # AUTO_CONVERT=high
        unknown = Product(asin="B2", title="T", hours=0.0)
        decision, _, state, _ = bench.decide(unit, Match(product=unknown), "HIGH", 10.0)
        self.assertEqual((decision, state), ("hold", "unknown"))  # no runtime is not agreement

    def test_an_unseen_write_with_unknown_runtime_fails_the_gate(self):
        run, _ = bench.run_bench()
        o = next(o for o in run.corpus if o.decision == "convert")
        o.runtime = "unknown"
        g = bench.gate(run, bench.load_baseline())
        self.assertTrue(any(o.key in f and "runtime unknown" in f for f in g.failures))

    def test_the_baseline_records_every_decision(self):
        rows = bench.read_csv(bench.BASELINE)
        self.assertTrue(all(r["decision"] in ("convert", "remux", "hold", "review") for r in rows))


class TestFailClosed(unittest.TestCase):
    def test_a_baseline_missing_a_row_fails_loudly(self):
        run, _ = bench.run_bench()
        baseline = bench.load_baseline()
        dropped = sorted(baseline)[3]
        del baseline[dropped]
        g = bench.gate(run, baseline)
        self.assertTrue(any("STALE" in f and dropped in f for f in g.failures))

    def test_a_baseline_with_an_extra_row_fails_loudly(self):
        run, _ = bench.run_bench()
        baseline = dict(bench.load_baseline(), ghost={"key": "ghost"})
        g = bench.gate(run, baseline)
        self.assertTrue(any("STALE" in f and "ghost" in f for f in g.failures))

    def test_a_baseline_missing_a_column_fails_loudly(self):
        run, _ = bench.run_bench()
        header = [c for c in bench.RESULT_FIELDS if c != "decision"]
        g = bench.gate(run, bench.load_baseline(), header)
        self.assertTrue(any("STALE" in f and "decision" in f for f in g.failures))

    def test_a_cache_miss_is_a_bench_error_on_its_row_not_a_none(self):
        data = bench.load_catalog()
        victim = "Marie Lu/Legend"
        run, _ = bench.run_bench(dict(data))
        legend = next(o for o in run.corpus if o.key == victim)
        # drop every response this one row reads
        probe = bench.FrozenCatalog(dict(data))
        with bench.sealed(probe):
            bench.evaluate_row(next(r for r in bench.read_csv(bench.CORPUS) if r["key"] == victim), legend.label, probe)
        trimmed = {u: v for u, v in data.items() if u not in probe.used}
        run, _ = bench.run_bench(trimmed)
        out = next(o for o in run.corpus if o.key == victim)
        self.assertEqual(out.verdict, "error")
        self.assertEqual(bench.result_row(out)["tier"], "ERR")
        g = bench.gate(run, bench.load_baseline())
        self.assertTrue(any(f.startswith(f"A {victim}:") and "not a NONE" in f for f in g.failures))

    def test_a_label_change_is_shown_but_is_not_a_matcher_regression(self):
        run, _ = bench.run_bench()
        baseline = bench.load_baseline()
        key = "ingest:17 Private Beijing"
        baseline[key] = dict(baseline[key], label="B0XXXXXXXX", verdict="wrong")
        g = bench.gate(run, baseline)
        self.assertTrue(any(c.startswith(f"{key}: LABEL") for c in g.changes))
        self.assertFalse(any(key in r for r in g.regressions))


class TestGradeMirror(unittest.TestCase):
    def grid(self):
        product = Product(asin="B0", title="T", authors=["A"], hours=10.0)
        for title in (0.80, 0.85, 0.87, 0.88, 0.95, 0.96, 1.0):
            for author in (0.0, 0.59, 0.60, 0.84, 0.85, 1.0):
                for margin in (0.0, 0.04, 0.05, 0.09, 0.10, 0.3):
                    for coverage in (0.5, 1.0):
                        for language in ("english", "german"):
                            for known in (True, False):
                                for knockoff, edition, veto, hint in (
                                        (False, "", "", ""), (True, "", "", ""),
                                        (False, "an abridged edition", "", ""), (False, "", "another volume", ""),
                                        (False, "", "", "another book")):
                                    for adaptation, missing in (("", ""), ("a Dramatized Adaptation", ""),
                                                                ("", "a full-cast production")):
                                        yield Match(product=product, title_sim=title, author_sim=author,
                                                    margin=margin, title_window_coverage=coverage,
                                                    language=language, author_known=known, knockoff=knockoff,
                                                    unasked_adaptation=adaptation, edition_conflict=edition,
                                                    veto=veto, missing_adaptation=missing,
                                                    hint_conflict=hint)

    def test_limiting_reasons_and_grade_with_agree_with_grade(self):
        """Stage E's reasons and mutate's grade mirror both reproduce grade() on a grid of evidence."""
        n = 0
        for m in self.grid():
            tier = grade(m)
            self.assertEqual(bench.grade_with(m, bench.GRADE_GATES), tier, vars(m))
            self.assertEqual(tier == "HIGH", not bench.limiting_reasons(m), (vars(m), tier))
            n += 1
        self.assertGreater(n, 8000)


class TestMutate(unittest.TestCase):
    BRIEF = ("TITLE_FLOOR", "SUBTITLE_GATE", "MIN_HIGH_WINDOW_COVERAGE", "LANGUAGE_PENALTY",
             "grade.medium_title", "grade.medium_author", "EMBEDDED_SCORE_EDGE", "PIN_GIVE_UP_AFTER",
             "runtime tolerance")

    def test_every_gate_constant_has_a_knob_whose_default_is_the_code_value(self):
        names = {k.name for k in bench.KNOBS}
        for name in self.BRIEF:
            self.assertIn(name, names)
        for knob in bench.KNOBS:
            self.assertEqual(knob.current(), knob.default, knob.name)
            self.assertTrue(any(v < knob.default for v in knob.values), knob.name)
            self.assertTrue(any(v > knob.default for v in knob.values), knob.name)

    def test_a_knob_moves_the_outcome_and_is_restored(self):
        knob = next(k for k in bench.KNOBS if k.name == "TITLE_FLOOR")
        base, _ = bench.run_bench()
        with knob.set(0.95):
            self.assertEqual(matching.TITLE_FLOOR, 0.95)
            moved, _ = bench.run_bench()
        self.assertEqual(matching.TITLE_FLOOR, 0.60)
        self.assertNotEqual(bench._decisions(base), bench._decisions(moved))

    def test_gate_and_tolerance_knobs_restore(self):
        for name, probe in (("grade.high_title", lambda: bench._ACTIVE_GATES["high_title"]),
                            ("runtime tolerance", lambda: config.effective_val("max_runtime_delta"))):
            knob = next(k for k in bench.KNOBS if k.name == name)
            before = probe()
            with knob.set(0.5):
                self.assertEqual(probe(), 0.5)
            self.assertEqual(probe(), before)
        self.assertIs(matching.grade, grade)

    def test_classify(self):
        def row(v, flips):
            return {"value": str(v), "real_flips": str(flips), "adv_flips": "0", "errors": "0"}
        self.assertTrue(bench.classify([row(0.5, 0), row(0.7, 0)], 0.6).startswith("NO EFFECT"))
        self.assertTrue(bench.classify([row(0.5, 1), row(0.7, 2)], 0.6).startswith("load-bearing"))
        self.assertTrue(bench.classify([row(0.4, 1), row(0.5, 0), row(0.7, 0), row(0.8, 1)], 0.6).startswith("slack"))
        self.assertTrue(bench.classify([row(0.5, 0), row(0.7, 1)], 0.6).startswith("one-sided"))
        # a label-only constant: no decision flips, but labels become contradicted - an outcome, not NO EFFECT
        labelled = [dict(row(0.2, 0), label_contradicted="6"), dict(row(0.5, 0), label_contradicted="0")]
        self.assertTrue(bench.classify(labelled, 0.35, 425, 0).startswith("one-sided: only lowering"))
        self.assertTrue(bench.classify(labelled, 0.35).startswith("NO EFFECT"))  # without the base: blind

    def test_the_label_tolerance_knob_reaches_stage_l(self):
        """check_labels' tolerance default is bound at import; the run must read the module value."""
        knob = next(k for k in bench.KNOBS if k.name == "LABEL_RUNTIME_TOLERANCE")
        with knob.set(0.15):
            run, _ = bench.run_bench()
        self.assertEqual(bench.LABEL_RUNTIME_TOLERANCE, 0.35)
        self.assertGreater(bench._summary(run)["label_contradicted"], 0)
        self.assertTrue(any(f.startswith("L contradiction") for f in bench.gate(run, bench.load_baseline()).failures))

    def test_the_recorded_profile_covers_every_knob(self):
        with open(bench.MUTATIONS, newline="", encoding="utf-8") as fh:
            recorded = {(r["constant"], r["value"]) for r in csv.DictReader(fh)}
        for knob in bench.KNOBS:
            for v in knob.values:
                self.assertIn((knob.name, f"{v:g}"), recorded)


class TestCaseMutation(unittest.TestCase):
    """`mutate --cases`: the expectations are flipped, not the code. A flip the gate does not report
    means the expectation tests nothing - the bench could say PASS whatever the matcher did."""

    @classmethod
    def setUpClass(cls):
        cls.rows, _ = bench.case_mutation_profile()

    def _with_cases(self, *cases):
        spec = bench.load_adversarial()
        spec["cases"] = spec["cases"] + list(cases)
        return mock.patch.object(bench, "load_adversarial", lambda *a, **k: spec)

    def test_the_recorded_profile_is_current_and_no_case_is_vacuous(self):
        with open(bench.CASE_MUTATIONS, newline="", encoding="utf-8") as fh:
            recorded = list(csv.DictReader(fh))
        self.assertEqual(self.rows, recorded, "run `python -m tests.matchbench mutate --cases --record` and "
                                              "say in the commit why an expectation changed")
        self.assertEqual(bench.vacuous_cases(self.rows), [])
        cases = {c["name"] for c in bench.load_adversarial()["cases"]}
        self.assertEqual({r["case"] for r in self.rows}, cases, "every case has at least one expectation")

    def test_every_expectation_of_a_known_gap_is_load_bearing(self):
        """A gap only had to "still fail", so its other expectations could break unseen (15 of them
        did not register a flip). gap_fails pins how it fails; now every one does."""
        gaps = [r for r in self.rows if r["gap"]]
        self.assertGreaterEqual(len({r["case"] for r in gaps}), 12)
        self.assertEqual([(r["case"], r["key"], r["verdict"]) for r in gaps if r["verdict"] != bench.LOAD_BEARING], [])

    def test_what_is_not_load_bearing_is_only_a_label_under_a_tier_ceiling(self):
        """The accepted remainder: a label (asin_not / none) in a passing case whose max_tier already
        forbids everything the label could flag. Redundant, not vacuous - and nothing else may join it.

        ADV-CASES raised this from 9 to 10 deliberately: `asin_hint_right_name_wrong` pins a wrong pick
        under a LOW ceiling, where the picked-ASIN label can never be load-bearing (the ceiling already
        forbids everything it could flag). The label is still required - the harness reads it as the
        case's labelled edition for the pool check - so it is a reviewed remainder, not a vacuous case.
        """
        spec = {c["name"]: c["expect"] for c in bench.load_adversarial()["cases"]}
        rest = [r for r in self.rows if r["verdict"] != bench.LOAD_BEARING]
        self.assertEqual(len(rest), 10)
        for r in rest:
            self.assertEqual(r["verdict"], bench.BASELINE_ONLY, r)
            if r["case"] == "asin_hint_right_name_wrong":
                # the one documented exception: its `asin` is the case's labelled edition
                self.assertEqual(r["key"], "asin", r)
                continue
            self.assertIn(r["key"], ("asin_not", "none"), r)
            self.assertIn("max_tier", spec[r["case"]], r)

    def test_a_vacuous_case_is_reported_and_fails_the_command(self):
        # two expectations that are true of every outcome: the case passes whatever the matcher does
        vacuous = {"name": "zz_vacuous", "classes": [], "folder": "Andy Weir - The Martian", "catalog": ["martian"],
                   "expect": {"min_tier": "NONE", "max_tier": "HIGH"}, "why": "test"}
        with self._with_cases(vacuous):
            rows, _ = bench.case_mutation_profile(only="zz_vacuous")
            text, code = bench.mutate_cases(only="zz_vacuous")
        self.assertEqual({r["key"]: r["verdict"] for r in rows},
                         {"min_tier": bench.DECORATION, "max_tier": bench.DECORATION})
        self.assertEqual(bench.vacuous_cases(rows), ["zz_vacuous"])
        self.assertEqual(code, 1)
        self.assertIn("VACUOUS CASES (no load-bearing expectation): 1", text)

    def test_a_misspelt_expectation_fails_admission(self):
        typo = {"name": "zz_typo", "classes": [], "folder": "Andy Weir - The Martian", "catalog": ["martian"],
                "expect": {"asin": "B082BHJMFF", "max_teir": "LOW"}, "why": "test"}
        with self._with_cases(typo):
            run, g, _ = _run()
        out = next(o for o in run.adversarial if o.key == "adv:zz_typo")
        self.assertEqual((out.tier, out.expect_problems), ("HIGH", []))  # the case itself "passes"
        self.assertTrue(any(f.startswith("A adversarial.json: 'zz_typo' expects max_teir") for f in g.failures),
                        g.failures)

    def test_a_known_gap_that_fails_differently_fails_the_gate(self):
        """torrent_noise is accepted as "the right book, graded LOW". Relabel it so the pick is the
        wrong book: it still fails, so the old gate ("a known gap must keep failing") said nothing."""
        spec = bench.load_adversarial()
        case = next(c for c in spec["cases"] if c["name"] == "torrent_noise")
        case["expect"] = dict(case["expect"], asin="B009WROSXW")
        with mock.patch.object(bench, "load_adversarial", lambda *a, **k: spec):
            run, g, _ = _run()
        out = next(o for o in run.adversarial if o.key == "adv:torrent_noise")
        self.assertTrue(out.expect_problems)
        self.assertTrue(any("adv:torrent_noise is a known gap that now fails differently" in f for f in g.failures),
                        g.failures)
        # and a gap without its pin (or a pin without a gap) is refused at admission
        del case["gap_fails"]
        with mock.patch.object(bench, "load_adversarial", lambda *a, **k: spec):
            _, g, _ = _run()
        self.assertTrue(any("'torrent_noise' needs known_gap (why) and gap_fails" in f for f in g.failures))

    def test_the_flags_are_two_way(self):
        spec = bench.load_adversarial()
        case = next(c for c in spec["cases"] if c["name"] == "swap_by_author")  # HIGH, right, in pool, held

        def problems(**expect):
            with bench.sealed(bench.FrozenCatalog({})):
                return bench.evaluate_case(dict(case, expect=dict(asin="B082BHJMFF", **expect)),
                                           spec["products"]).expect_problems

        self.assertEqual(problems(must_pick=True, in_pool=True, no_write=True, no_pick=False), [])
        self.assertEqual(len(problems(must_pick=False, in_pool=False, no_write=False, no_pick=True)), 4)


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

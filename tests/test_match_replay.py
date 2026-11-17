"""The independent replay (docs/match-replay.py) agrees with the bench's baseline - and can disagree.

The replay is a second path to the same per-row answer: the app's scanner names the units from a
real (temporary) tree and ``pipeline.audit_unit`` decides them. It must share no code with
``tests/matchbench``; this file does not import the bench either, so a failure here is a
disagreement between two instruments, never one instrument checking itself.
"""

import ast
import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import config, matching

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "docs" / "match-replay.py"

_spec = importlib.util.spec_from_file_location("match_replay", SCRIPT)
replay_mod = importlib.util.module_from_spec(_spec)
sys.modules["match_replay"] = replay_mod  # dataclasses resolve their module by name
_spec.loader.exec_module(replay_mod)

HARD_WAY = "Jack Reacher Series/10 The Hard Way"


class TestReplayAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = replay_mod.replay()
        cls.baseline = replay_mod.load_baseline()

    def test_the_two_paths_agree_on_every_corpus_row_today(self):
        differences = replay_mod.compare(self.result, self.baseline)
        self.assertEqual(differences, [], "\n".join(differences))

    def test_every_corpus_row_was_replayed_none_skipped(self):
        corpus = replay_mod.read_csv(replay_mod.CORPUS)
        self.assertEqual(len(self.result.rows), len(corpus))
        self.assertEqual([r.key for r in self.result.rows if r.problem], [])
        self.assertEqual({r.key for r in self.result.rows}, set(self.baseline))
        self.assertEqual(self.result.strays, [])
        # a compared row always carries a tier and a decision: "" == "" must never count as agreement
        self.assertTrue(all(r.tier and r.decision for r in self.result.rows))

    def test_a_changed_baseline_value_is_reported_in_each_compared_column(self):
        for col, value in (("asin", "B0NOTTHIS1"), ("tier", "LOW"), ("decision", "review")):
            baseline = dict(self.baseline)
            baseline[HARD_WAY] = dict(baseline[HARD_WAY], **{col: value})
            differences = replay_mod.compare(self.result, baseline)
            self.assertEqual(len(differences), 1, differences)
            self.assertTrue(differences[0].startswith(f"{HARD_WAY}: {col.upper()} replay "), differences)
            self.assertIn(f"!= baseline {value}", differences[0])

    def test_a_row_missing_from_either_side_is_reported(self):
        baseline = dict(self.baseline)
        del baseline[HARD_WAY]
        baseline["Nobody/No Such Book"] = {"asin": "", "tier": "NONE", "decision": "review"}
        differences = replay_mod.compare(self.result, baseline)
        self.assertIn(f"{HARD_WAY}: in the corpus, not in the baseline", differences)
        self.assertIn("Nobody/No Such Book: in the baseline, not in the corpus", differences)


class TestReplaySeesTheApp(unittest.TestCase):
    """The replay runs the app, not a recording: moving the app moves the replay."""

    def test_a_wider_runtime_tolerance_changes_decisions_and_the_replay_says_so(self):
        # the settings page stores a percent (20 = 20 %); the Hard Way's wrong HIGH pick is held at
        # 15 % by a -18 % runtime gap and converts at 20 %
        with mock.patch.dict(config._EFFECTIVE, {"runtime_delta": 20}):
            result = replay_mod.replay()
        self.assertAlmostEqual(result.tolerance, 0.20)  # the replay reports the tolerance it decided with
        differences = replay_mod.compare(result, replay_mod.load_baseline())
        self.assertIn(f"{HARD_WAY}: DECISION replay convert != baseline hold", differences)
        self.assertTrue(all(": DECISION replay " in d for d in differences), differences)

    def test_a_stricter_title_floor_changes_tiers_and_the_replay_says_so(self):
        with mock.patch.object(matching, "TITLE_FLOOR", 0.999):
            result = replay_mod.replay()
        differences = replay_mod.compare(result, replay_mod.load_baseline())
        self.assertTrue(any(": TIER replay NONE != baseline " in d for d in differences), differences)

    def test_a_row_the_scanner_cannot_produce_is_a_finding_not_a_skip(self):
        rows = replay_mod.read_csv(replay_mod.CORPUS)
        victim = next(r for r in rows if r["key"] == HARD_WAY)
        victim["shape"] = "bucket"  # a bucket key needs "<parent>#<key>"; this one has none
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            result = replay_mod.replay(corpus_path=path)
        differences = replay_mod.compare(result, replay_mod.load_baseline())
        self.assertEqual(len(differences), 1, differences)
        self.assertTrue(differences[0].startswith(f"{HARD_WAY}: NOT REPLAYED - not laid out"), differences)

    def test_a_catalog_answer_missing_from_the_cache_is_a_finding_not_a_none(self):
        import json

        data = json.loads(replay_mod.CACHE.read_text(encoding="utf-8"))
        gone = next(u for u in sorted(data) if "title=The+Hard+Way" in u)
        del data[gone]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = replay_mod.replay(cache_path=path)
        row = next(r for r in result.rows if r.key == HARD_WAY)
        self.assertIn("missing from the frozen cache", row.problem)
        self.assertEqual((row.asin, row.tier, row.decision), ("", "", ""))


class TestReplayIsIndependent(unittest.TestCase):
    def test_the_script_imports_nothing_from_the_tests_package(self):
        imported = set()
        for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertEqual(sorted(m for m in imported if m.split(".")[0] in ("tests", "bench", "matchbench")), [])
        self.assertFalse(any(m.startswith(".") for m in imported), imported)
        self.assertNotIn("import_module", SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn("__import__", SCRIPT.read_text(encoding="utf-8"))

    def test_it_runs_as_a_script_agrees_and_never_loads_the_bench(self):
        # a fresh interpreter: main() itself exits 1 if any tests.* module was loaded on the way
        done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120,
                              cwd=tempfile.gettempdir())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("REPLAY: AGREES", done.stdout)
        self.assertNotIn("NOT INDEPENDENT", done.stdout)


if __name__ == "__main__":
    unittest.main()

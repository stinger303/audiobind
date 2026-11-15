"""Regression tests for the v0.14.0 audit fixes."""

import unittest
from unittest.mock import patch

from audiobind import config
from audiobind.catalog import Product
from audiobind.control import Control
from audiobind.encode import _metadata_map_args
from audiobind.matching import Match, grade


class TestMetadataMapArgs(unittest.TestCase):
    """The ffmetadata file's input index moves when the cover is missing."""

    def test_with_cover_the_metadata_file_is_input_two(self):
        self.assertEqual(
            _metadata_map_args(True), ["-map_metadata", "2", "-map_chapters", "-1", "-map_chapters", "2"]
        )

    def test_without_cover_the_metadata_file_is_input_one(self):
        args = _metadata_map_args(False)
        self.assertEqual(args[1], "1")
        self.assertEqual(args[-1], "1")
        self.assertIn("-map_chapters", args)
        self.assertIn("-1", args)


class TestEffectiveAutoTiers(unittest.TestCase):
    """The settings page stores the tier uppercase; the lookup is case-insensitive."""

    def tearDown(self):
        config._EFFECTIVE.clear()

    def test_uppercase_medium_override_now_applies(self):
        config._EFFECTIVE["auto_convert"] = "MEDIUM"
        self.assertEqual(config.effective_auto_tiers(), {"HIGH", "MEDIUM"})

    def test_uppercase_off_override_now_applies(self):
        config._EFFECTIVE["auto_convert"] = "OFF"
        self.assertEqual(config.effective_auto_tiers(), set())

    def test_unknown_value_falls_back_to_high(self):
        config._EFFECTIVE["auto_convert"] = "banana"
        self.assertEqual(config.effective_auto_tiers(), {"HIGH"})


class TestEnvFloat(unittest.TestCase):
    def test_garbage_value_falls_back(self):
        with patch.dict("os.environ", {"MAX_RUNTIME_DELTA": "abc"}):
            cfg = config.Config.from_env() if hasattr(config.Config, "from_env") else None
        if cfg is None:
            self.skipTest("no from_env constructor")
        self.assertEqual(cfg.max_runtime_delta, 0.25)


class TestGradeGuards(unittest.TestCase):
    @staticmethod
    def product(language="english"):
        return Product(asin="B0TEST", title="T", authors=["A"], hours=10, language=language)

    def test_non_english_match_is_capped_at_medium(self):
        m = Match(
            product=self.product("deutsch"),
            title_sim=1.0,
            author_sim=1.0,
            margin=0.2,
            author_known=True,
            language="deutsch",
        )
        self.assertEqual(grade(m), "MEDIUM")

    def test_english_exact_match_still_high(self):
        m = Match(
            product=self.product(),
            title_sim=1.0,
            author_sim=1.0,
            margin=0.2,
            author_known=True,
            language="english",
        )
        self.assertEqual(grade(m), "HIGH")

    def test_medium_requires_a_positive_margin(self):
        m = Match(product=self.product(), title_sim=0.9, author_sim=0.7, margin=0.0, author_known=True)
        self.assertEqual(grade(m), "LOW")


class TestConversionQueue(unittest.TestCase):
    def test_enqueue_dequeue_roundtrip(self):
        c = Control()
        c.enqueue("A/B")
        c.enqueue("A/B")  # duplicate clicks do not stack
        c.enqueue("C/D")
        self.assertEqual(c.queue_snapshot()["count"], 2)
        self.assertEqual(c.queue_snapshot()["units"], ["A/B", "C/D"])

    def test_begin_unit_moves_the_book_out_of_the_queue(self):
        c = Control()
        c.enqueue("A/B")
        c.begin_unit("A/B")
        self.assertEqual(c.queue_snapshot()["count"], 0)
        self.assertEqual(c.current_unit, "A/B")

    def test_describe_carries_the_queue(self):
        c = Control()
        c.enqueue("A/B")
        self.assertEqual(c.describe()["queue"]["count"], 1)


if __name__ == "__main__":
    unittest.main()


class EntrypointRefusesRoot(unittest.TestCase):
    """0.19.0-dev5: PUID/PGID 0 is refused unless ALLOW_ROOT=1."""

    def run_entry(self, **env):
        import os
        import pathlib
        import stat
        import subprocess
        import tempfile

        root = pathlib.Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "audiobind"
            fake.write_text("#!/bin/sh\necho started\n")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            full = {"PATH": f"{tmp}:{os.environ['PATH']}", "STATE_DIR": tmp, **env}
            return subprocess.run(["sh", str(root / "entrypoint.sh")], env=full, capture_output=True, text=True)

    def test_zero_is_refused(self):
        for env in ({"PUID": "0"}, {"PGID": "0"}, {"PUID": "00", "PGID": "100"}, {"PUID": "0", "PGID": "0"}):
            out = self.run_entry(**env)
            self.assertEqual(out.returncode, 1, env)
            self.assertIn("refusing to run as root", out.stderr)
            self.assertNotIn("started", out.stdout)

    def test_allow_root_is_explicit(self):
        import os
        if os.geteuid() == 0:
            self.skipTest("as root the entrypoint would call gosu")
        out = self.run_entry(PUID="0", ALLOW_ROOT="1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("started", out.stdout)
        self.assertEqual(self.run_entry(PUID="0", ALLOW_ROOT="yes").returncode, 1)

    def test_non_numeric_is_refused_and_ordinary_ids_pass(self):
        import os
        self.assertEqual(self.run_entry(PUID="abc").returncode, 1)
        if os.geteuid() != 0:
            out = self.run_entry(PUID="99", PGID="100")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("started", out.stdout)
            self.assertEqual(self.run_entry(PUID="007", PGID="100").returncode, 0)

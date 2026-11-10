import unittest

from audiobind.advice import explain

DETAIL = {"source": {"hours": 9.74}, "match": {"hours": 3.45, "asin": "B1"}, "output": {"chapters": 16}}


class TestExplain(unittest.TestCase):
    def test_converted_says_nothing_to_do(self):
        why, todo = explain("converted", "HIGH", "", {"output": {"chapters": 16}})
        self.assertIn("16 chapters", why)
        self.assertIn("Nothing to do", todo)

    def test_dry_run_ready_explains_the_switch(self):
        why, todo = explain("ready", "HIGH", "dry run - nothing written", {})
        self.assertIn("dry run", why.lower())
        self.assertIn("Turn Dry Run off", todo)

    def test_held_shows_both_runtimes(self):
        why, todo = explain("held", "HIGH", "runtime 9.74h vs catalog 3.45h", DETAIL)
        self.assertIn("9.74", why)
        self.assertIn("3.45", why)
        self.assertIn("Convert anyway", todo)

    def test_held_parses_runtimes_from_a_legacy_note(self):
        why, _todo = explain("held", "HIGH", "runtime 12.95h vs catalog 7.65h", {})
        self.assertIn("12.95", why)
        self.assertIn("7.65", why)

    def test_interrupted_says_partial_file_discarded(self):
        why, todo = explain("interrupted", "HIGH", "", {})
        self.assertIn("thrown away", why)
        self.assertIn("Retry now", todo)

    def test_unreadable_source_is_named(self):
        why, todo = explain("review", "HIGH", "3 unreadable source file(s); not converted", {})
        self.assertIn("3 source file", why)
        self.assertIn("re-rip", todo)

    def test_no_match_points_at_the_asin_field(self):
        why, todo = explain("review", "NONE", "", {})
        self.assertIn("couldn't find this book on Audible", why)
        self.assertIn("ASIN", todo)

    def test_low_confidence_explains_the_disagreement(self):
        why, todo = explain("review", "LOW", "", {})
        self.assertIn("isn't trustworthy", why)
        self.assertIn("ASIN", todo)

    def test_failed_quotes_the_error(self):
        why, todo = explain("failed", "HIGH", "ffmpeg failed: invalid data", {})
        self.assertIn("invalid data", why)
        self.assertIn("Retry", todo)

    def test_collection_suggests_splitting(self):
        why, todo = explain("review", "COLLECTION", "", {})
        self.assertIn("several books", why)
        self.assertIn("Split", todo)

    def test_medium_fallback_names_the_actual_tier(self):
        why, todo = explain("review", "MEDIUM", "", {}, tier="high")
        self.assertIn("Auto-convert is set to High", why)
        self.assertIn("Convert anyway", todo)

    def test_medium_fallback_respects_the_off_tier(self):
        why, todo = explain("review", "MEDIUM", "", {}, tier="off")
        self.assertIn("auto-convert is off", why)
        self.assertNotIn("set to High", why)

    def test_a_dead_pinned_asin_is_explained(self):
        detail = {
            "match": {
                "asin": "B00LZZQBIY",
                "title": "Get Up!",
                "hours": 7.52,
                "confidence": "LOW",
                "asin_pin_failed": "B013ILGDHQ",
            },
            "source": {"hours": 0.99},
        }
        why, todo = explain("review", "LOW", "", detail)
        self.assertIn("The ASIN you set (B013ILGDHQ) is not in Audible's catalog - nothing is published", why)
        self.assertIn("The ASIN you set stays saved", todo)
        self.assertNotIn("pin", (why + todo).lower())

    def test_collection_advice_says_set_not_pin(self):
        _why, todo = explain("review", "COLLECTION", "", {})
        self.assertIn("or set it to one exact Audible entry", todo)
        self.assertNotIn("pin", todo.lower())


if __name__ == "__main__":
    unittest.main()

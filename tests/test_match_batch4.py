"""0.21.0-dev11 matching fixes: window coverage follows floor_sim, window-only never MEDIUM,
a dead ASIN pin gives up after a bounded number of checks."""

import unittest

from audiobind import matching
from audiobind.catalog import Product


class TestWindowCoverageFollowsFloorSim(unittest.TestCase):
    WANTED = "Moneyball The Art of Winning an Unfair Game"

    def test_subtitle_behind_the_gate_does_not_widen_coverage(self):
        # the title alone ("Zzyzx") is nowhere in the folder name, so floor_sim never scores the
        # subtitle - and the coverage must not take its 7-word window from it either
        decoy = Product(asin="B0DECOY", title="Zzyzx", subtitle="The Art of Winning an Unfair Game")
        self.assertLess(matching.title_alone(self.WANTED, decoy), matching.SUBTITLE_GATE)
        self.assertEqual(matching.floor_candidates(self.WANTED, decoy), ["Zzyzx"])
        bare = Product(asin="B0DECOY", title="Zzyzx")
        self.assertEqual(matching.window_coverage(self.WANTED, decoy), matching.window_coverage(self.WANTED, bare))
        self.assertLess(matching.window_coverage(self.WANTED, decoy), matching.MIN_HIGH_WINDOW_COVERAGE)
        self.assertEqual(matching.floor_sim(self.WANTED, decoy), matching.floor_sim(self.WANTED, bare))

    def test_subtitle_past_the_gate_still_counts(self):
        # "Moneyball" is a window of the folder name: the subtitle is scored, and covers the rest
        real = Product(asin="B005EHN61O", title="Moneyball", subtitle="The Art of Winning an Unfair Game")
        self.assertGreaterEqual(matching.title_alone(self.WANTED, real), matching.SUBTITLE_GATE)
        self.assertIn("Moneyball The Art of Winning an Unfair Game", matching.floor_candidates(self.WANTED, real))
        self.assertEqual(matching.floor_sim(self.WANTED, real), 1.0)
        self.assertEqual(matching.window_coverage(self.WANTED, real), 1.0)

    def test_coverage_window_is_the_floor_sim_window(self):
        # the window window_coverage picks scores exactly floor_sim
        for title, subtitle in (("Dune", "Dune, Book 1"), ("Zzyzx", "Dune Messiah"), ("Heir of Novron", "")):
            product = Product(asin="B0X", title=title, subtitle=subtitle)
            wanted = "Dune Messiah Heir of Novron"
            tokens = wanted.split()
            n = round(matching.window_coverage(wanted, product) * len(tokens))
            best = max(
                matching.sim(" ".join(tokens[i:i + n]), c)
                for i in range(len(tokens) - n + 1)
                for c in matching.floor_candidates(wanted, product)
            )
            self.assertEqual(best, round(matching.floor_sim(wanted, product), 3), (title, subtitle))


if __name__ == "__main__":
    unittest.main()

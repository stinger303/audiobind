import unittest

from audiobind.util import clean, natkey, norm, safe_name, sim


class TestNorm(unittest.TestCase):
    def test_strips_accents_and_punctuation(self):
        self.assertEqual(norm("Brian Jacques -- Redwall!"), "brian jacques redwall")

    def test_ampersand_becomes_and(self):
        self.assertEqual(norm("Bad Luck & Trouble"), "bad luck and trouble")


class TestClean(unittest.TestCase):
    def test_removes_rip_noise(self):
        self.assertEqual(clean("The Innocent (Unabridged)"), "The Innocent")
        self.assertEqual(clean("King and Maxwell [Audobook] MP3"), "King and Maxwell")

    def test_removes_year_suffix(self):
        self.assertEqual(clean("Rogue (2012)"), "Rogue")

    def test_keeps_real_words(self):
        self.assertIn("Part", clean("Part of Your World"))


class TestSim(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(sim("Red Rising", "Red Rising"), 1.0)

    def test_containment(self):
        self.assertGreaterEqual(sim("Moneyball", "Moneyball The Art of Winning"), 0.85)

    def test_unrelated(self):
        self.assertLess(sim("Red Rising", "Pride and Prejudice"), 0.4)


class TestHelpers(unittest.TestCase):
    def test_safe_name_strips_path_separators(self):
        self.assertNotIn("/", safe_name("Author/Title: Part 1?"))
        self.assertEqual(safe_name(""), "Unknown")

    def test_natkey_orders_numbers(self):
        items = ["Part 10", "Part 2", "Part 1"]
        self.assertEqual(sorted(items, key=natkey), ["Part 1", "Part 2", "Part 10"])


if __name__ == "__main__":
    unittest.main()

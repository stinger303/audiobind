import unittest

from audiobind.util import clean, natkey, norm, safe_name, sim, title_sim


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
        # 0.21.0-dev7: containment counts by the share of words covered, for a multi-word side only
        # (one word inside a longer string no longer earns 0.85); the near title still matches
        # through the title scorer, where "The Art of Winning" is the catalog's subtitle
        from audiobind.catalog import Product
        from audiobind.matching import title_score

        self.assertGreaterEqual(sim("Moneyball The Art", "Moneyball The Art of Winning"), 0.85 * 3 / 5)
        moneyball = Product(title="Moneyball", subtitle="The Art of Winning an Unfair Game")
        self.assertGreaterEqual(title_score("Moneyball", moneyball), 0.85)

    def test_unrelated(self):
        self.assertLess(sim("Red Rising", "Pride and Prejudice"), 0.4)

    def test_one_word_inside_a_longer_title_is_not_a_near_match(self):
        self.assertLess(sim("dune", "dune messiah"), 0.8)
        self.assertLess(sim("blue moon", "once in a blue moon"), 0.8)


class TestTitleSim(unittest.TestCase):
    def test_a_leading_article_does_not_count(self):
        self.assertGreaterEqual(title_sim("Midnight Line", "The Midnight Line"), 0.95)
        self.assertGreaterEqual(title_sim("Power of Habit", "The Power of Habit"), 0.95)

    def test_one_changed_word_is_not_a_near_match(self):
        self.assertLess(title_sim("Blue Moon", "Blue Noon"), 0.9)

    def test_a_typo_still_scores(self):
        self.assertGreaterEqual(title_sim("Steeleart", "Steelheart"), 0.9)

    def test_identical_and_empty(self):
        self.assertEqual(title_sim("Red Rising", "red rising!"), 1.0)
        self.assertEqual(title_sim("", "Red Rising"), 0.0)

    def test_a_repeated_word_is_not_double_counted(self):
        from audiobind.util import _weighted_dice

        # a multiset intersection: one "private" matches one "private" (a set would say 2/3)
        self.assertEqual(_weighted_dice("private private", "private dublin"), 0.5)
        self.assertLess(title_sim("Private Private", "Private Dublin"), 0.75)


class TestHelpers(unittest.TestCase):
    def test_safe_name_strips_path_separators(self):
        self.assertNotIn("/", safe_name("Author/Title: Part 1?"))
        self.assertEqual(safe_name(""), "Unknown")

    def test_natkey_orders_numbers(self):
        items = ["Part 10", "Part 2", "Part 1"]
        self.assertEqual(sorted(items, key=natkey), ["Part 1", "Part 2", "Part 10"])


if __name__ == "__main__":
    unittest.main()


class TestSafeNameControlChars(unittest.TestCase):
    def test_control_characters_become_a_space(self):
        self.assertEqual(safe_name("Dune\x00Messiah"), "Dune Messiah")
        self.assertEqual(safe_name("Line\none\r\ntwo\ttab\x7fend"), "Line one two tab end")
        self.assertEqual(safe_name("\x00\x01\x1f"), "Unknown")
        for ch in map(chr, list(range(32)) + [127]):
            self.assertNotIn(ch, safe_name(f"a{ch}b"))

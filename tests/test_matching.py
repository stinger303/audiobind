import unittest

from audiobind.catalog import Product
from audiobind.matching import Match, grade, runtime_ok


def product(title="Moneyball", author="Michael Lewis", hours=10.43):
    return Product(asin="B005EHN61O", title=title, authors=[author], hours=hours,
                   language="english")


class TestGrade(unittest.TestCase):
    def test_title_and_author_agree(self):
        match = Match(product=product(), title_sim=1.0, author_sim=1.0, margin=0.4, author_known=True)
        self.assertEqual(grade(match), "HIGH")

    def test_title_exact_but_author_unknown_is_medium(self):
        match = Match(product=product(), title_sim=1.0, author_sim=0.0, margin=0.3, author_known=False)
        self.assertEqual(grade(match), "MEDIUM")

    def test_ambiguous_title_only_is_low(self):
        match = Match(product=product(), title_sim=1.0, author_sim=0.0, margin=0.0, author_known=False)
        self.assertEqual(grade(match), "LOW")

    def test_author_mismatch_downgrades(self):
        match = Match(product=product(), title_sim=1.0, author_sim=0.2, margin=0.3, author_known=True)
        self.assertEqual(grade(match), "LOW")

    def test_no_product(self):
        self.assertEqual(grade(Match()), "NONE")


class TestRuntimeCheck(unittest.TestCase):
    def test_close_enough(self):
        self.assertTrue(runtime_ok(10.5, 10.43, 0.25))

    def test_wrong_edition_is_rejected(self):
        self.assertFalse(runtime_ok(9.74, 3.45, 0.25))     # radio drama vs novel
        self.assertFalse(runtime_ok(12.21, 5.98, 0.25))

    def test_unknown_catalog_runtime_passes(self):
        self.assertTrue(runtime_ok(9.0, 0.0, 0.25))


if __name__ == "__main__":
    unittest.main()

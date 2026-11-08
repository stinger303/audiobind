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


class TestAsinPin(unittest.TestCase):
    def test_match_unit_records_a_pin_that_has_no_catalog_data(self):
        from audiobind import catalog, matching
        from audiobind.units import BookUnit
        saved_book, saved_search = catalog.book, catalog.search
        catalog.book = lambda asin: {}
        catalog.search = lambda *a, **k: []
        try:
            unit = BookUnit(path="X", name="X", parent="", parts=[],
                            title_guess="Get Up", author_guess="James Levine")
            match = matching.match_unit(unit, "B013ILGDHQ")
        finally:
            catalog.book, catalog.search = saved_book, saved_search
        self.assertIsNone(match.product)
        self.assertEqual(match.asin_pin_failed, "B013ILGDHQ")

    def test_a_live_pinned_asin_does_not_set_the_failure_flag(self):
        from audiobind import catalog, matching
        from audiobind.units import BookUnit
        saved = catalog.book
        catalog.book = lambda asin: {
            "asin": asin, "title": "Get Up!", "authors": [{"name": "James A. Levine"}],
            "narrators": [], "runtimeLengthMin": 451, "language": "english"}
        try:
            unit = BookUnit(path="X", name="X", parent="", parts=[],
                            title_guess="Get Up", author_guess="James Levine")
            match = matching.match_unit(unit, "B00LZZQBIY")
        finally:
            catalog.book = saved
        self.assertIsNotNone(match.product)
        self.assertEqual(match.asin_pin_failed, "")
        self.assertEqual(match.confidence, "HIGH")


if __name__ == "__main__":
    unittest.main()

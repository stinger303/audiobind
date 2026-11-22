"""A whitespace-only title has no readings; it must not crash the query builder.

Found by the widened property grid (tests/test_match_properties.py) after MATCHING-04/05:
`search_queries(" ", "")` raised IndexError on `variants[-1]` because title_variants()
returns [] for whitespace. Pre-existing on 0.25.4, not a regression from those fixes.
"""

import unittest

from audiobind.matching import search_queries


class TestBlankTitleQueries(unittest.TestCase):
    def test_whitespace_title_builds_no_title_query(self):
        for title in (" ", "\t", "   \n "):
            with self.subTest(title=title):
                queries, n_variants, cleaned = search_queries(title, "---")
                self.assertEqual(n_variants, 0)
                self.assertEqual(cleaned, "")
                for query in queries:
                    self.assertNotIn("title", query)

    def test_whitespace_title_with_author_still_searches_the_author(self):
        queries, n_variants, _ = search_queries("  ", "James Patterson")
        self.assertEqual(n_variants, 0)
        self.assertIn({"author": "James Patterson"}, queries)

    def test_a_real_title_is_unaffected(self):
        queries, n_variants, cleaned = search_queries("1. Dune", "Frank Herbert")
        self.assertGreaterEqual(n_variants, 1)
        self.assertTrue(cleaned)
        self.assertIn({"title": "1. Dune", "author": "Frank Herbert"}, queries)


if __name__ == "__main__":
    unittest.main()

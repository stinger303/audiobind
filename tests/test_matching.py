import unittest

from audiobind.catalog import Product
from audiobind.matching import Match, grade, runtime_ok


def product(title="Moneyball", author="Michael Lewis", hours=10.43):
    return Product(asin="B005EHN61O", title=title, authors=[author], hours=hours, language="english")


class TestGrade(unittest.TestCase):
    def test_foreign_language_still_has_to_clear_the_gates(self):
        german = Product(asin="B0DE", title="Moneyball", authors=["Michael Lewis"], hours=10.0, language="german")
        weak = Match(product=german, title_sim=0.42, author_sim=1.0, margin=0.2, author_known=True, language="german")
        self.assertEqual(grade(weak), "LOW")

    def test_foreign_language_is_capped_at_medium(self):
        german = Product(asin="B0DE", title="Moneyball", authors=["Michael Lewis"], hours=10.0, language="german")
        strong = Match(product=german, title_sim=0.95, author_sim=0.95, margin=0.2, author_known=True, language="german")
        self.assertEqual(grade(strong), "MEDIUM")

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
        self.assertFalse(runtime_ok(9.74, 3.45, 0.25))  # radio drama vs novel
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
        calls = []

        def fake_search(*a, **k):
            calls.append(a)
            return []

        catalog.search = fake_search
        try:
            unit = BookUnit(
                path="X", name="X", parent="", parts=[], title_guess="Get Up", author_guess="James Levine"
            )
            match = matching.match_unit(unit, "B013ILGDHQ")
        finally:
            catalog.book, catalog.search = saved_book, saved_search
        self.assertIsNone(match.product)
        self.assertEqual(match.asin_pin_failed, "B013ILGDHQ")
        # a dead pin must never fall through to a search that could convert
        # a different edition
        self.assertEqual(calls, [])
        self.assertEqual(match.confidence, "NONE")

    def test_a_live_pinned_asin_does_not_set_the_failure_flag(self):
        from audiobind import catalog, matching
        from audiobind.units import BookUnit

        saved = catalog.book
        catalog.book = lambda asin: {
            "asin": asin,
            "title": "Get Up!",
            "authors": [{"name": "James A. Levine"}],
            "narrators": [],
            "runtimeLengthMin": 451,
            "language": "english",
        }
        try:
            unit = BookUnit(
                path="X", name="X", parent="", parts=[], title_guess="Get Up", author_guess="James Levine"
            )
            match = matching.match_unit(unit, "B00LZZQBIY")
        finally:
            catalog.book = saved
        self.assertIsNotNone(match.product)
        self.assertEqual(match.asin_pin_failed, "")
        self.assertEqual(match.confidence, "HIGH")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 0.21.0-dev7: the matcher's inputs
from unittest import mock  # noqa: E402

from audiobind import catalog, matching  # noqa: E402
from audiobind.units import BookUnit  # noqa: E402
from audiobind.util import norm, sim  # noqa: E402


def book_unit(title, author="", **extra):
    return BookUnit(path="X", name="X", parent="", parts=[], title_guess=title, author_guess=author, **extra)


def patterson(asin, title, hours=8.0):
    return Product(asin=asin, title=title, authors=["James Patterson"], hours=hours, language="english")


class StrictCatalog:
    """A fake catalog.search with Audible's strict ``title=`` filter: a title query finds a book
    only when it is (nearly) the book's own title. Every call is recorded."""

    def __init__(self, books, keywords_find=()):
        self.books = list(books)
        self.keywords_find = list(keywords_find)
        self.calls: list[dict] = []

    def __call__(self, title=None, author=None, limit=10, keywords=None):
        self.calls.append({k: v for k, v in (("title", title), ("author", author), ("keywords", keywords)) if v})
        if keywords:
            return [b for b in self.keywords_find if norm(keywords) in norm(f"{b.title} {' '.join(b.authors)}")]
        found = []
        for b in self.books:
            if title and norm(title) != norm(b.title):
                continue
            if author and not any(sim(author, a) >= 0.85 for a in b.authors):
                continue
            found.append(b)
        return found[:limit]


class TestCatalogParentheticals(unittest.TestCase):
    LOCK_IN = Product(asin="B0LOCKIN01", title="Lock In (Narrated by Wil Wheaton)", authors=["John Scalzi"])

    def test_a_narrator_parenthetical_on_the_catalog_side_is_ignored(self):
        self.assertGreaterEqual(matching.title_score("Lock In", self.LOCK_IN), 0.95)
        self.assertGreaterEqual(matching.floor_sim("Lock In", self.LOCK_IN), 0.95)
        self.assertEqual(matching.window_coverage("Lock In", self.LOCK_IN), 1.0)

    def test_stripping_does_not_loosen_a_sequel(self):
        self.assertLess(matching.floor_sim("Dune", Product(asin="B0DUNEMSG1", title="Dune Messiah")), 0.60)
        self.assertLess(matching.floor_sim("Dune", Product(asin="B0DUNEMSG2", title="Dune Messiah (Unabridged)")), 0.60)

    def test_a_stripped_subtitle_cannot_stand_in_for_a_title(self):
        decoy = Product(asin="B0DECOY002", title="Fall From Grace", subtitle="(Lee Child)", authors=["X"])
        self.assertLess(matching.floor_sim("Lee Child", decoy), 0.60)
        self.assertLess(matching.title_score("Lee Child", decoy), 0.60)


class TestSubtitleCannotCarryTheTitle(unittest.TestCase):
    def test_a_subtitle_that_names_the_query_earns_nothing(self):
        decoy = Product(asin="B0DECOY001", title="Fall From Grace",
                        subtitle="A Thriller for Fans Hunting Lee Child's Jack Reacher", authors=["Someone Else"])
        self.assertLess(matching.floor_sim("Lee Child", decoy), 0.60)
        self.assertLess(matching.title_score("Lee Child", decoy), 0.60)

    def test_a_subtitle_still_counts_once_the_title_does(self):
        moneyball = Product(asin="B005EHN61O", title="Moneyball", subtitle="The Art of Winning an Unfair Game")
        # the folder carries title + subtitle: the catalog title is a window of it, so the gate opens
        self.assertGreaterEqual(matching.title_score("Moneyball The Art of Winning an Unfair Game", moneyball), 0.95)
        self.assertGreaterEqual(matching.floor_sim("Moneyball The Art of Winning an Unfair Game", moneyball), 0.95)

    def test_each_side_of_an_aka_is_scored_on_its_own(self):
        self.assertEqual(matching.catalog_titles("Count to Ten aka Private Delhi"),
                         ["Count to Ten aka Private Delhi", "Count to Ten", "Private Delhi"])
        self.assertEqual(matching.catalog_titles("Moneyball"), ["Moneyball"])
        aka = patterson("B0COUNT001", "Count to Ten a.k.a. Private Delhi")
        self.assertEqual(matching.title_score("Private Delhi", aka), 1.0)
        # and no containment boost comes back with it
        self.assertLess(matching.title_score("Dune", Product(title="Dune Messiah")), 0.85)


class TestRetrieval(unittest.TestCase):
    DUBLIN = patterson("B00DUBLIN1", "Private Dublin")
    OTHERS = [patterson("B0STEP0001", "Step on a Crack"), patterson("B0SPIDER01", "Along Came a Spider")]

    def run_match(self, unit, fake):
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            return matching.match_unit(unit)

    def test_a_variant_query_finds_the_book_the_raw_title_cannot(self):
        fake = StrictCatalog([self.DUBLIN, *self.OTHERS])
        m = self.run_match(book_unit("Private 20 - Private Dublin", "James Patterson"), fake)
        self.assertEqual(fake.calls[0], {"title": "Private 20 - Private Dublin"})
        self.assertIn({"title": "Private Dublin"}, fake.calls)
        self.assertEqual(m.asin, "B00DUBLIN1")
        self.assertLessEqual(len(fake.calls), matching.MAX_QUERIES)

    def test_a_bare_word_number_title_is_a_variant(self):
        self.assertIn("Private Rome", matching.title_variants("Private 18 Private Rome"))
        self.assertNotIn("Yossarian", matching.title_variants("Catch 22 Yossarian"))

    def test_a_series_book_tail_is_dropped_as_a_variant(self):
        for title, bare in [
            ("Rise of Empire - Riyria Revelations, Book 2", "Rise of Empire"),
            ("Theft of Swords - Riyria Revelations - Book 1", "Theft of Swords"),
            ("Heir of Novron - Riyira Revelations Book 03", "Heir of Novron"),
            ("Heir of Novron — Riyria Revelations Volume 3", "Heir of Novron"),
        ]:
            with self.subTest(title):
                self.assertEqual(matching.title_variants(title), [title, bare])
        # no series words before "Book N", or no dash: nothing to drop
        for title in ["Title - Book 2", "The Book 2", "Dune"]:
            with self.subTest(title):
                self.assertEqual(matching.title_variants(title), [title])

    def test_rip_noise_is_stripped_for_a_query(self):
        self.assertEqual(matching.strip_rip_noise("Killing Floor Chapterized 64kbps (Read by Dick Hill)"),
                         "Killing Floor")
        self.assertEqual(matching.strip_rip_noise("Die Trying by chapter (MP3)"), "Die Trying")
        queries, _n, cleaned = matching.search_queries("Killing Floor chapterized", "Lee Child")
        self.assertEqual(cleaned, "Killing Floor")
        self.assertIn({"title": "Killing Floor"}, queries)
        self.assertIn({"title": "Killing Floor", "author": "Lee Child"}, queries)
        self.assertEqual(queries[-1], {"author": "Lee Child"})

    def test_a_raw_hit_does_not_stop_the_variants_being_asked(self):
        raw = [patterson(f"B0RAW0000{i}", "Private 20 - Private Dublin") for i in range(3)]
        fake = StrictCatalog([*raw, self.DUBLIN])
        self.run_match(book_unit("Private 20 - Private Dublin", "James Patterson"), fake)
        self.assertIn({"title": "Private Dublin"}, fake.calls)

    def test_the_keyword_fallback_runs_only_when_every_title_query_is_empty(self):
        fake = StrictCatalog([], keywords_find=[self.DUBLIN])
        m = self.run_match(book_unit("Private 20 - Private Dublin"), fake)
        self.assertEqual(fake.calls[-1], {"keywords": "Private Dublin"})
        self.assertEqual(m.asin, "B00DUBLIN1")
        self.assertLessEqual(len(fake.calls), matching.MAX_QUERIES)

        fake = StrictCatalog([self.DUBLIN], keywords_find=[self.DUBLIN])
        self.run_match(book_unit("Private Dublin"), fake)
        self.assertFalse(any("keywords" in c for c in fake.calls))

    def test_the_query_set_is_capped(self):
        queries, _n, _cleaned = matching.search_queries(
            "Private 20 - Private Dublin [CA] (Unabridged) chapterized", "James Patterson")
        self.assertLessEqual(len(queries), matching.MAX_QUERIES - 1)  # one slot for the keyword fallback
        self.assertEqual(queries[-1], {"author": "James Patterson"})


class TestOrientation(unittest.TestCase):
    PAST_TENSE = Product(asin="B07FPM4J8V", title="Past Tense", authors=["Lee Child"], hours=13.0,
                         language="english")

    def test_the_catalog_decides_which_side_is_the_author(self):
        fake = StrictCatalog([self.PAST_TENSE])
        unit = book_unit("Lee Child", "Past Tense", alt_author_guess="Lee Child", alt_title_guess="Past Tense")
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(unit)
        self.assertEqual(m.asin, "B07FPM4J8V")
        self.assertEqual((m.query_title, m.query_author), ("Past Tense", "Lee Child"))

    def test_a_clear_reading_asks_no_second_set(self):
        fake = StrictCatalog([self.PAST_TENSE])
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(book_unit("Past Tense", "Lee Child"))
        self.assertEqual(m.asin, "B07FPM4J8V")
        self.assertFalse(any(c.get("author") == "Past Tense" for c in fake.calls))

    def test_a_confident_primary_reading_issues_no_swapped_queries(self):
        stone = Product(asin="B0STONE001", title="Stone Cold", authors=["David Baldacci"], hours=11.0,
                        language="english")
        fake = StrictCatalog([stone])
        unit = book_unit("Stone Cold", "David Baldacci",
                         alt_author_guess="Stone Cold", alt_title_guess="David Baldacci")
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(unit)
        self.assertEqual(m.asin, "B0STONE001")
        self.assertGreaterEqual(m.score, matching.ARBITRATE_BELOW)
        swapped = [c for c in fake.calls if c.get("title") == "David Baldacci" or c.get("author") == "Stone Cold"]
        self.assertEqual(swapped, [])
        solo = StrictCatalog([stone])
        with mock.patch.object(catalog, "search", solo), mock.patch.object(catalog, "book", lambda asin: {}):
            matching.match_unit(book_unit("Stone Cold", "David Baldacci"))
        self.assertEqual(fake.calls, solo.calls)  # exactly the primary pass, nothing more

    def test_a_weak_primary_reading_does_issue_swapped_queries(self):
        fake = StrictCatalog([self.PAST_TENSE])
        unit = book_unit("Lee Child", "Past Tense", alt_author_guess="Lee Child", alt_title_guess="Past Tense")
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(unit)
        self.assertTrue(any(c.get("title") == "Past Tense" for c in fake.calls))
        self.assertEqual((m.asin, m.query_title, m.query_author), ("B07FPM4J8V", "Past Tense", "Lee Child"))
        self.assertEqual(m.product.title, "Past Tense")
        self.assertEqual(m.product.authors, ["Lee Child"])

    def test_no_alt_guess_means_no_second_pass_even_when_weak(self):
        fake = StrictCatalog([])
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            matching.match_unit(book_unit("Lee Child", "Past Tense"))
        self.assertFalse(any(c.get("title") == "Past Tense" for c in fake.calls))

    def test_a_tie_keeps_the_original_reading(self):
        fake = StrictCatalog([])
        unit = book_unit("Past Tense", "Lee Child", alt_author_guess="Past Tense", alt_title_guess="Lee Child")
        with mock.patch.object(catalog, "search", fake), mock.patch.object(catalog, "book", lambda asin: {}):
            m = matching.match_unit(unit)
        self.assertEqual((m.query_title, m.query_author), ("Past Tense", "Lee Child"))


class TestAsinHint(unittest.TestCase):
    def book(self, title):
        return {"asin": "B0CLGF5CFH", "title": title, "authors": [{"name": "Lee Child"}],
                "runtimeLengthMin": 600, "language": "english"}

    def run_hint(self, title_on_record):
        fake = StrictCatalog([])
        with mock.patch.object(catalog, "search", fake), \
                mock.patch.object(catalog, "book", lambda asin: self.book(title_on_record)):
            return matching.match_unit(book_unit("In Too Deep", asin_hint="B0CLGF5CFH"))

    def test_a_hint_whose_title_fails_the_floor_is_ignored(self):
        m = self.run_hint("A Completely Different Book")
        self.assertIsNone(m.product)
        self.assertEqual(m.confidence, "NONE")

    def test_a_hint_whose_title_passes_is_a_graded_candidate_not_a_pin(self):
        m = self.run_hint("In Too Deep")
        self.assertEqual(m.asin, "B0CLGF5CFH")
        self.assertEqual((m.score, m.title_sim), (1.0, 1.0))
        self.assertFalse(m.asin_override)
        self.assertEqual(m.confidence, grade(m))
        self.assertEqual(m.confidence, "MEDIUM")  # title-only: never HIGH from a name alone


if __name__ == "__main__":
    unittest.main()

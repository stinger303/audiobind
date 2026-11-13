"""0.16.0-dev3: torrent-junk names are cleaned before ingest title/author extraction.

Observed live: ``01 Convergence [CA]`` parsed to title "Convergence CA", author empty, and the
catalog search found nothing. The cleaner is ingest-only: library folders were named by hand.
"""

import unittest

from audiobind.config import CONFIG
from audiobind.units import clean_torrent_name, discover_ingest_units, discover_units

try:
    from test_ingest_discovery import IngestConfigCase, touch
except ImportError:
    from tests.test_ingest_discovery import IngestConfigCase, touch


class TestCleanTorrentName(unittest.TestCase):
    CASES = {
        # the live failure
        "01 Convergence [CA]": "Convergence",
        # numbers that ARE the title survive (the library's SEQ_RE rule)
        "1984": "1984",
        "2001 A Space Odyssey": "2001 A Space Odyssey",
        # tags in brackets and braces go, contents and all
        "The Hobbit [MAM v5] {ASIN:XYZ}": "The Hobbit",
        "[MAM] The Hobbit": "The Hobbit",
        # Unabridged/Abridged is never part of an Audible title
        "Book 2 - Title (Unabridged)": "Title",
        "Title (Abridged)": "Title",
        "Title Unabridged": "Title",
        "03. Andy Weir - Artemis [Unabridged]": "Andy Weir - Artemis",
        "Book 1: Dune": "Dune",
        "001 - Dune": "Dune",
        # whitespace collapses
        "  The   Martian   [MAM]  ": "The Martian",
        # nothing but tags: the name is kept rather than emptied
        "[MAM] {B00}": "[MAM] {B00}",
        # an author - title name keeps its separator for the parser
        "Brandon Sanderson - The Way of Kings [MAM v5] {B003ZWFO7E}": "Brandon Sanderson - The Way of Kings",
    }

    def test_cases(self):
        for raw, cleaned in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_torrent_name(raw), cleaned)

    def test_a_number_without_a_following_letter_survives(self):
        self.assertEqual(clean_torrent_name("11-22-63"), "11-22-63")
        self.assertEqual(clean_torrent_name("2312"), "2312")

    def test_empty_input(self):
        self.assertEqual(clean_torrent_name(""), "")


class TestIngestGuessesUseTheCleaner(IngestConfigCase):
    def units(self):
        return {u.rel_path: u for u in discover_ingest_units(self.ingest_root, CONFIG.ignore_globs)}

    def test_the_live_folder_parses_to_its_real_title(self):
        touch(str(self.ingest_root / "01 Convergence [CA]" / "01.mp3"))
        unit = self.units()["01 Convergence [CA]"]
        self.assertEqual(unit.title_guess, "Convergence")
        self.assertEqual(unit.author_guess, "")
        # the key and the path on disk are the raw torrent name - only the guesses are cleaned
        self.assertEqual(unit.state_key, "ingest:01 Convergence [CA]")

    def test_numeric_titles_survive_in_ingest(self):
        touch(str(self.ingest_root / "1984" / "01.mp3"))
        touch(str(self.ingest_root / "2001 A Space Odyssey [MAM]" / "01.mp3"))
        units = self.units()
        self.assertEqual(units["1984"].title_guess, "1984")
        self.assertEqual(units["2001 A Space Odyssey [MAM]"].title_guess, "2001 A Space Odyssey")

    def test_single_file_names_are_cleaned_too(self):
        touch(str(self.ingest_root / "The Hobbit [MAM v5] {ASIN:XYZ}.m4b"))
        touch(str(self.ingest_root / "Book 2 - Title (Unabridged).mp3"))
        units = self.units()
        self.assertEqual(units["The Hobbit [MAM v5] {ASIN:XYZ}.m4b"].title_guess, "The Hobbit")
        self.assertEqual(units["Book 2 - Title (Unabridged).mp3"].title_guess, "Title")

    def test_author_extraction_is_unchanged(self):
        touch(str(self.ingest_root / "03. Andy Weir - Artemis [Unabridged] {MAM}" / "01.mp3"))
        unit = self.units()["03. Andy Weir - Artemis [Unabridged] {MAM}"]
        self.assertEqual(unit.author_guess, "Andy Weir")
        self.assertEqual(unit.title_guess, "Artemis")

    def test_library_units_are_not_cleaned(self):
        """Regression: the cleaner is ingest-only. The same names in the library parse as before."""
        lib = CONFIG.library_root
        touch(str(lib / "01 Convergence [CA]" / "01.mp3"))
        touch(str(lib / "The Hobbit [MAM v5]" / "01.mp3"))
        units = {u.rel_path: u for u in discover_units(lib, CONFIG.ignore_globs)}
        # the bracket contents stay in a library guess, exactly as in 0.16.0-dev2
        self.assertEqual(units["01 Convergence [CA]"].title_guess, "Convergence CA")
        self.assertEqual(units["The Hobbit [MAM v5]"].title_guess, "The Hobbit MAM v5")


if __name__ == "__main__":
    unittest.main()

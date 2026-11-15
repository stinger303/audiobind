"""v0.14.4: the series index can never steer the output outside OUTPUT_ROOT.

0.19.0-dev14: the layout is Audiobookshelf's - every author joined with " & ", a series book
always under <author>/<series>/, and the sequence used as given (no zero padding)."""

import pathlib
import tempfile
import unittest

from audiobind import library
from audiobind.config import CONFIG


class TestOutputContainment(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = CONFIG.output_root
        CONFIG.output_root = pathlib.Path(self.dir.name) / "output"

    def tearDown(self):
        CONFIG.output_root = self.saved
        self.dir.cleanup()

    def inside(self, path):
        root = CONFIG.output_root.resolve()
        return root in path.resolve().parents

    def test_traversal_in_series_index_stays_inside(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "../../../../tmp/x")
        self.assertTrue(self.inside(path), path)
        self.assertNotIn("..", path.relative_to(CONFIG.output_root).parts)

    def test_slash_in_catalog_position_is_one_component(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "1/2")
        rel = path.relative_to(CONFIG.output_root).parts
        self.assertEqual(len(rel), 4)  # author / series / "NN - title" / file
        self.assertTrue(rel[2].endswith(" - Book"))

    def test_bare_dotdot_sequence_is_dropped(self):
        # the sequence goes; the book still sits in its series folder
        path = library.m4b_path("Saga", ["Author"], "Book", "..")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Saga" / "Book" / "Book.m4b")

    def test_normal_sequence_is_unchanged(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "3")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Saga" / "3 - Book" / "Book.m4b")
        path = library.m4b_path("Saga", ["Author"], "Book", "2.5")
        self.assertEqual(path.parent.name, "2.5 - Book")

    def test_sequence_is_not_zero_padded(self):
        self.assertEqual(library.m4b_path("Saga", ["Author"], "Book", "1").parent.name, "1 - Book")
        self.assertEqual(library.m4b_path("Saga", ["Author"], "Book", "12").parent.name, "12 - Book")
        # a sequence written with a leading zero is kept exactly as the catalog gave it
        self.assertEqual(library.m4b_path("Saga", ["Author"], "Book", "03").parent.name, "03 - Book")

    def test_series_without_sequence_stays_in_the_series_folder(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Saga" / "Book" / "Book.m4b")

    def test_no_series_is_a_standalone_book(self):
        path = library.m4b_path("", ["Author"], "Book", "4")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Book" / "Book.m4b")
        path = library.m4b_path("   ", ["Author"], "Book", "")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Book" / "Book.m4b")

    def test_every_author_is_joined_with_an_ampersand(self):
        path = library.m4b_path("", ["Terry Pratchett", "Neil Gaiman"], "Good Omens")
        self.assertEqual(path, CONFIG.output_root / "Terry Pratchett & Neil Gaiman" / "Good Omens" / "Good Omens.m4b")
        # a "A; B" entry is two authors, and a repeated name counts once
        path = library.m4b_path("", ["A. One; B. Two", "A. One"], "Book")
        self.assertEqual(path.relative_to(CONFIG.output_root).parts[0], "A. One & B. Two")

    def test_no_author_is_unknown(self):
        self.assertEqual(library.m4b_path("", [], "Book").relative_to(CONFIG.output_root).parts[0], "Unknown")
        self.assertEqual(library.m4b_path("", ["", " "], "Book").relative_to(CONFIG.output_root).parts[0], "Unknown")

    def test_author_names_are_sanitised(self):
        path = library.m4b_path("", ["AC/DC", "../../etc"], "Book")
        rel = path.relative_to(CONFIG.output_root).parts
        self.assertEqual(len(rel), 3)
        self.assertNotIn("..", rel)
        self.assertTrue(self.inside(path))

    def test_mp3_file_name(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "1", ext="mp3")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Saga" / "1 - Book" / "Book.mp3")

    def test_series_symlink_escape_is_refused(self):
        outside = pathlib.Path(self.dir.name) / "elsewhere"
        outside.mkdir()
        (CONFIG.output_root / "Author").mkdir(parents=True)
        (CONFIG.output_root / "Author" / "Saga").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            library.m4b_path("Saga", ["Author"], "Book", "1")

    def test_containment_check_rejects_a_symlink_escape(self):
        outside = pathlib.Path(self.dir.name) / "elsewhere"
        outside.mkdir()
        CONFIG.output_root.mkdir()
        (CONFIG.output_root / "Author").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            library.m4b_path("", ["Author"], "Book")


if __name__ == "__main__":
    unittest.main()

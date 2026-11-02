import os
import tempfile
import unittest

from audiobind.units import (author_like, discover_units, group_key, looks_like_collection,
                             split_author_title)


class TestGroupKey(unittest.TestCase):
    def test_camel_case_parts_collapse(self):
        self.assertEqual(group_key("StoneColdUnabridgedPart1"), group_key("StoneColdUnabridgedPart2"))

    def test_pure_marker_keys_empty_so_caller_uses_parent(self):
        self.assertEqual(group_key("Disc1"), "")
        self.assertEqual(group_key("Part 02"), "")

    def test_distinct_books_stay_distinct(self):
        self.assertNotEqual(group_key("The Enemy"), group_key("Killing Floor"))


class TestSplitAuthorTitle(unittest.TestCase):
    def test_author_then_title(self):
        self.assertEqual(split_author_title("David Baldacci - Stone Cold"), ("David Baldacci", "Stone Cold"))

    def test_title_then_author_is_corrected(self):
        self.assertEqual(split_author_title("Influx - Daniel Suarez"), ("Daniel Suarez", "Influx"))
        self.assertEqual(split_author_title("The Easy Way To Stop Smoking - Allen Carr"),
                         ("Allen Carr", "The Easy Way To Stop Smoking"))

    def test_by_construction(self):
        self.assertEqual(split_author_title("The Martian by Andy Weir"), ("Andy Weir", "The Martian"))

    def test_bare_title(self):
        self.assertEqual(split_author_title("World War Z"), (None, "World War Z"))

    def test_leading_sequence_number_is_dropped_but_decimal_kept_in_number(self):
        self.assertEqual(split_author_title("02 Die Trying"), (None, "Die Trying"))
        self.assertEqual(split_author_title("15.5 Second Son"), (None, "Second Son"))
        self.assertEqual(split_author_title("14 61 Hours"), (None, "14 61 Hours"))

    def test_numeric_titles_survive(self):
        self.assertEqual(split_author_title("1984"), (None, "1984"))
        self.assertEqual(split_author_title("2001 A Space Odyssey"), (None, "2001 A Space Odyssey"))

    def test_abbreviation_is_not_an_author(self):
        self.assertGreater(author_like("Michael Lewis"), author_like("Private L.A"))


class TestDiscovery(unittest.TestCase):
    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()

    def test_disc_folders_form_one_book(self):
        with tempfile.TemporaryDirectory() as root:
            for disc in (1, 2, 3):
                self._touch(os.path.join(root, "George Orwell - 1984", f"Disc{disc}", "track1.mp3"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 1)
            self.assertEqual(len(units[0].parts), 3)
            self.assertEqual(units[0].author_guess, "George Orwell")

    def test_separate_books_stay_separate(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, "Jack Reacher Series", "01 Killing Floor", "a.mp3"))
            self._touch(os.path.join(root, "Jack Reacher Series", "02 Die Trying", "a.mp3"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 2)
            self.assertEqual(units[0].title_guess, "Killing Floor")

    def test_loose_file_is_a_unit(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, "Nora Roberts - The Guardians.m4b"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0].author_guess, "Nora Roberts")

    def test_hidden_and_system_dirs_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, ".bookfix-work", "x", "a.mp3"))
            self._touch(os.path.join(root, "@eaDir", "a.mp3"))
            self.assertEqual(discover_units(root, [".*", "@eaDir"]), [])


if __name__ == "__main__":
    unittest.main()

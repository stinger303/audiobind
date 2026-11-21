"""CORE-05: sibling folders become one unit only on positive multi-part evidence.

group_key() strips every digit, so ``Author/Dune 1`` and ``Author/Dune 2`` used to key to "dune"
and become one unit: two books concatenated into one file. Now siblings are merged only when each
carries a real part marker (disc/part/cd + number; "Vol 2" is a book's own number), their names
agree once that marker is removed (other digits kept), their ordinals differ, and their sizes - the
runtime proxy discovery has - are comparable (CORE-05b; it was a 921.6 MB bar).
"""

import os
import tempfile
import unittest

from audiobind import units
from audiobind.units import discover_ingest_units, discover_units

MB = 1_000_000


class GroupingCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name

    def touch(self, *parts, size=5):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)  # sparse: a "book-length" file costs no disk
        return path

    def rels(self, found=None):
        found = discover_units(self.root, []) if found is None else found
        return sorted((u.rel_path, len(u.parts)) for u in found)


class NumberedTitlesTests(GroupingCase):
    def test_dune_1_and_dune_2_are_two_books(self):
        """(a) a number in a folder name is content, not a part marker."""
        self.touch("Author", "Dune 1", "01.mp3", size=400 * MB)
        self.touch("Author", "Dune 2", "01.mp3", size=420 * MB)
        self.assertEqual(self.rels(), [("Author/Dune 1", 1), ("Author/Dune 2", 1)])

    def test_numbered_titles_with_a_part_set_beside_them(self):
        self.touch("Author", "Dune 1", "01.mp3")
        self.touch("Author", "Dune 2", "01.mp3")
        self.touch("Author", "Messiah Part 1", "01.mp3")
        self.touch("Author", "Messiah Part 2", "01.mp3")
        self.assertEqual(self.rels(), [("Author#messiah", 2), ("Author/Dune 1", 1), ("Author/Dune 2", 1)])

    def test_two_numbered_books_each_in_parts_stay_two_books(self):
        for book in (1, 2):
            for part in (1, 2):
                self.touch("Author", f"Foo {book} Part {part}", "01.mp3")
        found = discover_units(self.root, [])
        self.assertEqual(self.rels(found), [("Author#foo 1", 2), ("Author#foo 2", 2)])
        for unit in found:
            names = [p.name for p in unit.parts]
            self.assertEqual(len(names), 2)
            self.assertEqual(len({n.split(" Part ")[0] for n in names}), 1, names)

    def test_a_marker_inside_a_word_is_not_a_marker(self):
        self.touch("Author", "Transcript 1", "01.mp3")
        self.touch("Author", "Transcript 2", "01.mp3")
        self.assertEqual(self.rels(), [("Author/Transcript 1", 1), ("Author/Transcript 2", 1)])

    def test_loose_numbered_files_at_the_root_are_separate_books(self):
        self.touch("Dune 1.m4b")
        self.touch("Dune 2.m4b")
        self.assertEqual(self.rels(), [("Dune 1.m4b", 1), ("Dune 2.m4b", 1)])

    def test_ingest_torrent_with_two_numbered_books(self):
        self.touch("Torrent", "Book 1", "01.mp3")
        self.touch("Torrent", "Book 2", "01.mp3")
        found = discover_ingest_units(self.root, [])
        self.assertEqual(self.rels(found), [("Torrent/Book 1", 1), ("Torrent/Book 2", 1)])


class GenuinePartsTests(GroupingCase):
    def test_disc_folders_are_one_unit(self):
        """(b) today's behaviour for a disc set is preserved."""
        for disc in (1, 2, 3):
            self.touch("Novel", f"Disc {disc}", "01.mp3", size=70 * MB)
        (unit,) = discover_units(self.root, [])
        self.assertEqual(unit.rel_path, "Novel")
        self.assertEqual([p.name for p in unit.parts], ["Disc 1", "Disc 2", "Disc 3"])
        self.assertEqual(unit.key, "")

    def test_cd_folders_under_a_book_are_one_unit(self):
        """(c) Author/Book/CD1 + CD2."""
        self.touch("Author", "Book", "CD1", "01.mp3")
        self.touch("Author", "Book", "CD2", "01.mp3")
        self.assertEqual(self.rels(), [("Author/Book", 2)])

    def test_named_parts_and_camel_case_parts(self):
        for n in (1, 2):
            self.touch("A", f"Foo Part {n}", "01.mp3")
            self.touch("B", f"StoneColdUnabridgedPart{n}", "01.mp3")
            self.touch("C", f"Bar - Pt. {n}", "01.mp3")
        self.assertEqual(self.rels(), [("A", 2), ("B", 2), ("C", 2)])

    def test_a_short_last_disc_and_mp3_cd_sized_discs_still_group(self):
        self.touch("Big", "Disc 1", "01.mp3", size=690 * MB)
        self.touch("Big", "Disc 2", "01.mp3", size=680 * MB)
        self.touch("Big", "Disc 3", "01.mp3", size=40 * MB)
        self.assertEqual(self.rels(), [("Big", 3)])

    def test_shared_parent_keys_are_unchanged(self):
        # rel_path is the state DB's key: a real part set keeps the key it always had
        for book in ("Foo", "Bar"):
            for part in (1, 2):
                self.touch("Author", f"{book} Part {part}", "01.mp3")
        self.touch("Author", "Baz", "01.mp3")
        self.assertEqual(self.rels(), [("Author#bar", 2), ("Author#foo", 2), ("Author/Baz", 1)])


class RuntimeCompatibilityTests(GroupingCase):
    def test_volumes_that_are_each_a_whole_book_are_separate(self):
        self.touch("Saga", "Saga Vol 1", "01.m4b", size=970 * MB)
        self.touch("Saga", "Saga Vol 2", "01.m4b", size=930 * MB)
        self.assertEqual(self.rels(), [("Saga/Saga Vol 1", 1), ("Saga/Saga Vol 2", 1)])

    def test_a_long_part_then_a_short_last_part_keeps_the_set(self):
        self.touch("Saga", "Saga Part 1", "01.m4b", size=970 * MB)
        self.touch("Saga", "Saga Part 2", "01.m4b", size=200 * MB)
        self.assertEqual(self.rels(), [("Saga", 2)])


class CompatibilityNotSizeTests(GroupingCase):
    """CORE-05b: the bar was "every member under 921.6 MB" - a size coin-flip on both sides."""

    def test_a_genuine_two_part_book_of_1_gb_parts_is_one_unit(self):
        """About seven hours a part at 320 kbps: a fourteen-hour book, not two books."""
        self.touch("Author", "Book", "Part 1", "01.mp3", size=1000 * MB)
        self.touch("Author", "Book", "Part 2", "01.mp3", size=1000 * MB)
        self.assertEqual(self.rels(), [("Author/Book", 2)])

    def test_two_volumes_of_ordinary_size_are_two_books(self):
        """Saga Vol 1 and Vol 2: five hours each at 128 kbps, identical runtimes, two books."""
        self.touch("Author", "Saga Vol 1", "01.m4b", size=288 * MB)
        self.touch("Author", "Saga Vol 2", "01.m4b", size=288 * MB)
        self.assertEqual(self.rels(), [("Author/Saga Vol 1", 1), ("Author/Saga Vol 2", 1)])

    def test_volumes_each_in_parts_stay_apart(self):
        for vol in (1, 2):
            for part in (1, 2):
                self.touch("Author", f"Saga Vol {vol} Part {part}", "01.mp3", size=288 * MB)
        self.assertEqual(self.rels(), [("Author#saga vol 1", 2), ("Author#saga vol 2", 2)])

    def test_disc_1_and_disc_01_are_not_successive_parts(self):
        """The same ordinal twice is two copies of disc one, never discs one and two."""
        self.touch("Author", "Title Disc 1", "01.mp3", size=288 * MB)
        self.touch("Author", "Title Disc 01", "01.mp3", size=288 * MB)
        found = discover_units(self.root, [])
        self.assertFalse([u for u in found if len(u.parts) > 1], [(u.rel_path, len(u.parts)) for u in found])
        self.assertEqual(self.rels(found), [("Author/Title Disc 01", 1), ("Author/Title Disc 1", 1)])

    def test_a_repeated_ordinal_in_a_longer_set_is_not_guessed_at(self):
        for name in ("Disc 1", "Disc 2", "Disc 02"):
            self.touch("Novel", name, "01.mp3", size=70 * MB)
        self.assertEqual(self.rels(), [("Novel/Disc 02", 1), ("Novel/Disc 1", 1), ("Novel/Disc 2", 1)])

    def test_a_title_beside_its_numbered_sequel_is_two_books(self):
        self.touch("Author", "Book", "01.mp3", size=300 * MB)
        self.touch("Author", "Book 2", "01.mp3", size=300 * MB)
        self.assertEqual(self.rels(), [("Author/Book", 1), ("Author/Book 2", 1)])

    def test_parts_of_one_book_have_comparable_lengths(self):
        """A 100 MB "part 1" beside a 700 MB "part 2" is not one book split in two."""
        self.touch("Author", "Foo Part 1", "01.mp3", size=100 * MB)
        self.touch("Author", "Foo Part 2", "01.mp3", size=700 * MB)
        self.assertEqual(self.rels(), [("Author/Foo Part 1", 1), ("Author/Foo Part 2", 1)])

    def test_a_middle_disc_far_off_the_others_breaks_the_set(self):
        self.touch("Novel", "Disc 1", "01.mp3", size=700 * MB)
        self.touch("Novel", "Disc 2", "01.mp3", size=100 * MB)
        self.touch("Novel", "Disc 3", "01.mp3", size=700 * MB)
        self.assertEqual(self.rels(), [("Novel/Disc 1", 1), ("Novel/Disc 2", 1), ("Novel/Disc 3", 1)])

    def test_a_marked_set_of_any_total_size_groups(self):
        for disc in range(1, 21):
            self.touch("Epic", f"CD{disc}", "01.mp3", size=(70 if disc < 20 else 30) * MB)
        self.assertEqual(self.rels(), [("Epic", 20)])


if __name__ == "__main__":
    unittest.main()

"""CORE-05: sibling folders become one unit only on positive multi-part evidence.

group_key() strips every digit, so ``Author/Dune 1`` and ``Author/Dune 2`` used to key to "dune"
and become one unit: two books concatenated into one file. Now siblings are merged only when each
carries a real part marker (disc/part/cd/vol + number), their names agree once that marker is
removed (other digits kept), and their sizes - the runtime proxy discovery has - do not say each
one is a whole book.
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
        whole = units.WHOLE_BOOK_BYTES
        self.touch("Saga", "Saga Vol 1", "01.m4b", size=whole + 50 * MB)
        self.touch("Saga", "Saga Vol 2", "01.m4b", size=whole + 10 * MB)
        self.assertEqual(self.rels(), [("Saga/Saga Vol 1", 1), ("Saga/Saga Vol 2", 1)])

    def test_one_long_part_beside_ordinary_ones_keeps_the_set(self):
        whole = units.WHOLE_BOOK_BYTES
        self.touch("Saga", "Saga Vol 1", "01.m4b", size=whole + 50 * MB)
        self.touch("Saga", "Saga Vol 2", "01.m4b", size=200 * MB)
        self.assertEqual(self.rels(), [("Saga", 2)])


if __name__ == "__main__":
    unittest.main()

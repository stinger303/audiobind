"""v0.14.4: multi-part units that share a parent folder.

Before: ``Author/{Foo Part 1, Foo Part 2, Bar Part 1, Bar Part 2, Baz}`` gave Foo and Bar
the same rel_path ("Author"), so they overwrote each other's row, and Delete on either one
ran rmtree(/library/Author) - removing Foo, Bar *and* Baz.
"""

import os
import tempfile
import unittest

from audiobind.state import UnitState
from audiobind.units import discover_units, owned_paths

try:
    from live import LiveServerCase  # unittest discover -s tests
except ImportError:  # python -m unittest tests.test_shared_parent
    from tests.live import LiveServerCase


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"audio")


def shared_author(root):
    for book in ("Foo", "Bar"):
        for part in (1, 2):
            touch(os.path.join(root, "Author", f"{book} Part {part}", "01.mp3"))
    touch(os.path.join(root, "Author", "Baz", "01.mp3"))


class TestSharedParentKeys(unittest.TestCase):
    def test_two_series_in_one_author_folder_get_unique_keys(self):
        with tempfile.TemporaryDirectory() as root:
            shared_author(root)
            units = discover_units(root, [])
            rels = sorted(u.rel_path for u in units)
            self.assertEqual(len(rels), 3)
            self.assertEqual(len(set(rels)), 3)
            self.assertIn("Author/Baz", rels)
            self.assertIn("Author#foo", rels)
            self.assertIn("Author#bar", rels)

    def test_shared_units_guess_their_own_title(self):
        with tempfile.TemporaryDirectory() as root:
            shared_author(root)
            by_rel = {u.rel_path: u for u in discover_units(root, [])}
            self.assertEqual(by_rel["Author#foo"].title_guess, "Foo")
            self.assertEqual(by_rel["Author#bar"].title_guess, "Bar")
            self.assertEqual(len(by_rel["Author#foo"].parts), 2)

    def test_shared_unit_owns_only_its_part_folders(self):
        with tempfile.TemporaryDirectory() as root:
            shared_author(root)
            foo = next(u for u in discover_units(root, []) if u.rel_path == "Author#foo")
            owned = sorted(os.path.basename(p) for p in owned_paths(foo))
            self.assertEqual(owned, ["Foo Part 1", "Foo Part 2"])

    def test_a_lone_disc_set_still_owns_its_wrapper_folder(self):
        with tempfile.TemporaryDirectory() as root:
            for disc in (1, 2):
                touch(os.path.join(root, "Orwell - 1984", f"Disc {disc}", "01.mp3"))
            (unit,) = discover_units(root, [])
            self.assertEqual(unit.rel_path, "Orwell - 1984")
            self.assertEqual(owned_paths(unit), [os.path.join(root, "Orwell - 1984")])

    def test_parent_with_its_own_audio_does_not_collide_with_its_disc_set(self):
        with tempfile.TemporaryDirectory() as root:
            touch(os.path.join(root, "Book", "intro.mp3"))
            for disc in (1, 2):
                touch(os.path.join(root, "Book", f"Disc {disc}", "01.mp3"))
            rels = sorted(u.rel_path for u in discover_units(root, []))
            self.assertEqual(rels, ["Book", "Book#parts"])

    def test_rel_paths_stay_unique_against_a_real_hash_folder(self):
        with tempfile.TemporaryDirectory() as root:
            shared_author(root)
            touch(os.path.join(root, "Author#foo", "01.mp3"))  # a real folder with that name
            rels = [u.rel_path for u in discover_units(root, [])]
            self.assertEqual(len(rels), len(set(rels)))


class TestSharedParentDelete(LiveServerCase):
    def test_rows_do_not_overwrite_each_other(self):
        shared_author(str(self.lib))
        for unit in discover_units(self.lib, []):
            self.state.save(UnitState(path=unit.rel_path, status="review", title=unit.title_guess))
        self.assertEqual(self.state.get("Author#foo").title, "Foo")
        self.assertEqual(self.state.get("Author#bar").title, "Bar")

    def test_deleting_one_series_leaves_the_siblings_intact(self):
        shared_author(str(self.lib))
        self.state.save(UnitState(path="Author#foo", status="review"))
        self.state.save(UnitState(path="Author#bar", status="review"))
        status, body = self.post({"action": "delete", "path": "Author#foo", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["source_removed"])
        author = self.lib / "Author"
        self.assertFalse((author / "Foo Part 1").exists())
        self.assertFalse((author / "Foo Part 2").exists())
        self.assertTrue((author / "Bar Part 1" / "01.mp3").exists())
        self.assertTrue((author / "Bar Part 2" / "01.mp3").exists())
        self.assertTrue((author / "Baz" / "01.mp3").exists())
        self.assertIsNone(self.state.get("Author#foo"))
        self.assertIsNotNone(self.state.get("Author#bar"))

    def test_a_legacy_shared_parent_row_never_rmtrees_the_author_folder(self):
        # rows written by <=0.14.3 used "Author" for both series; that path is no longer a
        # unit on disk, so delete must forget the row without touching the folder
        shared_author(str(self.lib))
        self.state.save(UnitState(path="Author", status="review"))
        status, body = self.post({"action": "delete", "path": "Author", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["source_removed"])
        self.assertTrue(body["source_kept"])
        for name in ("Foo Part 1", "Foo Part 2", "Bar Part 1", "Bar Part 2", "Baz"):
            self.assertTrue((self.lib / "Author" / name / "01.mp3").exists(), name)

    def test_refuses_when_the_folder_holds_another_books_audio(self):
        touch(str(self.lib / "Book" / "intro.mp3"))
        for disc in (1, 2):
            touch(str(self.lib / "Book" / f"Disc {disc}" / "01.mp3"))
        self.state.save(UnitState(path="Book", status="review"))
        status, body = self.post({"action": "delete", "path": "Book", "confirm": True})
        self.assertEqual(status, 409)
        self.assertIn("other books", body["error"])
        self.assertTrue((self.lib / "Book" / "Disc 1" / "01.mp3").exists())
        self.assertIsNotNone(self.state.get("Book"))


if __name__ == "__main__":
    unittest.main()

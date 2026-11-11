"""v0.15.1: Delete never takes another row's published book, and re-verifies every path
immediately before removing it (TOCTOU)."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import web
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def touch(path, data=b"audio"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


class RemovalUnsafeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve() / "lib"
        (self.root / "Author" / "Book").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_folder_is_safe(self):
        target, kind = web.plan_target(self.root / "Author" / "Book")
        self.assertEqual(kind, "dir")
        self.assertEqual(web.removal_unsafe(target, self.root, kind), "")

    def test_root_itself_is_refused(self):
        self.assertTrue(web.removal_unsafe(self.root, self.root, "dir"))

    def test_outside_root_is_refused(self):
        self.assertIn("inside", web.removal_unsafe(self.root.parent / "elsewhere", self.root, "dir"))

    def test_parent_swapped_for_a_link_is_refused(self):
        target, kind = web.plan_target(self.root / "Author" / "Book")
        outside = self.root.parent / "outside"
        (outside / "Book").mkdir(parents=True)
        os.rename(self.root / "Author", self.root / "Author.moved")
        os.symlink(outside, self.root / "Author")
        self.assertIn("link", web.removal_unsafe(target, self.root, kind))

    def test_folder_swapped_for_a_link_is_refused(self):
        target, kind = web.plan_target(self.root / "Author" / "Book")
        os.rename(target, self.root / "Author" / "Book.moved")
        os.symlink(self.root.parent, target)
        self.assertIn("replaced by a link", web.removal_unsafe(target, self.root, kind))

    def test_planned_link_may_be_unlinked(self):
        link = self.root / "Author" / "Link"
        os.symlink(self.root.parent, link)
        target, kind = web.plan_target(link)
        self.assertEqual(kind, "link")
        self.assertEqual(target, link)  # the leaf is not followed
        self.assertEqual(web.removal_unsafe(target, self.root, kind), "")


class SharedOutputTests(LiveServerCase):
    def _book(self, rel, status, output):
        touch(str(self.lib / rel / "01.mp3"))
        self.state.save(UnitState(path=rel, status=status, output=str(output)))

    def test_output_kept_when_another_row_uses_it(self):
        published = self.out / "Author" / "Title" / "Title.m4b"
        touch(str(published), b"m4b")
        self._book("Author/Title", "converted", published)
        self._book("Author/Title (re-rip)", "ready", published)  # "already published"
        status, body = self.post({"action": "delete", "path": "Author/Title (re-rip)", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "kept")
        self.assertEqual(body["output_kept_reason"], "also used by 1 other book(s)")
        self.assertTrue(published.exists())
        self.assertFalse((self.lib / "Author" / "Title (re-rip)").exists())  # source deleted as normal
        self.assertIsNone(self.state.get("Author/Title (re-rip)"))
        self.assertIsNotNone(self.state.get("Author/Title"))

    def test_counts_every_other_row(self):
        published = self.out / "Author" / "Title" / "Title.m4b"
        touch(str(published), b"m4b")
        self._book("Author/Title", "converted", published)
        self._book("Author/Title 2", "ready", published)
        self._book("Author/Title 3", "ready", self.out / "Author" / "Title" / "Other.m4b")
        status, body = self.post({"action": "delete", "path": "Author/Title", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output_kept_reason"], "also used by 2 other book(s)")
        self.assertTrue(published.exists())

    def test_unshared_output_is_still_removed(self):
        published = self.out / "Author" / "Solo" / "Solo.m4b"
        touch(str(published), b"m4b")
        self._book("Author/Solo", "converted", published)
        self._book("Author/Other", "converted", self.out / "Author" / "Other" / "Other.m4b")
        status, body = self.post({"action": "delete", "path": "Author/Solo", "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "removed")
        self.assertFalse(published.parent.exists())


class ToctouLiveTests(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.outside = self.root / "outside"
        touch(str(self.outside / "Book" / "precious.mp3"))
        touch(str(self.lib / "Author" / "Book" / "01.mp3"))
        self.state.save(UnitState(path="Author/Book", status="review"))
        self.real_files_under = web.files_under

    def _swap_then(self, swap):
        def side_effect(paths):
            result = self.real_files_under(paths)
            swap()
            return result

        return mock.patch.object(web, "files_under", side_effect=side_effect)

    def test_parent_swapped_mid_delete_aborts_and_keeps_row(self):
        def swap():
            os.rename(self.lib / "Author", self.lib / "Author.moved")
            os.symlink(self.outside, self.lib / "Author")

        with self._swap_then(swap):
            status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("changed on disk", body["error"])
        self.assertTrue((self.outside / "Book" / "precious.mp3").exists())
        self.assertIsNotNone(self.state.get("Author/Book"))

    def test_folder_swapped_for_link_mid_delete_aborts(self):
        def swap():
            os.rename(self.lib / "Author" / "Book", self.lib / "Author" / "Book.moved")
            os.symlink(self.outside / "Book", self.lib / "Author" / "Book")

        with self._swap_then(swap):
            status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertTrue((self.outside / "Book" / "precious.mp3").exists())
        self.assertIsNotNone(self.state.get("Author/Book"))

    def test_output_parent_swapped_mid_delete_aborts(self):
        published = self.out / "Author" / "Book" / "Book.m4b"
        touch(str(published), b"m4b")
        self.state.save(UnitState(path="Gone/Book", status="converted", output=str(published)))
        elsewhere = self.root / "elsewhere"
        touch(str(elsewhere / "Book" / "keep.m4b"), b"m4b")

        def sharers(state, rel, folder):
            os.rename(self.out / "Author", self.out / "Author.moved")
            os.symlink(elsewhere, self.out / "Author")
            return 0

        with mock.patch.object(web, "output_sharers", side_effect=sharers):
            status, body = self.post({"action": "delete", "path": "Gone/Book", "confirm": True})
        self.assertEqual(status, 409, body)
        self.assertIn("published folder changed", body["error"])
        self.assertTrue((elsewhere / "Book" / "keep.m4b").exists())
        self.assertIsNotNone(self.state.get("Gone/Book"))


if __name__ == "__main__":
    unittest.main()

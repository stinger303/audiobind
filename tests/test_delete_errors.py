"""v0.14.4: delete error responses are valid JSON, and a partial delete is never "kept"."""

import errno
import os
import unittest
from unittest import mock

from audiobind import web
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"audio")


class TestDeleteErrors(LiveServerCase):
    def test_error_text_with_quotes_and_backslashes_is_valid_json(self):
        touch(str(self.lib / "Author" / "Book" / "01.mp3"))
        self.state.save(UnitState(path="Author/Book", status="review"))
        nasty = OSError(errno.EIO, 'disk said "no" \\ twice')
        with mock.patch.object(web.shutil, "rmtree", side_effect=nasty):
            status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 500)
        self.assertIn('"no"', body["error"])
        self.assertIsNotNone(self.state.get("Author/Book"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_partial_delete_is_reported_and_the_row_is_kept(self):
        for part in (1, 2):
            touch(str(self.lib / "Author" / f"Foo Part {part}" / "01.mp3"))
        touch(str(self.lib / "Author" / "Bar" / "01.mp3"))
        out_book = self.out / "Author" / "Foo"
        out_book.mkdir(parents=True)
        (out_book / "Foo.m4b").write_text("m4b")
        self.state.save(UnitState(path="Author#foo", status="converted", output=str(out_book / "Foo.m4b")))
        locked = self.lib / "Author" / "Foo Part 2"
        locked.chmod(0o500)
        try:
            status, body = self.post({"action": "delete", "path": "Author#foo", "confirm": True})
        finally:
            locked.chmod(0o755)
        self.assertEqual(status, 500)
        self.assertTrue(body["source_partial"])
        self.assertIn("partly", body["error"])
        self.assertNotIn("source_kept", body)
        self.assertIsNotNone(self.state.get("Author#foo"))  # not forgotten
        self.assertTrue((out_book / "Foo.m4b").exists())  # output untouched

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_permission_denied_is_reported_separately_from_read_only(self):
        src = self.lib / "Author" / "Book"
        touch(str(src / "01.mp3"))
        self.state.save(UnitState(path="Author/Book", status="review"))
        src.chmod(0o500)
        try:
            status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        finally:
            src.chmod(0o755)
        self.assertEqual(status, 200)
        self.assertTrue(body["source_kept"])
        self.assertIn("permission", body["source_kept_reason"])
        self.assertNotIn("read-only", body["source_kept_reason"])

    def test_read_only_mount_says_read_only(self):
        touch(str(self.lib / "Author" / "Book" / "01.mp3"))
        self.state.save(UnitState(path="Author/Book", status="review"))
        with mock.patch.object(web.shutil, "rmtree", side_effect=OSError(errno.EROFS, "Read-only file system")):
            status, body = self.post({"action": "delete", "path": "Author/Book", "confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["source_kept"])
        self.assertIn("read-only", body["source_kept_reason"])
        self.assertIsNone(self.state.get("Author/Book"))


if __name__ == "__main__":
    unittest.main()

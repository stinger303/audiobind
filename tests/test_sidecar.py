"""SEC-03: metadata.json must never be written through a symlink, nor outside OUTPUT_ROOT.

The audio paths were already contained (library.m4b_path); the sidecar beside them was not,
and Path.write_text follows a pre-existing symlink."""

import json
import os
import pathlib
import tempfile
import unittest

from audiobind import encode, pipeline
from audiobind.config import CONFIG
from audiobind.catalog import Product
from audiobind.matching import Match

BOOK = {"title": "Moneyball", "authors": [{"name": "Michael Lewis"}], "asin": "B005EHN61O"}
OUTSIDE_TEXT = "precious - not audiobind's\n"


class SidecarCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.dir.name)
        self.root = self.tmp / "output"
        self.book_dir = self.root / "Michael Lewis" / "Moneyball"
        self.book_dir.mkdir(parents=True)
        self.outside = self.tmp / "elsewhere.txt"
        self.outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
        self.match = Match(product=Product(asin="B005EHN61O", title="Moneyball"))
        self.saved_root = CONFIG.output_root
        CONFIG.output_root = self.root

    def tearDown(self):
        CONFIG.output_root = self.saved_root
        self.dir.cleanup()

    def leftovers(self):
        return [p.name for p in self.book_dir.iterdir() if p.name != "metadata.json" and p.suffix != ".m4b"]


class WriteAbsMetadataTests(SidecarCase):
    def test_normal_write(self):
        target = self.book_dir / "metadata.json"
        data = encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), data)
        self.assertEqual(data["title"], "Moneyball")
        self.assertEqual(self.leftovers(), [])  # no temp file left behind

    def test_existing_symlink_is_replaced_not_written_through(self):
        target = self.book_dir / "metadata.json"
        target.symlink_to(self.outside)
        encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), OUTSIDE_TEXT)  # untouched
        self.assertFalse(target.is_symlink())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["title"], "Moneyball")
        self.assertEqual(self.leftovers(), [])

    def test_permissions_follow_the_umask_like_before(self):
        target = self.book_dir / "metadata.json"
        encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(target.stat().st_mode & 0o777, 0o666 & ~mask)

    def with_umask(self, mask):
        old = os.umask(mask)
        self.addCleanup(os.umask, old)

    def test_a_rewrite_keeps_an_existing_sidecars_mode(self):
        # SEC-03b: the rename installs a new inode; it must carry the old file's mode
        self.with_umask(0o022)
        target = self.book_dir / "metadata.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o600)
        encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["title"], "Moneyball")
        self.assertEqual(self.leftovers(), [])

    def test_a_fresh_sidecar_still_follows_the_umask(self):
        self.with_umask(0o027)
        target = self.book_dir / "metadata.json"
        encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_a_replaced_symlink_does_not_lend_its_targets_mode(self):
        self.with_umask(0o022)
        os.chmod(self.outside, 0o600)
        target = self.book_dir / "metadata.json"
        target.symlink_to(self.outside)
        encode.write_abs_metadata(target, BOOK, self.match, [], 3600.0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)


class PipelineSidecarTests(SidecarCase):
    """pipeline._write_sidecar: what convert_unit calls after publishing."""

    def write(self, out_path):
        lines = []
        pipeline._write_sidecar(out_path, BOOK, self.match, [], 3600.0, log=lines.append)
        return lines

    def test_writes_beside_the_book(self):
        lines = self.write(self.book_dir / "Moneyball.m4b")
        self.assertEqual(lines, [])
        self.assertEqual(json.loads((self.book_dir / "metadata.json").read_text(encoding="utf-8"))["asin"],
                         "B005EHN61O")

    def test_symlinked_sidecar_is_skipped_and_its_target_untouched(self):
        (self.book_dir / "metadata.json").symlink_to(self.outside)
        lines = self.write(self.book_dir / "Moneyball.m4b")
        self.assertTrue(any("metadata.json" in line and "skipped" in line for line in lines), lines)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), OUTSIDE_TEXT)
        self.assertTrue((self.book_dir / "metadata.json").is_symlink())  # left exactly as found

    def test_folder_outside_the_output_root_is_skipped(self):
        escape = self.tmp / "escape"
        escape.mkdir()
        (self.root / "Linked").symlink_to(escape, target_is_directory=True)
        lines = self.write(self.root / "Linked" / "Moneyball.m4b")
        self.assertTrue(any("outside the output root" in line for line in lines), lines)
        self.assertFalse((escape / "metadata.json").exists())


if __name__ == "__main__":
    unittest.main()

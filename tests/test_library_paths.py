"""v0.14.4: the series index can never steer the output outside OUTPUT_ROOT."""

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
        path = library.m4b_path("Saga", ["Author"], "Book", "..")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Book" / "Book.m4b")

    def test_normal_sequence_is_unchanged(self):
        path = library.m4b_path("Saga", ["Author"], "Book", "3")
        self.assertEqual(path, CONFIG.output_root / "Author" / "Saga" / "03 - Book" / "Book.m4b")
        path = library.m4b_path("Saga", ["Author"], "Book", "2.5")
        self.assertEqual(path.parent.name, "2.5 - Book")

    def test_containment_check_rejects_a_symlink_escape(self):
        outside = pathlib.Path(self.dir.name) / "elsewhere"
        outside.mkdir()
        CONFIG.output_root.mkdir()
        (CONFIG.output_root / "Author").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            library.m4b_path("", ["Author"], "Book")


if __name__ == "__main__":
    unittest.main()

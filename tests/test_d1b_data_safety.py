"""D1b: the data-loss holes the CX-D1 cross-review found in D1 (CORE-02b/03b/04a/04b, WEBAPI-02b).

The theme: never remove or overwrite a file this process did not create.
"""

import os
import pathlib
import unittest

from audiobind import pipeline

try:
    from test_d1_data_safety import DeleteCase, PublishCase
    from test_ingest_pipeline import KEY
except ImportError:
    from tests.test_d1_data_safety import DeleteCase, PublishCase
    from tests.test_ingest_pipeline import KEY


def published_m4b() -> pathlib.Path:
    return pipeline.library.m4b_path("", ["Andy Weir"], "Project Hail Mary")


# --------------------------------------------------------------------------- CORE-02b
class Core02bRollbackRemovesOnlyWhatItCreatedTests(PublishCase):
    """A failed publish unlinks only the paths it placed - never a link or file it found."""

    def test_a_dangling_audio_link_survives_a_first_placement_failure(self):
        m4b = published_m4b()
        m4b.parent.mkdir(parents=True)
        target = m4b.parent / "moved-away.m4b"  # inside the output root, and no longer there
        os.symlink(target, m4b)
        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=lambda dest: dest == m4b)
        self.assertTrue(os.path.islink(m4b), "the pre-existing link was removed by the rollback")
        self.assertEqual(os.readlink(m4b), str(target))
        self.assertEqual(self.listing(m4b.parent), [m4b.name])  # no backup or new file left over

    def test_a_file_another_writer_made_after_the_snapshot_is_left_alone(self):
        m4b = published_m4b()
        mp3 = m4b.with_suffix(".mp3")

        def other_writer_then_fail(dest):
            if dest == mp3:
                mp3.write_bytes(b"other-writer")  # appears after the snapshot, before placement
                return True
            return False

        with self.assertRaises(OSError):
            self.convert(KEY, "first", fail_place=other_writer_then_fail)
        self.assertEqual(mp3.read_bytes(), b"other-writer", "another writer's file was unlinked")
        # what this call did place is still cleaned up
        self.assertFalse(m4b.exists())
        self.assertFalse((m4b.parent / "cover.jpg").exists())


if __name__ == "__main__":
    unittest.main()

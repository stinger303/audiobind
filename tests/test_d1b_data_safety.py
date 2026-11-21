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


# --------------------------------------------------------------------------- CORE-03b
class Core03bCoverBackedUpWhenOnlyAFormatIsAddedTests(PublishCase):
    """Adding a missing format writes cover.jpg too: the existing art must be backed up first."""

    def kept_m4b_with_cover(self):
        self.output_format = "M4B"
        m4b = self.convert(KEY, "first")
        cover = m4b.parent / "cover.jpg"
        cover.write_bytes(b"owner-cover")
        self.output_format = "MP3"  # only the missing format is requested; the M4B is not replaced
        return m4b, cover

    def test_a_failed_sidecar_leaves_the_existing_cover_byte_identical(self):
        m4b, cover = self.kept_m4b_with_cover()
        before = self.listing(m4b.parent)
        with self.assertRaises(OSError):
            self.convert(KEY, "second", fail_sidecar=True)
        self.assertEqual(cover.read_bytes(), b"owner-cover", "the rollback left the new cover over the old art")
        self.assertEqual(m4b.read_bytes(), b"first-m4b")
        self.assertEqual(self.listing(m4b.parent), before)  # the MP3 went, the backup came back

    def test_a_successful_add_keeps_the_previous_cover_as_a_backup(self):
        m4b, cover = self.kept_m4b_with_cover()
        self.convert(KEY, "second")
        self.assertEqual(m4b.with_suffix(".mp3").read_bytes(), b"second-mp3")
        self.assertEqual(cover.read_bytes(), b"second-cover")
        (backup,) = sorted(m4b.parent.glob("cover.jpg.bak-*"))
        self.assertEqual(backup.read_bytes(), b"owner-cover")


# --------------------------------------------------------------------------- CORE-04b
class Core04bEveryDestinationIsContainedTests(PublishCase):
    """A saved anchor is checked file by file: its siblings, cover and metadata must all land in a
    book folder inside the output root, or nothing is written."""

    def anchored_at(self, anchor: pathlib.Path) -> pathlib.Path:
        """A converted M4B-only book whose row points at ``anchor``, a symlink to the real M4B."""
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        real = pathlib.Path(self.state.get(KEY).output)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real, anchor)
        row = self.state.get(KEY)
        row.output = str(anchor)
        self.state.save(row)
        self.output_format = "M4B + MP3"  # the MP3 is missing beside the anchor
        return real

    def assert_refused(self, real: pathlib.Path):
        row = self.state.get(KEY)
        self.assertEqual(row.status, "failed", "the conversion was not refused")
        self.assertIn("outside", row.note)
        self.assertEqual(self.listing(real.parent), [real.name, "cover.jpg", "metadata.json"])

    def test_an_anchor_link_from_outside_the_root_writes_nothing_beside_it(self):
        outside = self.root / "outside"
        anchor = outside / "Project Hail Mary.m4b"
        real = self.anchored_at(anchor)
        self.run_process(KEY, "second", manual=True)
        self.assertEqual(self.listing(outside), [anchor.name], "files were published outside the output root")
        self.assert_refused(real)

    def test_an_anchor_link_directly_in_the_root_does_not_publish_into_the_root(self):
        out_root = pathlib.Path(pipeline.CONFIG.output_root)
        anchor = out_root / "Project Hail Mary.m4b"
        real = self.anchored_at(anchor)
        before = self.listing(out_root)
        self.run_process(KEY, "second", manual=True)
        self.assertEqual(self.listing(out_root), before, "files were published into the output root itself")
        self.assert_refused(real)


if __name__ == "__main__":
    unittest.main()

"""Existing destinations need format-specific proof for the requested edition."""
import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from tests import test_d1_data_safety as fixtures
from tests.test_ingest_pipeline import KEY, PHM


class EditionOwnership(fixtures.PublishCase):
    def test_rewritten_companions_refresh_when_audio_proves_the_edition(self):
        self.output_format = "M4B"
        self.run_process(KEY, "original")
        primary = Path(self.state.get(KEY).output)
        metadata = primary.with_name("metadata.json")
        metadata.write_bytes(b"edited by Audiobookshelf")
        primary.with_name("cover.jpg").write_bytes(b"edited cover")
        logs = []
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "updated", logs=logs, manual=True)
        self.assertEqual(self.state.get(KEY).status, "converted")
        self.assertTrue(primary.with_suffix(".mp3").exists())
        self.assertNotEqual(metadata.read_bytes(), b"edited by Audiobookshelf")
        self.assertEqual(primary.with_name("cover.jpg").read_bytes(), b"updated-cover")
        self.assertTrue(any("Refreshing metadata.json" in line for line in logs), logs)

    def test_companion_without_proven_audio_is_refused_before_encode(self):
        self.run_process(KEY, "original")
        primary = Path(self.state.get(KEY).output)
        primary.unlink()
        primary.with_suffix(".mp3").unlink()
        self.encode_calls.clear()
        self.run_process(KEY, "updated", force=True, manual=True, reencode=True)
        self.assertEqual(self.state.get(KEY).status, "review")
        self.assertEqual(self.encode_calls, [])

    def test_second_unit_cannot_claim_existing_output_even_on_convert_anyway(self):
        self.run_process(KEY, "original")
        primary = Path(self.state.get(KEY).output)
        other = "ingest:" + self.OTHER
        self.encode_calls.clear()
        self.run_process(other, "other", manual=True)
        row = self.state.get(other)
        self.assertEqual(row.status, "review", row.note)
        self.assertIn("collision", row.note.lower())
        self.run_process(other, "other", force=True, manual=True, reencode=True)
        self.assertEqual(self.state.get(other).status, "review")
        self.assertEqual(primary.read_bytes(), b"original-m4b")
        self.assertEqual(self.encode_calls, [])

    def test_unowned_mp3_beside_owned_m4b_goes_to_review(self):
        self.output_format = "M4B"
        self.run_process(KEY, "original")
        primary = Path(self.state.get(KEY).output)
        mp3 = primary.with_suffix(".mp3")
        mp3.write_bytes(b"other edition")
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "updated", manual=True)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "review", row.note)
        self.assertIn(mp3.name, row.note)
        self.assertEqual(mp3.read_bytes(), b"other edition")

    def test_changed_match_cannot_relabel_a_published_edition(self):
        self.run_process(KEY, "original")
        row = self.state.get(KEY)
        other_edition = dataclasses.replace(PHM, asin="B000000001")
        with mock.patch.object(fixtures, "PHM", other_edition):
            for _ in range(2):
                self.run_process(KEY, "updated", force=True, manual=True, reencode=True)
                self.assertEqual(self.state.get(KEY).status, "review")
        self.assertEqual(Path(row.output).read_bytes(), b"original-m4b")


if __name__ == "__main__":
    unittest.main()

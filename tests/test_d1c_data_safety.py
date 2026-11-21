"""D1c: the last three data-safety items from the CX-D1b review (CORE-04c, CORE-04d, WEBAPI-02c).

The theme: a file is Audiobind's to remove only when Audiobind can prove it wrote it.
"""

import json
import unittest

try:
    from test_d1_data_safety import DeleteCase, PublishCase
    from test_d1b_data_safety import published_m4b, wrote
    from test_ingest_pipeline import KEY
except ImportError:
    from tests.test_d1_data_safety import DeleteCase, PublishCase
    from tests.test_d1b_data_safety import published_m4b, wrote
    from tests.test_ingest_pipeline import KEY


def listing(folder) -> list:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------- CORE-04c
class Core04cUnknownProvenanceIsNotOwnershipTests(DeleteCase):
    """A converted row with no list of the files it wrote (an older release, unreadable detail)
    proves only its own output file - never the whole folder."""

    def test_a_legacy_row_leaves_the_owner_files_and_says_so(self):
        (self.out_book / "notes.txt").write_bytes(b"owner")
        (self.out_book / "cover.jpg").write_bytes(b"owner-art")
        status, body = self.delete()  # DeleteCase's row has no detail: no "wrote" list at all
        self.assertEqual(status, 200, body)
        self.assertEqual(listing(self.out_book), ["cover.jpg", "notes.txt"], "an owner file was deleted")
        self.assertEqual(body["output"], "kept")
        self.assertEqual(body["output_removed_files"], ["Book.m4b"])
        self.assertEqual(sorted(body["output_left"]), ["cover.jpg", "notes.txt"])
        self.assertIn("could not", body["output_kept_reason"])
        self.assertIsNone(self.state.get(self.REL))

    def test_unreadable_detail_is_unknown_too(self):
        (self.out_book / "notes.txt").write_bytes(b"owner")
        row = self.state.get(self.REL)
        row.detail = "{not json"
        self.state.save(row)
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual(listing(self.out_book), ["notes.txt"])

    def test_a_legacy_folder_holding_only_our_files_is_removed_completely(self):
        (self.out_book / "cover.jpg").write_bytes(b"ours")
        (self.out_book / "metadata.json").write_bytes(b"{}")
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["output"], "removed")
        self.assertFalse(self.out_book.exists())


class Core04cLegacyRowRepublishTests(PublishCase):
    """A Convert anyway into a legacy row's folder must not record the owner's files as Audiobind's."""

    def test_an_owner_file_in_a_legacy_folder_is_not_adopted(self):
        self.output_format = "M4B"
        self.run_process(KEY, "first")
        folder = published_m4b().parent
        (folder / "notes.txt").write_bytes(b"owner")
        row = self.state.get(KEY)
        detail = json.loads(row.detail)
        del detail["output"]["wrote"]  # recorded by a release without the list
        detail["output"].pop("wrote_id", None)
        row.detail = json.dumps(detail)
        self.state.save(row)
        self.output_format = "M4B + MP3"
        self.run_process(KEY, "second", manual=True)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "converted")
        self.assertNotIn("notes.txt", wrote(row))
        self.assertIn("Project Hail Mary.m4b", wrote(row))  # the row's own output is still proven


if __name__ == "__main__":
    unittest.main()

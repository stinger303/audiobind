"""C2: the delete answer says what happened to Audiobind's own conversion separately from the
owner's files left beside it, and a loose single-file book is never called a folder.

Red on 4945ad7: a mixed output folder (Audiobind's M4B + an owner file) answered output="kept"
although the M4B was removed, so the toast said "The converted copy stays in your output library";
and a loose file's delete toast said "Deleted the source folder".
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobind import web
from audiobind.state import UnitState

try:
    from test_d1_data_safety import DeleteCase
    from test_fix_b9_delete_toast import HARNESS, function_source
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase
    from tests.test_d1_data_safety import DeleteCase
    from tests.test_fix_b9_delete_toast import HARNESS, function_source


def render_toast(case: unittest.TestCase, result: dict) -> str:
    node = shutil.which("node")
    # hard requirement: a missing node fails the test instead of skipping the proof
    case.assertIsNotNone(node, "node is required to render the delete toast")
    assert node is not None  # narrows the type for static checkers; no-op at runtime
    source = function_source(web.SCRIPT, "abModalConfirm")
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "modal.js"
        js.write_text(HARNESS % (json.dumps(result), source), encoding="utf-8")
        proc = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=30)
    case.assertEqual(proc.returncode, 0, proc.stderr)
    toasts = json.loads(proc.stdout.strip().splitlines()[-1])["toasts"]
    case.assertGreaterEqual(len(toasts), 2, f"no outcome message, only {toasts}")
    return toasts[-1]


class TestC2MixedOutputFolder(DeleteCase):
    def test_ours_removed_and_the_owner_file_left_are_both_said(self):
        # the row has no wrote list: only Book.m4b is provably ours
        (self.out_book / "notes.txt").write_bytes(b"owner")
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertFalse((self.out_book / "Book.m4b").exists())
        self.assertTrue((self.out_book / "notes.txt").exists(), "the owner's file was deleted")
        self.assertEqual(body["output"], "removed", "our M4B was removed, the answer says otherwise")
        self.assertEqual(body["output_removed_files"], ["Book.m4b"])
        self.assertIn("notes.txt", body["output_other_files_left"])
        self.assertEqual(body["source_kind"], "folder")

        text = render_toast(self, body)
        self.assertNotIn("stays in your output library", text)
        self.assertIn("converted copy", text)
        self.assertIn("notes.txt", text)
        self.assertIn("left", text)

    def test_nothing_of_ours_removed_is_still_kept(self):
        row = self.state.get(self.REL)
        row.status = "ready"  # already published: the folder is not Audiobind's
        self.state.save(row)
        status, body = self.delete()
        self.assertEqual(status, 200, body)
        self.assertTrue((self.out_book / "Book.m4b").exists())
        self.assertEqual(body["output"], "kept")
        self.assertNotIn("output_other_files_left", body)

    def test_the_toast_for_a_fully_removed_output_is_unchanged(self):
        status, body = self.delete()
        self.assertEqual((status, body["output"]), (200, "removed"), body)
        self.assertNotIn("output_other_files_left", body)
        text = render_toast(self, body)
        self.assertEqual(text, "Deleted the source folder and the converted copy in your output library.")


class TestC2LooseFileIsNotAFolder(LiveServerCase):
    REL = "Nora Roberts - Island of Glass.m4b"

    def setUp(self):
        super().setUp()
        (self.lib / self.REL).write_bytes(b"audio")
        self.state.save(UnitState(path=self.REL, status="review", title="Island of Glass"))

    def test_the_toast_says_file(self):
        status, body = self.post({"action": "delete", "path": self.REL, "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["source_removed"], body)
        self.assertFalse((self.lib / self.REL).exists())
        self.assertEqual(body["source_kind"], "file")
        text = render_toast(self, body)
        self.assertNotIn("folder", text)
        self.assertIn("source file", text)

    def test_a_kept_loose_file_is_not_a_folder_either(self):
        text = render_toast(self, {"ok": True, "source_removed": False, "source_kept": True,
                                   "source_kind": "file", "output": "absent",
                                   "source_kept_reason": "your source library is mounted read-only"})
        self.assertNotIn("folder", text)
        self.assertIn("The source file stays", text)


if __name__ == "__main__":
    unittest.main()

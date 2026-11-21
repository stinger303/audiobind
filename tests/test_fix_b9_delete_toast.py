"""B9: after a Delete that removed the source folder, the toast says what was deleted and what stayed.

Red on 9c9cae1: abModalConfirm (the one in web.SCRIPT - the library page's delete modal) never read
``source_removed``. A removed source with a removed output fell through to a bare reload with no
message, and a removed source with a kept output read only "Deleted. Output kept: ...".

The other abModalConfirm, in web.LIBRARY_SCRIPT, belongs to the Settings page's Audiobookshelf
layout move and never sees a delete response.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobind import web

HARNESS = """
const toasts = [];
let reloaded = false;
function abToast(text) { toasts.push(text); }
const btn = {textContent: 'Delete permanently', disabled: false};
const document = {getElementById: () => btn};
let abDeleteTrigger = {dataset: {path: 'Author/Book', source: 'library'}};
const location = {reload() { reloaded = true; }};
function setTimeout(fn) { fn(); }
const RESULT = %s;
function fetch() { return Promise.resolve({json: () => Promise.resolve(RESULT)}); }
%s
process.on('exit', () => console.log(JSON.stringify({toasts, reloaded})));
abModalConfirm();
"""


def function_source(script: str, name: str) -> str:
    start = script.index(f"function {name}(")
    depth = 0
    for i in range(script.index("{", start), len(script)):
        depth += {"{": 1, "}": -1}.get(script[i], 0)
        if depth == 0:
            return script[start:i + 1]
    raise AssertionError(f"{name} is not closed")


class TestB9DeleteToastReadsSourceRemoved(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        # node runs the page's own JavaScript against the real function source; it is a hard
        # requirement of this test, not an optional extra, so a missing node must fail loudly
        # rather than silently skipping the proof of the B9 fix.
        self.assertIsNotNone(self.node, "node is required to run the delete-toast tests")

    def run_modal(self, result: dict) -> dict:
        source = function_source(web.SCRIPT, "abModalConfirm")
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "modal.js"
            js.write_text(HARNESS % (json.dumps(result), source), encoding="utf-8")
            proc = subprocess.run([self.node, str(js)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(out["reloaded"])
        return out

    def last_toast(self, result: dict) -> str:
        toasts = self.run_modal({"ok": True, "deleted": "Author/Book", "source_kept": False,
                                 "source_kept_reason": "", "output_kept_reason": "", **result})["toasts"]
        # the first toast is the progress one ("deleting permanently..."); the outcome must follow it
        self.assertGreaterEqual(len(toasts), 2, f"no outcome message, only {toasts}")
        return toasts[-1]

    def test_source_and_output_both_removed(self):
        text = self.last_toast({"source_removed": True, "output": "removed"})
        self.assertIn("source folder", text)
        self.assertIn("converted copy", text)
        self.assertTrue(text.startswith("Deleted the source folder and the converted copy"), text)

    def test_source_removed_output_kept_says_both(self):
        text = self.last_toast({"source_removed": True, "output": "kept",
                                "output_kept_reason": "it was changed after Audiobind wrote it"})
        self.assertIn("source folder", text)
        self.assertIn("it was changed after Audiobind wrote it", text)
        self.assertNotEqual(text, "Deleted. Output kept: it was changed after Audiobind wrote it.")

    def test_source_removed_and_no_output_says_there_was_none(self):
        text = self.last_toast({"source_removed": True, "output": "absent"})
        self.assertIn("source folder", text)
        self.assertIn("no converted copy", text)

    def test_the_existing_branches_are_unchanged(self):
        kept = self.last_toast({"source_removed": False, "source_kept": True,
                                "source_kept_reason": "your source library is mounted read-only",
                                "output": "removed"})
        self.assertIn("The source folder stays (your source library is mounted read-only)", kept)
        seeding = self.last_toast({"source_removed": False, "dismissed": True, "output": "absent"})
        self.assertTrue(seeding.startswith("Removed from Audiobind. The torrent is still seeding"))

    def test_only_one_delete_modal_handler_is_on_the_library_page(self):
        self.assertEqual(web.SCRIPT.count("function abModalConfirm("), 1)
        self.assertIn("action:'delete'", function_source(web.SCRIPT, "abModalConfirm"))
        self.assertNotIn("action:'delete'", function_source(web.LIBRARY_SCRIPT, "abModalConfirm"))


if __name__ == "__main__":
    unittest.main()

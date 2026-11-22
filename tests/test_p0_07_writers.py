import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiobind import pipeline
from audiobind.state import State
from audiobind.units import BookUnit


class TestWriterClaims(unittest.TestCase):
    def test_live_process_blocks_same_book_and_death_releases_claim(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        root = Path(tmp)
        state = State(root)
        self.addCleanup(state.close)
        book = BookUnit(path="", name="", parent="", parts=[], rel_path="Author/Book")
        other = BookUnit(path="", name="", parent="", parts=[], rel_path="Author/Other")
        key = hashlib.sha256(book.state_key.encode()).hexdigest()
        code = (
            "import fcntl, sys; "
            "lock = open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX); "
            "print('locked', flush=True); sys.stdin.read()"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code, str(root / f"media-unit-{key}.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        ) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                messages = []

                def process(_state, unit, *_args):
                    return pipeline.Audit(unit=unit, action="worked")

                with mock.patch.object(pipeline, "_process_unit", side_effect=process) as work:
                    result = pipeline.process_unit(state, book, log=messages.append)
                    self.assertEqual(result.action, "busy")
                    self.assertIn("another run is working on this book", " ".join(messages))
                    work.assert_not_called()
                    self.assertEqual(pipeline.process_unit(state, other).action, "worked")
                    child.kill()
                    child.wait(timeout=5)
                    self.assertEqual(pipeline.process_unit(state, book).action, "worked")
            finally:
                if child.poll() is None:
                    child.kill()

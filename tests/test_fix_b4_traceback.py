"""B4: a failed book leaves its traceback in the log; the row keeps only the short note.

Red on 9c9cae1: process_unit logged "FAILED: <message>" and recorded the message, so nothing said
where the failure happened.
"""

import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import matching, pipeline
from audiobind.catalog import Product
from audiobind.state import State
from audiobind.units import BookUnit, Part


def deep_failure(*_a, **_kw):
    raise ValueError("chapter table is inconsistent " + "x" * 400)


class TestB4FailedBookLogsItsTraceback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.state.close)
        self.unit = BookUnit(path="/library/A/B", name="B", parent="A",
                             parts=[Part("B", ["/library/A/B/01.mp3"])], rel_path="A/B")

    def run_unit(self):
        audit = pipeline.Audit(unit=self.unit, action="convert", confidence="HIGH")
        audit.match = matching.Match(product=Product(asin="B000000001", title="B", authors=["A"], hours=1.0))
        lines = []
        with (
            mock.patch.object(pipeline, "audit_unit", return_value=audit),
            mock.patch.object(pipeline, "convert_unit", side_effect=deep_failure),
        ):
            pipeline.process_unit(self.state, self.unit, log=lines.append, force=True)
        return "\n".join(str(line) for line in lines)

    def test_the_log_carries_the_traceback(self):
        log = self.run_unit()
        self.assertIn("FAILED: chapter table is inconsistent", log)
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("deep_failure", log)  # the frame that raised: the "where"

    def test_the_row_keeps_the_short_note_and_no_traceback(self):
        self.run_unit()
        row = self.state.get("A/B")
        self.assertEqual(row.status, "failed")
        self.assertTrue(row.note.startswith("chapter table is inconsistent"))
        self.assertLessEqual(len(row.note), 300)
        self.assertNotIn("Traceback", row.note)
        self.assertNotIn("Traceback", row.detail)


if __name__ == "__main__":
    unittest.main()

"""Delete must preserve files and rows that it cannot safely remove."""

from unittest import mock

from audiobind import pipeline, web
from audiobind.probe import Probe
from audiobind.state import UnitState
from audiobind.units import find_unit
from tests.live import LiveServerCase


class DeleteRegressionTests(LiveServerCase):
    def audited_book(self):
        source = self.lib / "Author" / "Book" / "01.mp3"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        unit = find_unit(self.lib, [], "Author/Book")
        with mock.patch.object(pipeline, "probe", return_value=Probe()), mock.patch.object(
            pipeline.matching, "match_unit", return_value=pipeline.matching.Match()
        ):
            pipeline.record(self.state, pipeline.audit_unit(unit), "review")
        return source

    def delete(self, path="Author/Book", **extra):
        return self.post({"action": "delete", "path": path, "confirm": True, **extra})

    def test_p0_01_same_path_replacement_is_kept(self):
        source = self.audited_book()
        source.write_bytes(b"different book")
        status, body = self.delete()
        self.assertEqual(status, 409, body)
        self.assertIn("re-check", body["error"])
        self.assertEqual(source.read_bytes(), b"different book")
        self.assertIsNotNone(self.state.get("Author/Book"))

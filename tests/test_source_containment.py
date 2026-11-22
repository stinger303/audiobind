import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobind import matching, pipeline
from audiobind.config import CONFIG
from audiobind.probe import Probe
from audiobind.state import State
from audiobind.units import BookUnit, Part


class TestSourceContainment(unittest.TestCase):
    def test_outside_symlink_goes_to_review_before_probe(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        tmp = tmpdir.name
        root = Path(tmp) / "library"
        book = root / "Book"
        book.mkdir(parents=True)
        outside = Path(tmp) / "outside.mp3"
        outside.touch()
        path = book / "track.mp3"
        path.symlink_to(outside)
        unit = BookUnit(str(book), "Book", str(root), [Part("Book", [str(path)])], rel_path="Book")
        state = State(Path(tmp) / "state")
        self.addCleanup(state.close)
        with (
            patch.object(CONFIG, "library_root", root),
            patch.object(pipeline, "probe", return_value=Probe(duration=60)) as probe,
            patch.object(matching, "match_unit", return_value=matching.Match()),
        ):
            pipeline.process_unit(state, unit, log=lambda _: None)
        probe.assert_not_called()
        row = state.get(unit.state_key)
        self.assertEqual(row.status, "review")
        self.assertIn("escapes library root", row.note)

    def test_internal_parent_symlink_is_resolved_and_rechecked(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name) / "library"
        actual = root / "actual"
        actual.mkdir(parents=True)
        (actual / "track.mp3").touch()
        alias = root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        path = str(alias / "track.mp3")
        unit = BookUnit(str(alias), "Book", str(root), [Part("Book", [path])])
        with patch.object(CONFIG, "library_root", root):
            guarded = pipeline.guarded_media_unit(unit)
            self.assertEqual(guarded.resolve_media(path), str(actual / "track.mp3"))
            alias.unlink()
            alias.symlink_to(root.parent, target_is_directory=True)
            with self.assertRaisesRegex(pipeline.SourceContainmentError, "escapes library root"):
                guarded.resolve_media(path)

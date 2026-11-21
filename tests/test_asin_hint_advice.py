import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobind import catalog, matching, pipeline, web
from audiobind.state import State
from audiobind.units import SOURCE_INGEST, BookUnit


class TestAsinHintAdvice(unittest.TestCase):
    def test_only_a_resolved_hint_for_a_different_work_is_explained(self):
        pick = catalog.Product(asin="B0DUNE0002", title="Dune Messiah", authors=["Frank Herbert"])
        dune = {"asin": "B0DUNE0001", "title": "Dune", "authors": [{"name": "Frank Herbert"}]}
        edition = dict(dune, title=pick.title)
        prefix = "The ASIN in your folder name,"
        sentence = (
            f"{prefix} B0DUNE0001 (Dune), "
            "belongs to a different book than the suggested match (Dune Messiah)."
        )
        rows = [
            ("Conflict", dune["asin"], dune),
            ("Same ASIN", pick.asin, None),
            ("Same work", edition["asin"], edition),
            ("No hint", "", None),
            ("Unknown hint", dune["asin"], None),
            ("Unreadable hint", dune["asin"], dict(dune, title=None)),
        ]
        # the State keeps a sqlite connection (and its WAL sidecars) open. closing() exits before the
        # TemporaryDirectory does (context managers unwind in reverse), so the connection is shut
        # before the directory is removed instead of racing a file it recreates.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                contextlib.closing(State(Path(tmp))) as state:
            for name, asin, record in rows:
                unit = BookUnit(path=str(Path(tmp) / name), name=name, parent=tmp, parts=[], rel_path=name,
                                source=SOURCE_INGEST, asin_hint=asin)
                audit = pipeline.Audit(unit=unit, match=matching.Match(product=pick), confidence="LOW")
                with patch.object(catalog, "cached_book", side_effect={asin: record}.get):
                    pipeline.record(state, audit, "review")
            self.assertIn(sentence, web.render(state, query="Conflict"))
            for name, _, _ in rows[1:]:
                with self.subTest(row=name):
                    self.assertNotIn(prefix, web.render(state, query=name))

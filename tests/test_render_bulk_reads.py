"""B2: the status page reads overrides and detail in bulk, and one odd row cannot take the page down."""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import abs_layout, discover, series, web
from audiobind.state import State, UnitState
from audiobind.util import loads_dict


class TestLoadsDict(unittest.TestCase):
    def test_only_a_json_object_survives(self):
        self.assertEqual(loads_dict('{"a": 1}'), {"a": 1})
        for raw in (None, "", "{}", "[]", "null", "5", '"x"', "{bad", "not json", b"\xff", 7):
            self.assertEqual(loads_dict(raw), {}, repr(raw))

    def test_the_modules_that_each_had_a_copy_share_it(self):
        for module in (abs_layout, series, discover):
            self.assertIs(module._loads, loads_dict, module.__name__)


class TestAllOverrides(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.state.close)

    def test_it_agrees_with_get_overrides_for_every_row(self):
        for name in ("A/One", "A/Two", "A/Three", "A/Four"):
            self.state.save(UnitState(path=name, status="review"))
        self.state.set_overrides("A/One", {"title": "Mine", "series": "S"})
        self.state.set_overrides("A/Two", {})
        self.state.connection.execute("UPDATE units SET overrides = '[1]' WHERE path = 'A/Three'")  # not an object
        self.state.connection.execute("UPDATE units SET overrides = 'junk' WHERE path = 'A/Four'")
        every = self.state.all_overrides()
        self.assertEqual(every, {"A/One": {"title": "Mine", "series": "S"}})
        for path in ("A/One", "A/Two", "A/Three", "A/Four", "A/Missing"):
            self.assertEqual(every.get(path, {}), self.state.get_overrides(path), path)


class TestRenderReadsInBulk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.state.close)

    def test_a_row_whose_detail_is_not_an_object_does_not_break_the_page(self):
        for i, raw in enumerate(("[]", "null", "5", '"text"', "{bad", "")):
            self.state.save(UnitState(path=f"A/B{i}", status="review", title=f"Book {i}", detail=raw))
        self.state.save(UnitState(path="A/Good", status="review", title="Good Book", detail=json.dumps({"match": {"title": "Good"}})))
        for status in ("", "attention", "review", "all"):
            html = web.render(self.state, status)
            self.assertIn("Good Book", html, status)
            self.assertIn("Book 0", html, status)

    def test_render_makes_no_per_row_overrides_query(self):
        for i in range(25):
            self.state.save(UnitState(path=f"A/B{i}", status="held", title=f"Book {i}"))
        self.state.set_overrides("A/B3", {"title": "Corrected"})
        with mock.patch.object(State, "get_overrides", side_effect=AssertionError("per-row query")):
            html = web.render(self.state, "held")
        self.assertIn("Book 7", html)

    def test_an_override_still_shows_in_the_detail_panel(self):
        self.state.save(UnitState(path="A/One", status="review", title="One"))
        self.state.set_overrides("A/One", {"title": "Corrected Title"})
        self.assertIn("Corrected Title", web.render(self.state, "review"))

    def test_the_reopen_flag_is_still_consumed_once(self):
        self.state.save(UnitState(path="A/One", status="review", title="One", detail=json.dumps({"reopen": True})))
        web.render(self.state, "review")
        self.assertFalse(json.loads(self.state.get("A/One").detail)["reopen"])


if __name__ == "__main__":
    unittest.main()

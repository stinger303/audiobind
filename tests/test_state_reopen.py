"""v0.14.4: clear_reopen is atomic; UnitState carries no misleading overrides field."""

import json
import pathlib
import tempfile
import threading
import types
import unittest
from unittest import mock

from audiobind import state as state_mod
from audiobind.state import State, UnitState


class TestClearReopenAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_a_record_landing_mid_clear_is_not_overwritten(self):
        self.state.save(UnitState(path="A/B", status="review", detail=json.dumps({"reopen": True, "v": "old"})))
        read_done = threading.Event()

        def slow_loads(text):
            value = json.loads(text)
            read_done.set()
            threading.Event().wait(0.3)  # widen the window between read and write
            return value

        fake_json = types.SimpleNamespace(loads=slow_loads, dumps=json.dumps)
        fresh = json.dumps({"reopen": True, "v": "new"})
        with mock.patch.object(state_mod, "json", fake_json):
            clearer = threading.Thread(target=self.state.clear_reopen, args=("A/B",))
            clearer.start()
            self.assertTrue(read_done.wait(5))
            # a manual re-check records fresh detail while the clear is in progress
            writer = threading.Thread(
                target=self.state.save, args=(UnitState(path="A/B", status="held", detail=fresh),)
            )
            writer.start()
            clearer.join(5)
            writer.join(5)
        row = self.state.get("A/B")
        self.assertEqual(row.status, "held")
        self.assertEqual(json.loads(row.detail)["v"], "new")

    def test_clear_reopen_still_clears(self):
        self.state.save(UnitState(path="A/B", status="review", detail=json.dumps({"reopen": True})))
        self.state.clear_reopen("A/B")
        self.assertFalse(json.loads(self.state.get("A/B").detail)["reopen"])


class TestNoOverridesField(unittest.TestCase):
    def test_unitstate_has_no_overrides_field(self):
        self.assertNotIn("overrides", UnitState.__annotations__)

    def test_a_resave_keeps_operator_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(pathlib.Path(d))
            try:
                st.save(UnitState(path="A/B", status="review"))
                st.set_overrides("A/B", {"title": "Mine"})
                row = st.get("A/B")
                st.save(row)  # a full round-trip of the row must not touch the overrides
                self.assertEqual(st.get_overrides("A/B"), {"title": "Mine"})
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()

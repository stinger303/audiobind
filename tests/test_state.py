import pathlib
import tempfile
import unittest

from audiobind.state import State, UnitState


class TestAsinOverride(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_roundtrip_on_an_existing_unit(self):
        self.state.save(UnitState(path="A/B", status="review", title="B"))
        self.state.set_asin_override("A/B", "B005EHN61O")
        self.assertEqual(self.state.get_asin_override("A/B"), "B005EHN61O")

    def test_override_creates_a_row_when_the_unit_is_unknown(self):
        self.state.set_asin_override("New/Book", "B005EHN61O")
        self.assertEqual(self.state.get_asin_override("New/Book"), "B005EHN61O")

    def test_default_is_empty(self):
        self.state.save(UnitState(path="C/D", status="review"))
        self.assertEqual(self.state.get_asin_override("C/D"), "")


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_delete_removes_the_row(self):
        self.state.save(UnitState(path="A/B", status="converted", title="B"))
        self.assertIsNotNone(self.state.get("A/B"))
        self.assertTrue(self.state.delete("A/B"))
        self.assertIsNone(self.state.get("A/B"))

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(self.state.delete("nowhere"))

    def test_delete_only_removes_the_target_row(self):
        self.state.save(UnitState(path="A/1", status="converted"))
        self.state.save(UnitState(path="A/2", status="held"))
        self.assertTrue(self.state.delete("A/1"))
        self.assertIsNone(self.state.get("A/1"))
        self.assertIsNotNone(self.state.get("A/2"))


if __name__ == "__main__":
    unittest.main()

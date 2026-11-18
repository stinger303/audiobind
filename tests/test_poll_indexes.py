"""B2: the status poll's three aggregates read a covering index, not the whole table."""

import pathlib
import tempfile
import unittest

from audiobind.state import State, UnitState

POLL_QUERIES = {
    "counts": "SELECT status, COUNT(*) n FROM units GROUP BY status",
    "rev": "SELECT MAX(updated_at), COUNT(*) FROM units",
    "library_totals": (
        "SELECT COUNT(*), COALESCE(SUM(status = 'converted'), 0),"
        " COALESCE(SUM(CASE WHEN status = 'converted' THEN COALESCE(hours, 0) END), 0),"
        " COALESCE(SUM(CASE WHEN status = 'converted' THEN COALESCE(out_mb, 0) END), 0) FROM units"
    ),
}


class TestPollIndexes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.state.close)

    def index_names(self, state=None):
        rows = (state or self.state).connection.execute("PRAGMA index_list(units)").fetchall()
        return {r["name"] for r in rows}

    def test_the_indexes_exist_and_a_second_open_is_a_no_op(self):
        self.assertTrue({"idx_units_updated_at", "idx_units_status_totals"} <= self.index_names())
        again = State(pathlib.Path(self.dir.name))  # CREATE INDEX IF NOT EXISTS: idempotent
        self.addCleanup(again.close)
        self.assertEqual(self.index_names(again), self.index_names())

    def test_each_poll_query_is_answered_from_a_covering_index(self):
        for i in range(20):
            self.state.save(UnitState(path=f"A/B{i}", status="converted" if i % 2 else "held", hours=1.0, out_mb=2.0, updated_at=float(i)))
        for name, sql in POLL_QUERIES.items():
            plan = " ".join(r[3] for r in self.state.connection.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("COVERING INDEX", plan, f"{name}: {plan}")

    def test_the_answers_do_not_depend_on_the_index(self):
        self.state.save(UnitState(path="A/One", status="converted", hours=2.5, out_mb=100.0, updated_at=5.0))
        self.state.save(UnitState(path="A/Two", status="held", hours=9.0, out_mb=900.0, updated_at=7.0))
        self.assertEqual(self.state.counts(), {"converted": 1, "held": 1})
        newest = self.state.connection.execute("SELECT MAX(updated_at) FROM units").fetchone()[0]
        self.assertEqual(self.state.rev(), f"{newest:.6f}|2")
        self.assertEqual(self.state.library_totals(), (2, 1, 2.5, 100.0))

    def test_an_existing_database_without_the_indexes_gains_them(self):
        self.state.connection.execute("DROP INDEX idx_units_updated_at")
        self.state.connection.execute("DROP INDEX idx_units_status_totals")
        self.state.close()
        reopened = State(pathlib.Path(self.dir.name))
        self.addCleanup(reopened.close)
        self.state = reopened
        self.assertTrue({"idx_units_updated_at", "idx_units_status_totals"} <= self.index_names())


if __name__ == "__main__":
    unittest.main()

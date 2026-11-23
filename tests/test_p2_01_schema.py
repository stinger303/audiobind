import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from audiobind.state import State, UnitState


class SchemaMigration(unittest.TestCase):
    def test_concurrent_open_migrates_and_bootstraps_once(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            connect = sqlite3.connect
            state = State(directory)
            state.save(UnitState(path="Book", status="review"))
            state.set_overrides("Book", {"title": "Corrected"})
            state.connection.execute("ALTER TABLE units DROP COLUMN pin_failures")
            state.connection.execute("DELETE FROM field_locks")
            state.connection.execute("DELETE FROM meta WHERE key = 'field_locks_v1'")
            state.close()

            started = threading.Event()
            second_attempt = threading.Event()
            alters = []
            bootstraps = []

            class Connection(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql.startswith("ALTER TABLE units ADD COLUMN pin_failures"):
                        alters.append(sql)
                        if len(alters) == 1:
                            started.set()
                            second_attempt.wait(0.5)
                        else:
                            second_attempt.set()
                    if "INSERT OR REPLACE INTO meta" in sql and "field_locks_v1" in sql:
                        bootstraps.append(sql)
                    return super().execute(sql, *args)

            def open_and_check():
                state = State(directory)
                try:
                    return state.get("Book").status, state.locked_fields("Book")
                finally:
                    state.close()

            with patch("audiobind.state.sqlite3.connect",
                       side_effect=lambda *a, **kw: connect(*a, factory=Connection, **kw)):
                with ThreadPoolExecutor(2) as pool:
                    first = pool.submit(open_and_check)
                    self.assertTrue(started.wait(5))
                    second = pool.submit(open_and_check)
                    self.assertEqual(first.result(), ("review", {"title"}))
                    self.assertEqual(second.result(), ("review", {"title"}))

            self.assertEqual(len(alters), 1)
            self.assertEqual(len(bootstraps), 1)

import pathlib
import tempfile
import threading
import unittest

from audiobind.state import State, UnitState


class TestConcurrentAccess(unittest.TestCase):
    """The daemon, the web handlers and manual actions share one State object."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_parallel_writes_do_not_raise(self):
        errors = []

        def writer(n):
            try:
                for i in range(25):
                    self.state.save(UnitState(path=f"w{n}/book{i}", status="ready",
                                              title=f"book{i}", hours=1.5))
                    self.state.counts()
                    self.state.set_meta("paused", False)
            except Exception as exc:                      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent access failed: {errors}")
        self.assertEqual(self.state.counts().get("ready"), 100)

    def test_reads_during_writes_are_consistent(self):
        errors = []

        def writer():
            try:
                for i in range(50):
                    self.state.save(UnitState(path=f"x/{i}", status="converted"))
            except Exception as exc:                      # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(50):
                    self.state.counts()
                    len(self.state.all())
            except Exception as exc:                      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

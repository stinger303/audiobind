import pathlib
import tempfile
import unittest

from audiobind import web
from audiobind.state import State, UnitState


class TestLibraryStats(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def save(self, path, status, hours=0.0, out_mb=0.0):
        self.state.save(UnitState(path=path, status=status, title=path,
                                  hours=hours, out_mb=out_mb))

    def test_hours_and_gb_sum_only_converted_units(self):
        self.save("A/One", "converted", hours=6.5, out_mb=2048.0)
        self.save("A/Two", "converted", hours=1.5, out_mb=512.0)
        self.save("A/Three", "ready", hours=9.9, out_mb=9999.0)   # not converted yet
        self.save("A/Four", "failed")
        stats = web.library_stats(self.state)
        self.assertEqual(stats["hours"], 8.0)
        self.assertEqual(stats["gb"], 2.5)                        # 2560 MB / 1024
        self.assertEqual(stats["converted"], 2)
        self.assertEqual(stats["total"], 4)

    def test_progress_is_converted_over_total(self):
        self.save("A/One", "converted")
        self.save("A/Two", "converted")
        self.save("A/Three", "ready")
        self.save("A/Four", "review")
        stats = web.library_stats(self.state)
        self.assertEqual(stats["progress"], 50.0)

    def test_empty_library_reports_zeroes_without_dividing_by_zero(self):
        stats = web.library_stats(self.state)
        self.assertEqual(stats, {"hours": 0, "gb": 0, "converted": 0,
                                 "total": 0, "progress": 0.0})

    def test_units_without_sizes_contribute_nothing(self):
        self.save("A/One", "converted")                           # hours/out_mb default 0
        stats = web.library_stats(self.state)
        self.assertEqual(stats["hours"], 0)
        self.assertEqual(stats["gb"], 0)
        self.assertEqual(stats["progress"], 100.0)

    def test_snapshot_carries_the_stats_block(self):
        self.save("A/One", "converted", hours=3.0, out_mb=1024.0)
        stats = web.snapshot(self.state)["stats"]
        self.assertEqual(stats["hours"], 3.0)
        self.assertEqual(stats["gb"], 1.0)

    def test_render_shows_the_stat_cards(self):
        self.save("A/One", "converted", hours=6.0, out_mb=1536.0)
        self.save("A/Two", "ready")
        page = web.render(self.state)
        self.assertIn("hours converted", page)
        self.assertIn("6 h", page)
        self.assertIn("1.5 GB", page)
        self.assertIn("library progress", page)
        self.assertIn("50%", page)
        self.assertIn('style="width:50.0%"', page)
        self.assertIn("1 of 2 books converted", page)


if __name__ == "__main__":
    unittest.main()

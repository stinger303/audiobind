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
        self.state.save(UnitState(path=path, status=status, title=path, hours=hours, out_mb=out_mb))

    def test_hours_and_gb_sum_only_converted_units(self):
        self.save("A/One", "converted", hours=6.5, out_mb=2048.0)
        self.save("A/Two", "converted", hours=1.5, out_mb=512.0)
        self.save("A/Three", "ready", hours=9.9, out_mb=9999.0)  # not converted yet
        self.save("A/Four", "failed")
        stats = web.library_stats(self.state)
        self.assertEqual(stats["hours"], 8.0)
        self.assertEqual(stats["gb"], 2.56)  # 2560 decimal MB
        self.assertEqual(stats["converted"], 2)
        self.assertEqual(stats["total"], 4)

    def test_gb_follows_the_decimal_mb_the_pipeline_records(self):
        """out_mb is bytes/1e6, so 1000 MB is exactly 1 GB - never 1024."""
        self.save("A/One", "converted", out_mb=1000.0)
        self.assertEqual(web.library_stats(self.state)["gb"], 1.0)

    def test_progress_is_converted_over_total(self):
        self.save("A/One", "converted")
        self.save("A/Two", "converted")
        self.save("A/Three", "ready")
        self.save("A/Four", "review")
        stats = web.library_stats(self.state)
        self.assertEqual(stats["progress"], 50.0)

    def test_empty_library_reports_zeroes_without_dividing_by_zero(self):
        stats = web.library_stats(self.state)
        self.assertEqual(stats, {"hours": 0, "gb": 0, "converted": 0, "total": 0, "progress": 0.0})

    def test_units_without_sizes_contribute_nothing(self):
        self.save("A/One", "converted")  # hours/out_mb default 0
        stats = web.library_stats(self.state)
        self.assertEqual(stats["hours"], 0)
        self.assertEqual(stats["gb"], 0)
        self.assertEqual(stats["progress"], 100.0)

    def test_null_sizes_in_the_table_count_as_zero(self):
        """A hand-made or old row can hold NULL hours / out_mb; SQL SUM must not turn that into None."""
        self.save("A/One", "converted", hours=2.0, out_mb=100.0)
        self.state.connection.execute("UPDATE units SET hours = NULL, out_mb = NULL WHERE path = 'A/One'")
        self.save("A/Two", "converted", hours=1.0, out_mb=200.0)
        self.assertEqual(self.state.library_totals(), (2, 2, 1.0, 200.0))
        self.assertEqual(State(pathlib.Path(tempfile.mkdtemp())).library_totals(), (0, 0, 0.0, 0.0))

    def test_path_statuses_and_outputs_skip_rows_without_the_field(self):
        self.state.save(UnitState(path="A/One", status="converted", output="/out/One.m4b"))
        self.state.save(UnitState(path="A/Two", status="review"))
        self.assertEqual(sorted(self.state.path_statuses()), [("A/One", "converted"), ("A/Two", "review")])
        self.assertEqual(self.state.outputs(), [("A/One", "/out/One.m4b")])

    def test_get_meta_many_matches_get_meta_per_key(self):
        self.state.set_meta("a", {"x": 1})
        self.state.set_meta("b", "text")
        self.state.set_meta("n", None)  # stored JSON null: present, decodes to None
        keys = ["a", "b", "n", "missing", "a"]  # duplicate and absent keys are fine
        self.assertEqual(
            self.state.get_meta_many(keys),
            {k: self.state.get_meta(k) for k in ("a", "b", "n")},
        )
        self.assertEqual(self.state.get_meta_many([]), {})
        many = [f"k{i}" for i in range(1200)]  # more than one chunk
        for k in many[::100]:
            self.state.set_meta(k, k)
        self.assertEqual(set(self.state.get_meta_many(many)), set(many[::100]))

    def test_snapshot_carries_the_stats_block(self):
        self.save("A/One", "converted", hours=3.0, out_mb=1000.0)
        stats = web.snapshot(self.state)["stats"]
        self.assertEqual(stats["hours"], 3.0)
        self.assertEqual(stats["gb"], 1.0)

    def test_render_shows_progress_and_on_demand_stats(self):
        self.save("A/One", "converted", hours=6.0, out_mb=1500.0)
        self.save("A/Two", "ready")
        page = web.render(self.state)
        self.assertIn("converted", page)
        stats = page.split('<details class="ab-libstats">')[1].split("</details>")[0]
        self.assertIn("6.0 hours converted", stats)
        self.assertIn("1.5 GB written", stats)
        self.assertNotIn("h h", page)
        self.assertNotIn("GB GB", page)
        self.assertIn('role="progressbar"', page)
        self.assertIn('aria-valuenow="50.0"', page)
        self.assertIn('style="width:50.0%"', page)
        self.assertIn("<b>1</b> of 2 converted", page)

    def test_the_stats_bar_does_not_reuse_the_encode_bar_classes(self):
        """A shared .bar class crushed the toolbar once; the stats bar owns its classes."""
        self.save("A/One", "converted", hours=1.0, out_mb=100.0)
        page = web.render(self.state)
        self.assertIn('class="ab-statsline"', page)
        self.assertIn('class="ab-libbar"', page)
        # .ptrack belongs to the live encode progress bar and must not appear in the stats line
        stats_section = page.split('class="ab-statsline"')[1].split("</section>")[0]
        self.assertNotIn("ptrack", stats_section)


if __name__ == "__main__":
    unittest.main()

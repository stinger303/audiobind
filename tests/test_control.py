import unittest

from audiobind.control import Control, scan_label, unit_display_name


class TestControl(unittest.TestCase):
    def setUp(self):
        self.c = Control()

    def test_starts_running(self):
        self.assertFalse(self.c.paused)
        self.assertFalse(self.c.scan_requested())

    def test_pause_and_resume(self):
        self.c.pause()
        self.assertTrue(self.c.paused)
        self.c.resume()
        self.assertFalse(self.c.paused)

    def test_resume_requests_a_scan(self):
        self.c.pause()
        self.c.resume()
        self.assertTrue(self.c.scan_requested(), "resuming should not wait for the next interval")

    def test_scan_request_can_be_cleared(self):
        self.c.request_scan()
        self.assertTrue(self.c.scan_requested())
        self.c.clear_scan()
        self.assertFalse(self.c.scan_requested())

    def test_wait_returns_immediately_when_scan_requested(self):
        self.c.request_scan()
        self.assertTrue(self.c.wait(5))

    def test_wait_times_out_quietly(self):
        self.assertFalse(self.c.wait(0.01))

    def test_dry_run_follows_the_env_by_default(self):
        from audiobind.config import CONFIG

        fresh = Control()
        self.assertEqual(fresh.dry_run, CONFIG.dry_run)
        self.assertEqual(fresh.dry_run_source, "default")

    def test_dry_run_can_be_overridden_at_runtime(self):
        from audiobind.config import CONFIG

        self.c.set_dry_run(not CONFIG.dry_run)
        self.assertNotEqual(self.c.dry_run, CONFIG.dry_run)
        self.assertEqual(self.c.dry_run_source, "set in UI")

    def test_stop_requests_and_pauses(self):
        self.assertFalse(self.c.stop_requested)
        self.c.stop()
        self.assertTrue(self.c.stop_requested)
        self.assertTrue(self.c.paused, "stopping must also hold, or the next book starts at once")

    def test_resume_clears_the_stop(self):
        self.c.stop()
        self.c.resume()
        self.assertFalse(self.c.stop_requested)
        self.assertFalse(self.c.paused)

    def test_current_unit_tracks_the_running_book(self):
        self.assertFalse(self.c.running)
        self.c.begin_unit("Author/Book")
        self.assertTrue(self.c.running)
        self.assertEqual(self.c.current_unit, "Author/Book")
        self.c.end_unit()
        self.assertFalse(self.c.running)

    def test_describe_reports_whether_a_book_is_running(self):
        self.c.begin_unit("A/B")
        described = self.c.describe()
        self.assertTrue(described["running"])
        self.assertEqual(described["current_unit"], "A/B")

    def test_progress_is_reported_and_cleared(self):
        self.c.begin_unit("A/B")
        self.c.set_progress(percent=42.5, eta=120, speed=4.1)
        described = self.c.describe()
        self.assertEqual(described["progress"]["percent"], 42.5)
        self.c.clear_progress()
        self.assertEqual(self.c.describe()["progress"], {})

    def test_starting_a_book_resets_progress(self):
        self.c.set_progress(percent=90)
        self.c.begin_unit("C/D")
        self.assertEqual(self.c.progress, {})

    def test_finishing_a_book_clears_progress_and_unit(self):
        self.c.begin_unit("E/F")
        self.c.set_progress(percent=10)
        self.c.end_unit()
        self.assertEqual(self.c.progress, {})
        self.assertEqual(self.c.current_unit, "")

    def test_describe_reports_dry_run(self):
        self.c.set_dry_run(True)
        self.assertTrue(self.c.describe()["dry_run"])


class TestScanProgress(unittest.TestCase):
    """The status page's busy indicator reads this - it must appear and vanish."""

    def setUp(self):
        self.c = Control()

    def test_describe_reports_not_scanning_by_default(self):
        self.assertEqual(self.c.describe()["scan"], {"scanning": False, "done": 0, "total": 0})

    def test_begin_tick_end_roundtrip(self):
        self.c.begin_scan(98)
        self.c.scan_tick(37)
        self.assertTrue(self.c.scanning)
        self.assertEqual(self.c.describe()["scan"], {"scanning": True, "done": 37, "total": 98})
        self.c.end_scan()
        self.assertFalse(self.c.scanning)
        self.assertEqual(self.c.describe()["scan"]["done"], 0)

    def test_end_scan_clears_even_without_ticks(self):
        self.c.begin_scan(3)
        self.c.end_scan()
        self.assertFalse(self.c.describe()["scan"]["scanning"])

    def test_scan_label_names_the_book_being_encoded(self):
        self.c.begin_scan(41)
        self.c.scan_tick(37)
        self.c.begin_unit("Matt Dinniman/Dungeon Crawler Carl 04", "The Gate of the Feral Gods")
        self.c.set_progress(percent=43.7, elapsed=400, eta=520, speed=40.0)
        d = self.c.describe()
        self.assertEqual(d["scan_label"], "Encoding The Gate of the Feral Gods - 43%…")
        self.assertEqual(d["current_title"], "The Gate of the Feral Gods")
        # the scan counters are still reported alongside, unchanged
        self.assertEqual(d["scan"], {"scanning": True, "done": 37, "total": 41})

    def test_scan_label_falls_back_to_the_count_when_nothing_encodes(self):
        self.c.begin_scan(41)
        self.c.scan_tick(37)
        self.assertEqual(self.c.describe()["scan_label"], "Scanning 37/41…")
        self.c.begin_unit("A/B", "B")
        self.c.end_unit()
        self.assertEqual(self.c.describe()["scan_label"], "Scanning 37/41…")
        self.assertEqual(self.c.describe()["current_title"], "")

    def test_scan_label_is_empty_when_not_scanning(self):
        self.c.begin_unit("A/B", "B")  # a manual convert outside a scan has its own strip
        self.assertEqual(self.c.describe()["scan_label"], "")

    def test_scan_label_before_the_first_progress_tick(self):
        self.c.begin_scan(2)
        self.c.begin_unit("Author/Book", "Book")
        self.assertEqual(self.c.describe()["scan_label"], "Encoding Book…")

    def test_scan_label_formatting(self):
        on = {"scanning": True, "done": 1, "total": 3}
        self.assertEqual(scan_label(on, "Author/Folder Name", {"percent": 99.9}), "Encoding Folder Name - 99%…")
        self.assertEqual(scan_label(on, "ingest:Some Book", {}), "Encoding Some Book…")
        self.assertEqual(scan_label(on, "Author/Parent#2", {"percent": 0}), "Encoding Parent (2) - 0%…")
        self.assertEqual(scan_label(on, "A/B", {"percent": "junk"}, "Title"), "Encoding Title…")
        self.assertEqual(scan_label(on, "", {"percent": 50}), "Scanning 1/3…")
        self.assertEqual(scan_label({"scanning": False}, "A/B", {"percent": 50}), "")
        self.assertEqual(unit_display_name("A/B", "  "), "B")

    def test_scan_force_is_one_shot(self):
        self.c.request_scan(force=True)
        self.assertTrue(self.c.scan_force)
        self.assertFalse(self.c.scan_force)  # consumed by the first read
        self.c.request_scan()  # a plain Scan now never sets it
        self.assertFalse(self.c.scan_force)


if __name__ == "__main__":
    unittest.main()

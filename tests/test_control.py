import unittest

from audiobind.control import Control


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

    def test_describe_reports_dry_run(self):
        self.c.set_dry_run(True)
        self.assertTrue(self.c.describe()["dry_run"])


if __name__ == "__main__":
    unittest.main()

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

    def test_describe_reports_dry_run(self):
        self.c.set_dry_run(True)
        self.assertTrue(self.c.describe()["dry_run"])


if __name__ == "__main__":
    unittest.main()

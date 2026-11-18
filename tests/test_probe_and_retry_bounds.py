"""B1: no ffprobe call may wait forever, and a failed catalog lookup does not back off after its last try."""

import subprocess
import unittest
import urllib.error
from unittest import mock

from audiobind import catalog, probe


class TestListProbesHaveATimeout(unittest.TestCase):
    def test_timeout_is_passed_to_subprocess(self):
        for func in (probe.probe_chapters, probe.probe_streams):
            with mock.patch.object(probe.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="{}")
                func("/x.m4b")
            self.assertEqual(run.call_args.kwargs.get("timeout"), probe.LIST_PROBE_TIMEOUT, func.__name__)

    def test_a_hung_ffprobe_is_an_empty_answer_not_a_hang(self):
        expired = subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)
        with self.assertLogs("audiobind", level="WARNING"):  # the timeout is logged (own test below)
            for func in (probe.probe_chapters, probe.probe_streams):
                with mock.patch.object(probe.subprocess, "run", side_effect=expired):
                    self.assertEqual(func("/x.m4b"), [], func.__name__)
            with mock.patch.object(probe.subprocess, "run", side_effect=expired):
                self.assertFalse(probe.has_embedded_cover("/x.m4b"))

    def test_a_timeout_is_logged_not_silent(self):
        # an empty list reads as "no chapters" / "no streams" to the encoder's checks, so the
        # log is the only place the real cause shows
        expired = subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)
        for func in (probe.probe_chapters, probe.probe_streams, probe.probe):
            with mock.patch.object(probe.subprocess, "run", side_effect=expired):
                with self.assertLogs("audiobind", level="WARNING") as logs:
                    func("/x.m4b")
            self.assertIn("timed out", logs.output[0], func.__name__)
            self.assertIn("/x.m4b", logs.output[0], func.__name__)

    def test_a_missing_ffprobe_still_raises(self):
        # unchanged behaviour: only the timeout is softened
        with mock.patch.object(probe.subprocess, "run", side_effect=FileNotFoundError("ffprobe")):
            with self.assertRaises(FileNotFoundError):
                probe.probe_chapters("/x.m4b")


class TestCatalogBackoff(unittest.TestCase):
    def setUp(self):
        catalog.cache_clear()
        self.addCleanup(catalog.cache_clear)

    def _failing(self, exc):
        sleeps = []
        patches = (
            mock.patch.object(catalog.urllib.request, "urlopen", side_effect=exc),
            mock.patch.object(catalog.time, "sleep", side_effect=sleeps.append),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return sleeps

    def test_backoff_between_tries_but_not_after_the_last(self):
        sleeps = self._failing(urllib.error.URLError("down"))
        catalog._last_call = 0.0  # the 0.6 s politeness-gap sleeps stay below the 1.0 filter: not under test
        self.assertIsNone(catalog._get_json("https://example.test/x", tries=3))
        backoffs = [s for s in sleeps if s >= 1.0]
        self.assertEqual(backoffs, [1.2, 2.4])

    def test_http_error_follows_the_same_rule(self):
        err = urllib.error.HTTPError("https://example.test/x", 503, "busy", {}, None)
        self.addCleanup(err.close)  # it wraps a temp file: left open it is a ResourceWarning at exit
        sleeps = self._failing(err)
        catalog._last_call = 0.0
        self.assertIsNone(catalog._get_json("https://example.test/x", tries=2))
        self.assertEqual([s for s in sleeps if s >= 1.0], [1.2])


if __name__ == "__main__":
    unittest.main()

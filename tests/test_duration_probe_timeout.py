"""C4: an ffprobe timeout on a source file's duration fails the book instead of reading as 0.0.

probe() used to return Probe() (duration 0) for a timeout, so _durations() recorded 0.0, which made
_source_chapters() drop the whole book's chapters and plan_chapters() scale against a short total.
"""

import subprocess
import unittest
from unittest import mock

from audiobind import encode, probe
from audiobind.probe import Probe, ProbeTimeout
from audiobind.units import BookUnit, Part

EXPIRED = subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)


def unit(*files):
    return BookUnit(path="X", name="X", parent="", parts=[Part(name="X", files=list(files))])


class TestProbeFlag(unittest.TestCase):
    def test_default_is_unchanged_a_logged_empty_probe(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED):
            with self.assertLogs("audiobind", level="WARNING"):
                result = probe.probe("/x.mp3")
        self.assertEqual(result, Probe())
        self.assertEqual(result.duration, 0.0)

    def test_raise_on_timeout_raises_the_existing_error_with_the_cap(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED):
            with self.assertLogs("audiobind", level="WARNING") as logs:
                with self.assertRaises(ProbeTimeout) as caught:
                    probe.probe("/x.mp3", timeout=7, raise_on_timeout=True)
        self.assertEqual((caught.exception.path, caught.exception.timeout), ("/x.mp3", 7))
        self.assertIn("timed out", logs.output[0])

    def test_an_answer_without_a_duration_is_not_a_timeout(self):
        with mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(stdout="{}")):
            self.assertEqual(probe.probe("/x.mp3", raise_on_timeout=True), Probe())


class TestDurations(unittest.TestCase):
    def test_a_timed_out_file_fails_naming_the_file_and_the_cap(self):
        u = unit("/src/a.mp3", "/src/b.mp3")

        def fake(path, timeout=60, raise_on_timeout=False):
            if path == "/src/b.mp3":
                raise ProbeTimeout(path, 60, "duration")
            return Probe(duration=300.0)

        with mock.patch.object(encode, "probe", side_effect=fake) as probed:
            with self.assertRaises(RuntimeError) as caught:
                encode._durations(u, None)
        self.assertIn("/src/b.mp3", str(caught.exception))
        self.assertIn("60s", str(caught.exception))
        for call in probed.call_args_list:
            self.assertTrue(call.kwargs.get("raise_on_timeout"))

    def test_supplied_durations_are_not_probed(self):
        with mock.patch.object(encode, "probe", side_effect=AssertionError("probed")):
            self.assertEqual(encode._durations(unit("/a.mp3"), {"/a.mp3": 5.0}), {"/a.mp3": 5.0})

    def test_plan_chapters_fails_instead_of_summing_a_zero(self):
        u = unit("/src/a.mp3", "/src/b.mp3")
        with mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED), \
                mock.patch.object(encode.catalog, "chapters", return_value=[]), \
                self.assertLogs("audiobind", level="WARNING"):
            with self.assertRaises(RuntimeError) as caught:
                encode.plan_chapters(u, "B000000001")
        self.assertIn("/src/a.mp3", str(caught.exception))

    def test_a_file_answered_with_no_duration_still_means_no_source_chapters(self):
        u = unit("/src/a.mp3")
        with mock.patch.object(encode, "probe", return_value=Probe()), \
                mock.patch.object(encode, "probe_chapters", side_effect=AssertionError("not reached")):
            self.assertEqual(encode._source_chapters(u), [])


if __name__ == "__main__":
    unittest.main()

"""C3: an ffprobe timeout on a source file fails the book; a file with no chapter atoms still converts.

probe_chapters() used to return [] for both, so a timed-out source silently contributed zero chapters
and the output check (which compares against that already-shortened list) passed.
"""

import subprocess
import unittest
from unittest import mock

from audiobind import encode, probe
from audiobind.probe import Probe, ProbeTimeout
from audiobind.units import BookUnit, Part

from audiobind import catalog, daemon, pipeline

try:
    from test_ingest_pipeline import KEY, PHM, PipelineCase
except ImportError:
    from tests.test_ingest_pipeline import KEY, PHM, PipelineCase

EXPIRED = subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)
TAGGED = [
    {"start_time": "0", "end_time": "100", "tags": {"title": "One"}},
    {"start_time": "100", "end_time": "300", "tags": {"title": "Two"}},
]


def unit(*files):
    return BookUnit(path="X", name="X", parent="", parts=[Part(name="X", files=list(files))])


def fake_probe(durations):
    return lambda path, timeout=60: mock.Mock(duration=durations.get(path, 0.0), codec="")


class TestProbeFlag(unittest.TestCase):
    def test_default_stays_an_empty_answer(self):
        for func in (probe.probe_chapters, probe.probe_streams):
            with mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED):
                with self.assertLogs("audiobind", level="WARNING"):
                    self.assertEqual(func("/x.m4b"), [], func.__name__)

    def test_raise_on_timeout_raises_a_dedicated_error_and_still_logs(self):
        for func in (probe.probe_chapters, probe.probe_streams):
            with mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED):
                with self.assertLogs("audiobind", level="WARNING") as logs:
                    with self.assertRaises(ProbeTimeout) as caught:
                        func("/x.m4b", timeout=7, raise_on_timeout=True)
            self.assertEqual((caught.exception.path, caught.exception.timeout), ("/x.m4b", 7), func.__name__)
            self.assertIn("timed out", logs.output[0], func.__name__)

    def test_an_answer_is_never_a_timeout_even_when_empty(self):
        with mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(stdout='{"chapters": []}')):
            self.assertEqual(probe.probe_chapters("/x.m4b", raise_on_timeout=True), [])
        with mock.patch.object(probe.subprocess, "run", return_value=mock.Mock(stdout="not json")):
            self.assertEqual(probe.probe_chapters("/x.m4b", raise_on_timeout=True), [])


class TestEncodeSide(unittest.TestCase):
    def test_a_source_timeout_raises_naming_the_file_and_the_cap(self):
        u = unit("/src/a.mp3", "/src/b.mp3")
        durations = {"/src/a.mp3": 300.0, "/src/b.mp3": 300.0}

        def chapters(path, timeout=probe.LIST_PROBE_TIMEOUT, raise_on_timeout=False):
            if path == "/src/b.mp3":  # the second file hangs: the first one's chapters must not be used alone
                raise ProbeTimeout(path, timeout, "-show_chapters")
            return TAGGED

        with mock.patch.object(encode, "probe", fake_probe(durations)), \
                mock.patch.object(encode, "probe_chapters", side_effect=chapters):
            with self.assertRaises(RuntimeError) as caught:
                encode.build_chapters([], 600.0, u, durations)
        message = str(caught.exception)
        self.assertIn("/src/b.mp3", message)
        self.assertIn(f"{probe.LIST_PROBE_TIMEOUT}s", message)

    def test_the_real_probe_timing_out_fails_plan_chapters(self):
        u = unit("/src/book.m4b")
        with mock.patch.object(encode, "probe", fake_probe({"/src/book.m4b": 300.0})), \
                mock.patch.object(encode.catalog, "chapters", return_value=[]), \
                mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED), \
                self.assertLogs("audiobind", level="WARNING"):  # B2's log line is kept
            with self.assertRaises(RuntimeError) as caught:
                encode.plan_chapters(u, "B000000001")
        self.assertIn("/src/book.m4b", str(caught.exception))

    def test_a_file_with_no_chapter_atoms_still_converts(self):
        u = unit("/src/a.mp3")
        answered = mock.Mock(stdout="{}")  # ffprobe answered promptly: there are simply no chapters
        with mock.patch.object(encode, "probe", fake_probe({"/src/a.mp3": 300.0})), \
                mock.patch.object(encode.catalog, "chapters", return_value=[]), \
                mock.patch.object(probe.subprocess, "run", return_value=answered):
            plan = encode.plan_chapters(u, "B000000001")
        self.assertIn("per-file", plan.source)
        self.assertEqual(len(plan.chapters), 1)

    def test_a_timeout_is_not_swallowed_when_audnex_could_have_filled_in(self):
        # even with Audnex data on hand the book fails: silently choosing another tier would hide the sick disk
        u = unit("/src/a.m4b")
        audnex = [{"lengthMs": 300000, "title": "One"}]
        with mock.patch.object(encode, "probe", fake_probe({"/src/a.m4b": 300.0})), \
                mock.patch.object(probe.subprocess, "run", side_effect=EXPIRED), \
                self.assertLogs("audiobind", level="WARNING"):
            with self.assertRaises(RuntimeError):
                encode.build_chapters(audnex, 300.0, u)


class TestBookFailsThroughThePipeline(PipelineCase):
    def test_a_timed_out_source_is_a_failed_book_with_the_reason(self):
        self.add_torrent()

        def hung(path, timeout=probe.LIST_PROBE_TIMEOUT, raise_on_timeout=False):
            raise ProbeTimeout(path, timeout, "-show_chapters")

        self._scan_with(hung)
        row = self.state.get(KEY)
        self.assertEqual(row.status, "failed")
        self.assertIn("timed out", row.note)
        self.assertIn(".mp3", row.note)
        self.assertEqual(self.encode_calls, [], "nothing was encoded")

    def test_an_answered_but_empty_source_still_converts(self):
        self.add_torrent()
        summary = self._scan_with(lambda path, timeout=probe.LIST_PROBE_TIMEOUT, raise_on_timeout=False: [])
        self.assertEqual(summary["converted"], 1)
        self.assertEqual(self.state.get(KEY).status, "converted")

    def _scan_with(self, chapters):
        with (
            mock.patch.object(catalog, "search", return_value=[PHM]),
            mock.patch.object(catalog, "book", return_value={"title": "Project Hail Mary",
                                                              "authors": [{"name": "Andy Weir"}]}),
            mock.patch.object(catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline, "probe", return_value=Probe(duration=3600.0, codec="mp3", channels="2")),
            mock.patch.object(encode, "probe_chapters", side_effect=chapters),
            mock.patch.object(encode, "encode", side_effect=self.fake_encode),
        ):
            return daemon.scan_once(self.state)


if __name__ == "__main__":
    unittest.main()

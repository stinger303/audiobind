"""v0.14.4 encode fixes: chapter order, Audnex offsets, stderr join, one chapter plan."""

import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from audiobind import encode
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part, audio_files


def fake_probe(durations):
    def probe(path, timeout=60):
        class Info:
            duration = durations.get(str(path), 0.0)
            codec = ""

        return Info()

    return probe


class TestChapterOrder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        names = [f"Track {n}.mp3" for n in range(1, 11)]  # unpadded: 1, 2, ..., 10
        for name in names:
            open(os.path.join(self.dir.name, name), "wb").close()
        files = audio_files(self.dir.name)  # the concat order
        self.unit = BookUnit(path=self.dir.name, name="Book", parent="", parts=[Part("Book", files)])
        # every file a different length, so a misordered timeline shows in the boundaries
        self.durations = {f: 100.0 + i for i, f in enumerate(files)}

    def tearDown(self):
        self.dir.cleanup()

    def test_concat_order_is_natural(self):
        self.assertEqual(
            [os.path.basename(f) for f in self.unit.files][:3], ["Track 1.mp3", "Track 2.mp3", "Track 3.mp3"]
        )
        self.assertEqual(os.path.basename(self.unit.files[-1]), "Track 10.mp3")

    def test_per_file_chapters_follow_play_order(self):
        with mock.patch.object(encode, "probe_chapters", return_value=[]):
            chapters, label = encode.build_chapters([], sum(self.durations.values()), self.unit, self.durations)
        self.assertIn("per-file", label)
        self.assertEqual([c["title"] for c in chapters], [f"Track {n}" for n in range(1, 11)])
        # boundaries come from the right file: Track 2 is 101s long and starts at 100
        self.assertEqual(chapters[1]["start"], 100.0)
        self.assertEqual(chapters[1]["end"], 201.0)
        self.assertEqual(chapters[-1]["end"], sum(self.durations.values()))

    def test_source_chapters_land_at_play_order_offsets(self):
        def tagged(path):
            title = os.path.splitext(os.path.basename(path))[0]
            return [
                {"start_time": "0", "end_time": "50", "tags": {"title": title + " a"}},
                {"start_time": "50", "end_time": "100", "tags": {"title": title + " b"}},
            ]

        with mock.patch.object(encode, "probe_chapters", side_effect=tagged):
            chapters = encode._source_chapters(self.unit, self.durations)
        self.assertEqual(chapters[2]["title"], "Track 2 a")
        self.assertEqual(chapters[2]["start"], 100.0)
        self.assertEqual(chapters[-2]["title"], "Track 10 a")

    def test_files_are_not_resorted_by_basename(self):
        with (
            mock.patch.object(encode, "probe", fake_probe(self.durations)),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
        ):
            plan = encode.plan_chapters(self.unit, "B000000001")
        self.assertEqual(plan.chapters[9]["title"], "Track 10")


class TestAudnexOffsets(unittest.TestCase):
    def test_start_offsets_are_honoured(self):
        # a 10s brand intro before chapter 1: starts must not be a running sum of lengths
        data = [
            {"startOffsetMs": 10000, "lengthMs": 100000, "title": "One"},
            {"startOffsetMs": 110000, "lengthMs": 100000, "title": "Two"},
            {"startOffsetMs": 210000, "lengthMs": 90000, "title": "Three"},
        ]
        chapters = encode._audnex_chapters(data, 300.0)
        self.assertEqual([c["start"] for c in chapters], [10.0, 110.0, 210.0])
        self.assertEqual(chapters[-1]["end"], 300.0)

    def test_offsets_are_scaled_to_the_measured_runtime(self):
        data = [
            {"startOffsetMs": 0, "lengthMs": 100000, "title": "One"},
            {"startOffsetMs": 150000, "lengthMs": 50000, "title": "Two"},  # 50s gap before Two
        ]
        chapters = encode._audnex_chapters(data, 220.0)  # runtime 10% longer than catalog
        self.assertAlmostEqual(chapters[1]["start"], 165.0)
        self.assertEqual(chapters[0]["end"], chapters[1]["start"])  # the gap stays with One

    def test_missing_offsets_fall_back_to_cumulative_lengths(self):
        data = [{"lengthMs": 100000, "title": "One"}, {"lengthMs": 200000, "title": "Two"}]
        chapters = encode._audnex_chapters(data, 300.0)
        self.assertEqual([c["start"] for c in chapters], [0.0, 100.0])


class FakeProc:
    def __init__(self, stderr_delay=0.0, stderr_text="", returncode=1):
        self.returncode = returncode
        self.stdout = []
        delay = stderr_delay

        class Err:
            def read(self_inner):
                time.sleep(delay)
                return stderr_text

        self.stderr = Err()

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class TestEncodeRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.work = pathlib.Path(self.dir.name) / "work"
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.plan = encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": 60.0, "title": "a"}], source="per-file (1 chapters)",
            durations={"/src/a.mp3": 60.0},
        )
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def tearDown(self):
        self.dir.cleanup()

    def run_encode(self, proc, plan):
        with (
            mock.patch.object(encode.subprocess, "Popen", return_value=proc),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
        ):
            return encode.encode(self.unit, self.match, {}, self.work, self.work / "out.m4b", log=lambda *_: None, plan=plan)

    def test_ffmpeg_error_text_is_not_lost_to_a_slow_stderr_reader(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(FakeProc(stderr_delay=0.3, stderr_text="Invalid data found"), self.plan)
        self.assertIn("Invalid data found", str(ctx.exception))

    def test_a_passed_plan_is_used_without_reprobing(self):
        with (
            mock.patch.object(encode, "build_chapters", side_effect=AssertionError("planned twice")) as build,
            mock.patch.object(encode, "probe", side_effect=AssertionError("probed twice")) as probe,
            self.assertRaises(RuntimeError),
        ):
            self.run_encode(FakeProc(stderr_text="x"), self.plan)
        build.assert_not_called()
        probe.assert_not_called()

    def test_convert_unit_plans_once_with_the_audit_durations(self):
        from audiobind import pipeline

        audit = pipeline.Audit(unit=self.unit)
        audit.match = mock.Mock(asin="B000000001", product=mock.Mock(authors=["A"], title="T"))
        audit.durations = {"/src/a.mp3": 60.0}
        with (
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "T"}),
            mock.patch.object(pipeline.library, "m4b_path", return_value=self.work / "nope" / "T.m4b"),
            mock.patch.object(pipeline.CONFIG, "work_dir", self.work),
            mock.patch.object(pipeline.CONTROL, "_dry_run", False),
            mock.patch.object(encode, "probe", side_effect=AssertionError("probed again")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=RuntimeError("stop here")) as enc,
            self.assertRaises(RuntimeError),
        ):
            pipeline.convert_unit(audit, log=lambda *_: None)
        plan = enc.call_args.kwargs["plan"]
        self.assertEqual(plan.durations, {"/src/a.mp3": 60.0})


if __name__ == "__main__":
    unittest.main()

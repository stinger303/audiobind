"""encode.py correctness: the duration check, atomic publish, stderr handling, chapters."""

import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from audiobind import encode
from audiobind.control import CONTROL, Stopped
from audiobind.units import BookUnit, Part


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = []
        self.stderr = None

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class EncodeHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.work = self.root / "work"
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def plan(self, total):
        return encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": total, "title": "a"}],
            source="per-file (1 chapters)",
            durations={"/src/a.mp3": total},
        )

    def run_encode(self, total=60.0, output=60.0, popen=None, download=None, url="https://m.media-amazon.com/c.jpg"):
        def default_popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return FakeProc()

        def default_download(u, dest):
            pathlib.Path(dest).write_bytes(b"j" * 4000)
            return True

        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen or default_popen),
            mock.patch.object(encode.catalog, "download", side_effect=download or default_download) as dl,
            mock.patch.object(encode.catalog, "cover_url", return_value=url),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=output)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": "aac"}]),
            mock.patch.object(encode, "has_embedded_cover", return_value=True),
        ):
            result = encode.encode(
                self.unit, self.match, {"title": "T"}, self.work, self.root / "out" / "Book.m4b",
                log=lambda *_: None, plan=self.plan(total), publish=False,
            )
        self.download_mock = dl
        return result


# ---------------------------------------------------------------- 1. duration check
class DurationCheckTests(EncodeHarness):
    def test_fifteen_minutes_short_of_ten_hours_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(total=600 * 60.0, output=585 * 60.0)
        self.assertIn("files were dropped during the merge", str(ctx.exception))

    def test_two_minutes_short_of_ten_hours_is_rejected(self):
        # passed under the old max(120, 2%) rule (12 minutes of slack)
        with self.assertRaises(RuntimeError):
            self.run_encode(total=600 * 60.0, output=598 * 60.0)

    def test_a_second_or_two_of_container_rounding_passes(self):
        for output in (600 * 60.0 - 1.7, 600 * 60.0 + 1.2):
            with self.subTest(output=output):
                result = self.run_encode(total=600 * 60.0, output=output)
                self.assertEqual(result.duration, output)

    def test_short_book_keeps_the_thirty_second_floor(self):
        self.run_encode(total=600.0, output=575.0)
        with self.assertRaises(RuntimeError):
            self.run_encode(total=600.0, output=565.0)


# ---------------------------------------------------------------- 3. stderr cannot block ffmpeg
class StderrTests(EncodeHarness):
    """A real child process writes megabytes to stderr and nobody reads it while it runs."""

    def real_child(self, script):
        real_popen = subprocess.Popen

        def popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return real_popen([sys.executable, "-c", script], **kw)

        return popen

    def run_with_timeout(self, **kwargs):
        outcome = {}

        def target():
            try:
                outcome["result"] = self.run_encode(**kwargs)
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                outcome["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "encode hung on a full stderr pipe")
        return outcome

    def test_megabytes_of_stderr_do_not_hang_and_the_tail_is_reported(self):
        script = (
            "import sys\n"
            "for i in range(40000): sys.stderr.write('frame=%d chatty ffmpeg line\\n' % i)\n"
            "sys.stderr.write('[aac] the real reason at the end\\n')\n"
            "sys.exit(1)\n"
        )
        outcome = self.run_with_timeout(popen=self.real_child(script))
        error = outcome.get("error")
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("ffmpeg failed:", str(error))
        self.assertIn("[aac] the real reason at the end", str(error))

    def test_a_successful_chatty_run_returns(self):
        script = "import sys\nsys.stderr.write('x' * 2_000_000)\n"
        outcome = self.run_with_timeout(popen=self.real_child(script))
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"].duration, 60.0)

    def test_ffmpeg_hint_still_reaches_the_duration_error(self):
        def popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            kw["stderr"].write(b"frame=1\n[mp3] Invalid data found when processing input\n")
            return FakeProc()

        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(total=3600.0, output=3000.0, popen=popen)
        self.assertIn("ffmpeg: [mp3] Invalid data found", str(ctx.exception))

    def test_operator_stop_still_stops(self):
        self.addCleanup(CONTROL.resume)
        self.addCleanup(CONTROL.clear_stop)
        script = "import sys, time\nsys.stderr.write('y' * 500_000)\nsys.stderr.flush()\ntime.sleep(60)\n"
        stopper = threading.Timer(1.0, CONTROL.stop)
        stopper.start()
        self.addCleanup(stopper.cancel)
        outcome = self.run_with_timeout(popen=self.real_child(script))
        self.assertIsInstance(outcome.get("error"), Stopped)


# ---------------------------------------------------------------- 5. no zero-length chapters
class ZeroLengthChapterTests(unittest.TestCase):
    def assert_contiguous(self, chapters, duration):
        self.assertTrue(all(c["end"] > c["start"] for c in chapters), chapters)
        self.assertEqual(chapters[0]["start"], 0.0)
        for current, following in zip(chapters, chapters[1:]):
            self.assertEqual(current["end"], following["start"])
        self.assertEqual(chapters[-1]["end"], duration)

    def test_audnex_zero_length_entry_is_dropped(self):
        data = [{"lengthMs": 100000, "title": "A"}, {"lengthMs": 0, "title": "Credits"}, {"lengthMs": 200000, "title": "B"}]
        chapters = encode._audnex_chapters(data, 300.0)
        self.assertEqual([c["title"] for c in chapters], ["A", "B"])
        self.assert_contiguous(chapters, 300.0)

    def test_audnex_two_chapters_on_one_offset(self):
        data = [
            {"startOffsetMs": 0, "lengthMs": 100000, "title": "A"},
            {"startOffsetMs": 100000, "lengthMs": 0, "title": "X"},
            {"startOffsetMs": 100000, "lengthMs": 200000, "title": "B"},
        ]
        chapters = encode._audnex_chapters(data, 300.0)
        self.assertEqual([c["title"] for c in chapters], ["A", "B"])
        self.assert_contiguous(chapters, 300.0)

    def test_audnex_zero_length_last_entry(self):
        data = [{"lengthMs": 300000, "title": "A"}, {"lengthMs": 0, "title": "End"}]
        chapters = encode._audnex_chapters(data, 300.0)
        self.assertEqual([c["title"] for c in chapters], ["A"])
        self.assert_contiguous(chapters, 300.0)

    def test_per_file_zero_duration_file_gets_no_chapter(self):
        unit = BookUnit(path="/s", name="B", parent="", parts=[Part("B", ["/s/1.mp3", "/s/2.mp3", "/s/3.mp3"])])
        durations = {"/s/1.mp3": 100.0, "/s/2.mp3": 0.0, "/s/3.mp3": 100.0}
        with mock.patch.object(encode, "probe_chapters", return_value=[]):
            chapters, source = encode.build_chapters([], 200.0, unit, durations)
        self.assertEqual([c["title"] for c in chapters], ["1", "3"])
        self.assertEqual(source, "per-file (2 chapters)")
        self.assert_contiguous(chapters, 200.0)

    def test_embedded_tag_past_the_file_end_is_dropped(self):
        unit = BookUnit(path="/s", name="B", parent="", parts=[Part("B", ["/s/1.m4b"])])
        tags = [
            {"start_time": "0", "end_time": "50"},
            {"start_time": "50", "end_time": "100"},
            {"start_time": "120", "end_time": "130", "tags": {"title": "ghost"}},
        ]
        with mock.patch.object(encode, "probe_chapters", return_value=tags):
            chapters = encode._source_chapters(unit, {"/s/1.m4b": 100.0})
        self.assertEqual([c["title"] for c in chapters], ["Chapter 1", "Chapter 2"])
        self.assert_contiguous(chapters, 100.0)

    def test_sub_millisecond_chapter_is_dropped(self):
        # START/END are written as whole ms; 0.0004 s would become START == END
        data = [{"lengthMs": 100000, "title": "A"}, {"lengthMs": 0.4, "title": "blip"}, {"lengthMs": 200000, "title": "B"}]
        chapters = encode._audnex_chapters(data, 300.0004)
        self.assertNotIn("blip", [c["title"] for c in chapters])

    def test_ordinary_plans_are_unchanged(self):
        data = [{"lengthMs": 100000, "title": "One"}, {"lengthMs": 200000, "title": "Two"}]
        self.assertEqual(
            encode._audnex_chapters(data, 300.0),
            [{"start": 0.0, "end": 100.0, "title": "One"}, {"start": 100.0, "end": 300.0, "title": "Two"}],
        )


# ---------------------------------------------------------------- 2. atomic publish
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = pathlib.Path(self.dir.name)
        self.work = root / "work"
        self.work.mkdir()
        self.library = root / "library" / "Author" / "Book"
        self.out = self.library / "Book.m4b"
        self.audio = self.work / "output.m4b"
        self.audio.write_bytes(b"a" * 300_000)
        self.cover = self.work / "cover.jpg"
        self.cover.write_bytes(b"c" * 5000)
        self.result = encode.EncodeResult(
            path=self.audio, duration=60.0, size=300_000, chapters=1, chapters_source="x", cover=self.cover
        )
        real_replace = encode.os.replace

        def cross_device_replace(src, dst):
            # the work dir is "another filesystem": only renames inside the library work
            if pathlib.Path(src).parent != pathlib.Path(dst).parent:
                raise OSError(encode.errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst)

        self.cross_device = mock.patch.object(encode.os, "replace", side_effect=cross_device_replace)

    def test_same_filesystem_is_a_rename(self):
        with mock.patch.object(encode.shutil, "copyfileobj", wraps=encode.shutil.copyfileobj) as copy:
            result = encode.publish_result(self.result, self.out)
        self.assertEqual(result.path, self.out)
        self.assertEqual(self.out.stat().st_size, 300_000)
        self.assertFalse(self.audio.exists())
        self.assertEqual((self.library / "cover.jpg").read_bytes(), b"c" * 5000)
        self.assertEqual(copy.call_count, 1)  # only the cover is copied; the audio was renamed

    def test_cross_filesystem_publish_writes_the_whole_file(self):
        with self.cross_device:
            result = encode.publish_result(self.result, self.out)
        self.assertEqual(result.size, 300_000)
        self.assertEqual(self.out.read_bytes(), b"a" * 300_000)
        self.assertFalse(self.audio.exists())
        self.assertTrue((self.library / "cover.jpg").exists())
        self.assertEqual(sorted(p.name for p in self.library.iterdir()), ["Book.m4b", "cover.jpg"])

    def test_a_copy_that_dies_midway_leaves_nothing_in_the_library(self):
        def dying_copy(src, dst, length=0):
            dst.write(src.read(100_000))
            raise OSError(28, "No space left on device")

        with self.cross_device, mock.patch.object(encode.shutil, "copyfileobj", side_effect=dying_copy):
            with self.assertRaises(OSError):
                encode.publish_result(self.result, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.library.iterdir()), [])  # no stray temp file, no cover
        self.assertEqual(self.audio.stat().st_size, 300_000)  # the source survives for a retry

    def test_an_old_file_is_replaced_whole_or_not_at_all(self):
        self.library.mkdir(parents=True)
        self.out.write_bytes(b"old")

        def dying_copy(src, dst, length=0):
            dst.write(src.read(10))
            raise OSError(5, "Input/output error")

        with self.cross_device, mock.patch.object(encode.shutil, "copyfileobj", side_effect=dying_copy):
            with self.assertRaises(OSError):
                encode.publish_result(self.result, self.out)
        self.assertEqual(self.out.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.library.iterdir()], ["Book.m4b"])

    def test_cover_is_written_only_after_the_audio(self):
        order = []
        real = encode._place

        def spy(source, dest, move):
            order.append((dest.name, self.out.exists()))
            return real(source, dest, move)

        with mock.patch.object(encode, "_place", side_effect=spy):
            encode.publish_result(self.result, self.out)
        self.assertEqual(order, [("Book.m4b", False), ("cover.jpg", True)])


if __name__ == "__main__":
    unittest.main()

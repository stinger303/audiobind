"""CORE-01d: verification compares the sources' audio with the output's audio, like with like.

The source side summed *container* durations; the output side measures the mapped *audio stream*.
A valid MP4 whose video runs 5 s past its audio (65 s container, 60 s AAC) came out of the project's
own AAC-copy remux as 60.023 s of complete audio - and was rejected as dropped audio, because the
CORE-01 slack had been cut to 2 s plus 0.05 s a file.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import encode, probe
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ffprobe_json(fmt_duration, streams):
    return mock.Mock(stdout=json.dumps({"format": {"duration": str(fmt_duration)}, "streams": streams}))


class ProbeDurationTests(unittest.TestCase):
    def test_the_audio_streams_length_is_the_files_length(self):
        streams = [{"codec_type": "audio", "codec_name": "aac", "duration": "60.000000"},
                   {"codec_type": "video", "codec_name": "mpeg4", "duration": "65.000000"}]
        with mock.patch.object(probe.subprocess, "run", return_value=ffprobe_json(65.0, streams)):
            self.assertEqual(probe.probe("/src/a.m4b").duration, 60.0)

    def test_the_first_audio_stream_counts_even_behind_a_cover(self):
        streams = [{"codec_type": "video", "codec_name": "mjpeg", "duration": "0.04"},
                   {"codec_type": "audio", "codec_name": "mp3", "duration": "600.5"}]
        with mock.patch.object(probe.subprocess, "run", return_value=ffprobe_json(601.0, streams)):
            self.assertEqual(probe.probe("/src/a.mp3").duration, 600.5)

    def test_no_stream_duration_falls_back_to_the_container(self):
        for streams in ([{"codec_type": "audio", "codec_name": "opus"}],
                        [{"codec_type": "audio", "codec_name": "opus", "duration": "N/A"}],
                        [{"codec_name": "mp3"}]):
            with self.subTest(streams=streams), \
                    mock.patch.object(probe.subprocess, "run", return_value=ffprobe_json(321.0, streams)):
                self.assertEqual(probe.probe("/src/a.ogg").duration, 321.0)


class SlackTests(unittest.TestCase):
    def test_short_books_get_a_real_floor_again(self):
        # 2.05 s at 65 s was the regression; the floor is loosened but stays under the 15 s a
        # truncated 140 s encode loses
        self.assertGreaterEqual(encode.duration_slack(65.0, 1), 10.0)
        self.assertLess(encode.duration_slack(140.0, 2), 15.0)
        self.assertLess(encode.duration_slack(600.0, 1), 25.0)

    def test_long_books_keep_the_ratio(self):
        self.assertAlmostEqual(encode.duration_slack(40 * 3600.0, 40), 40 * 3600.0 * 0.002)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RemuxRealTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        CONTROL.clear_stop()

    def test_an_mp4_whose_video_outlasts_its_audio_remuxes_and_verifies(self):
        """(a) 65 s container, 60 s complete AAC: the AAC copy keeps every sample and passes."""
        source = self.root / "book.m4b"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=60", "-f", "lavfi",
             "-i", "color=c=black:s=64x64:r=5:d=65", "-map", "0:a", "-map", "1:v", "-c:a", "aac", "-b:a", "64k",
             "-ac", "2", "-ar", "44100", "-c:v", "mpeg4", str(source)],
            check=True, timeout=120,
        )
        fmt = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=60).stdout)["format"]
        self.assertGreater(float(fmt["duration"]), 64.0, "the fixture lost its long video stream")
        unit = BookUnit(path=str(self.root), name="Book", parent="", parts=[Part("Book", [str(source)])])
        match = mock.Mock(asin="B000000001", product=None)
        with (
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
        ):
            plan = encode.plan_chapters(unit, match.asin)
            result = encode.encode(unit, match, {"title": "T"}, self.root / "work", self.root / "o.m4b",
                                   log=lambda *_: None, copy_audio=True, plan=plan, publish=False)
        self.assertAlmostEqual(plan.total, 60.0, delta=0.1)
        self.assertAlmostEqual(result.duration, 60.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()

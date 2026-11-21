"""AB-MONO-REENC: a re-encode writes min(source, cap) for channels and rate, never the cap itself.

AB-MONO-CAP made AUDIO_CHANNELS and SAMPLE_RATE caps for the *decision* (a book within both is
copied). But a book that is re-encoded - above one cap, an mp3, a multi-file merge, the mp3 output -
was written with exactly the configured figures, so the axis that was *not* above its cap moved the
wrong way: a mono 48 kHz book came out stereo, a stereo 22050 Hz book under AUDIO_CHANNELS=1 came
out at 44100. Each axis is now min(source, cap), independently; for several files the source figure
is the lowest any file has (never upmix or raise the rate of any part).
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import encode, pipeline
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def unit_of(paths):
    paths = [str(p) for p in paths]
    return BookUnit(path=str(pathlib.Path(paths[0]).parent), name="Book", parent="",
                    parts=[Part("Book", paths)])


def caps(channels, rate):
    return (mock.patch.object(CONFIG, "audio_channels", channels),
            mock.patch.object(CONFIG, "sample_rate", rate))


CODEC_ARGS = {
    ".m4b": ["-c:a", "aac", "-b:a", "64k", "-f", "mp4"],
    ".m4a": ["-c:a", "aac", "-b:a", "64k", "-f", "mp4"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"],
    ".flac": ["-c:a", "flac", "-f", "flac"],
}


def make_source(path, channels, rate, seconds=20):
    path = pathlib.Path(path)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=d={seconds}", "-ac", str(channels),
         "-ar", str(rate), *CODEC_ARGS[path.suffix], str(path)],
        check=True, timeout=120,
    )


def stream_shape(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    return int(stream["channels"]), int(stream["sample_rate"])


class OutputShapeTests(unittest.TestCase):
    """The pure rule: each axis is min(lowest source figure, cap); unknown falls back to the cap."""

    def shape(self, channels, rates, cap=(2, 44100), codec="aac"):
        ch, sr = caps(*cap)
        with ch, sr:
            return encode.output_shape(channels, rates, codec)

    def test_each_axis_is_the_lower_of_source_and_cap(self):
        self.assertEqual(self.shape({"1"}, {"48000"}), (1, 44100))
        self.assertEqual(self.shape({"2"}, {"22050"}, cap=(1, 44100)), (1, 22050))
        self.assertEqual(self.shape({"2"}, {"48000"}), (2, 44100))
        self.assertEqual(self.shape({"6"}, {"96000"}), (2, 44100))
        self.assertEqual(self.shape({"1"}, {"22050"}), (1, 22050))

    def test_several_files_take_the_lowest_figure_of_any_file(self):
        self.assertEqual(self.shape({"1", "2"}, {"22050", "44100"}), (1, 22050))

    def test_an_unknown_figure_falls_back_to_the_cap(self):
        self.assertEqual(self.shape(None, None), (2, 44100))
        self.assertEqual(self.shape(set(), set()), (2, 44100))
        self.assertEqual(self.shape({"N/A"}, {"0"}), (2, 44100))
        # an axis is decided on its own: a known rate is kept while the channels are unknown
        self.assertEqual(self.shape(set(), {"22050"}), (2, 22050))
        # a bad figure among good ones is ignored, not allowed to sink the shape
        self.assertEqual(self.shape({"1", "x"}, {"22050", ""}), (1, 22050))

    def test_a_rate_the_encoder_cannot_write_steps_down_to_one_it_can(self):
        # 37800 Hz (CD-ROM XA) is neither an AAC nor an mp3 rate: the next one down, never up
        self.assertEqual(self.shape({"1"}, {"37800"}), (1, 32000))
        self.assertEqual(self.shape({"1"}, {"37800"}, codec="mp3"), (1, 32000))
        # 7350 is an AAC rate but not an mp3 one; below every mp3 rate the lowest is the only choice
        self.assertEqual(self.shape({"1"}, {"7350"}), (1, 7350))
        self.assertEqual(self.shape({"1"}, {"7350"}, codec="mp3"), (1, 8000))
        # 88200 is above the cap; the cap is a valid rate and wins
        self.assertEqual(self.shape({"2"}, {"88200"}), (2, 44100))


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RealReencodeTests(unittest.TestCase):
    """Real ffprobe in the audit, the real decision, the real encode, ffprobe on the output."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        CONTROL.clear_stop()

    def run_case(self, sources, cap, fmt="m4b"):
        """``sources`` is [(name, channels, rate)]; returns (audit, output path)."""
        paths = []
        for name, channels, rate in sources:
            path = self.root / "src" / name
            path.parent.mkdir(exist_ok=True)
            make_source(path, channels, rate)
            self.assertEqual(stream_shape(path), (channels, rate))
            paths.append(path)
        unit = unit_of(paths)
        match = pipeline.matching.Match(confidence="HIGH")
        ch, sr = caps(*cap)
        with (
            ch, sr,
            mock.patch.object(pipeline.matching, "match_unit", return_value=match),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
        ):
            audit = pipeline.audit_unit(unit)
            plan = encode.plan_chapters(unit, "B000000001")
            result = encode.encode(unit, mock.Mock(asin="B000000001", product=None), {"title": "T"},
                                   self.root / "work", self.root / f"out.{fmt}", log=lambda *_: None,
                                   copy_audio=(audit.action == "remux" and fmt == "m4b"), plan=plan,
                                   fmt=fmt, publish=False)
        return audit, result.path

    # --- the two directions from the brief -------------------------------------------------------

    def test_mono_48k_with_defaults_stays_mono_at_44100(self):
        audit, out = self.run_case([("book.m4b", 1, 48000)], (2, 44100))
        self.assertEqual(audit.action, "convert", "48 kHz is above the 44100 cap")
        self.assertEqual(stream_shape(out), (1, 44100), "a re-encode must not upmix a mono book")

    def test_stereo_22050_with_mono_cap_is_mono_at_22050(self):
        audit, out = self.run_case([("book.m4b", 2, 22050)], (1, 44100))
        self.assertEqual(audit.action, "convert", "2 channels is above the mono cap")
        self.assertEqual(stream_shape(out), (1, 22050), "a re-encode must not raise the rate")

    # --- above both caps: unchanged, still the cap -----------------------------------------------

    def test_stereo_48k_with_defaults_is_still_written_at_the_caps(self):
        audit, out = self.run_case([("book.m4b", 2, 48000)], (2, 44100))
        self.assertEqual(audit.action, "convert")
        self.assertEqual(stream_shape(out), (2, 44100))

    # --- every other re-encoding path ------------------------------------------------------------

    def test_a_single_mp3_converted_to_m4b_keeps_its_mono_22050(self):
        audit, out = self.run_case([("book.mp3", 1, 22050)], (2, 44100))
        self.assertEqual(audit.action, "convert")
        self.assertEqual(stream_shape(out), (1, 22050))

    def test_a_multi_file_mp3_merge_keeps_its_mono_22050(self):
        audit, out = self.run_case([("01.mp3", 1, 22050), ("02.mp3", 1, 22050)], (2, 44100))
        self.assertEqual(audit.action, "convert")
        self.assertEqual(stream_shape(out), (1, 22050))

    def test_the_mp3_output_keeps_a_mono_22050_source(self):
        audit, out = self.run_case([("01.mp3", 1, 22050), ("02.mp3", 1, 22050)], (2, 44100), fmt="mp3")
        self.assertEqual(stream_shape(out), (1, 22050))

    def test_the_mp3_output_of_a_mono_48k_book_is_mono_at_44100(self):
        audit, out = self.run_case([("book.m4b", 1, 48000)], (2, 44100), fmt="mp3")
        self.assertEqual(stream_shape(out), (1, 44100))

    def test_a_mixed_codec_join_takes_the_lowest_shape_of_any_part(self):
        # mp3 + aac goes through the PCM join (CORE-01); a stereo 44100 part is downmixed and
        # resampled to the mono 22050 part, never the mono part upmixed
        audit, out = self.run_case([("01.mp3", 1, 22050), ("02.m4a", 2, 44100)], (2, 44100))
        self.assertEqual(audit.action, "convert")
        self.assertEqual(stream_shape(out), (1, 22050))

    def test_process_units_plan_carries_the_audits_shape_without_probing_again(self):
        # process_unit hands the audit's probe to plan_chapters; the shape must ride along with it
        paths = [self.root / "01.mp3", self.root / "02.mp3"]
        for path in paths:
            make_source(path, 1, 22050)
        unit = unit_of(paths)
        audit = pipeline.Audit(unit=unit, durations={str(p): 20.0 for p in paths}, codecs={"mp3"},
                               channels={"1"}, sample_rates={"22050"})
        with (
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode, "probe", side_effect=AssertionError("probed again")),
        ):
            plan = pipeline._chapter_plan(unit, "B000000001", audit)
        self.assertEqual((plan.channels, plan.sample_rates), ({"1"}, {"22050"}))

    def test_a_rate_aac_cannot_write_steps_down_instead_of_failing(self):
        audit, out = self.run_case([("book.flac", 1, 37800)], (2, 44100))
        self.assertEqual(stream_shape(out), (1, 32000))


if __name__ == "__main__":
    unittest.main()

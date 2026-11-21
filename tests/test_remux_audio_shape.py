"""AB-MONO: AUDIO_CHANNELS / SAMPLE_RATE were silently ignored on the remux path.

choose_action() returned ``remux`` for every single AAC .m4b/.m4a, whatever its channel count or
sample rate. A remux is ``-c:a copy``, and ffmpeg ignores ``-ac``/``-ar`` next to a stream copy (exit
0, no warning), so a stereo book with AUDIO_CHANNELS=1 came out stereo, and a 48 kHz book with
SAMPLE_RATE=44100 came out 48 kHz. The copy is now chosen only when the probed shape already matches
the configured one; otherwise the book is re-encoded, which is the only path that applies -ac/-ar.
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
from audiobind.pipeline import choose_action
from audiobind.units import BookUnit, Part

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def one_file(path):
    return BookUnit(path=str(pathlib.Path(path).parent), name="Book", parent="",
                    parts=[Part("Book", [str(path)])])


def shape(config_channels, config_rate):
    return (mock.patch.object(CONFIG, "audio_channels", config_channels),
            mock.patch.object(CONFIG, "sample_rate", config_rate))


class ChooseActionShapeTests(unittest.TestCase):
    """The pure decision: remux only when the probed shape is already the configured one."""

    def decide(self, channels, rates, config_channels=2, config_rate=44100, path="/l/a.m4b"):
        ch, sr = shape(config_channels, config_rate)
        with ch, sr:
            return choose_action(one_file(path), "HIGH", True, {"aac"},
                                 channels=channels, sample_rates=rates)

    def test_stereo_source_with_mono_configured_is_re_encoded(self):
        self.assertEqual(self.decide({"2"}, {"44100"}, config_channels=1), "convert")

    def test_mono_source_with_stereo_configured_is_re_encoded(self):
        self.assertEqual(self.decide({"1"}, {"44100"}, config_channels=2), "convert")

    def test_48k_source_with_44k_configured_is_re_encoded(self):
        self.assertEqual(self.decide({"2"}, {"48000"}, config_rate=44100), "convert")

    def test_matching_shape_keeps_the_fast_path(self):
        self.assertEqual(self.decide({"2"}, {"44100"}), "remux")
        self.assertEqual(self.decide({"1"}, {"22050"}, config_channels=1, config_rate=22050), "remux")
        self.assertEqual(self.decide({"2"}, {"44100"}, path="/l/a.m4a"), "remux")

    def test_an_unprobed_shape_is_never_assumed_to_match(self):
        # probed, but ffprobe gave no channel count / rate: the copy cannot be shown to be right
        self.assertEqual(self.decide(set(), {"44100"}), "convert")
        self.assertEqual(self.decide({"2"}, set()), "convert")

    def test_the_shape_gates_only_the_copy(self):
        ch, sr = shape(1, 44100)
        with ch, sr:
            self.assertEqual(choose_action(one_file("/l/a.m4b"), "LOW", True, {"aac"},
                                           channels={"2"}, sample_rates={"44100"}), "review")
            self.assertEqual(choose_action(one_file("/l/a.m4b"), "HIGH", False, {"aac"},
                                           channels={"2"}, sample_rates={"44100"}), "hold")


def make_source(path, channels, rate, seconds=20):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:a", "aac", "-b:a", "64k",
         "-ac", str(channels), "-ar", str(rate), "-f", "mp4", str(path)],
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


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RealAuditAndEncodeTests(unittest.TestCase):
    """Real ffprobe in the audit, the real decision, the real encode, ffprobe on the output."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        CONTROL.clear_stop()

    def audit(self, source):
        match = pipeline.matching.Match(confidence="HIGH")
        with (
            mock.patch.object(pipeline.matching, "match_unit", return_value=match),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
        ):
            return pipeline.audit_unit(one_file(source))

    def encode(self, source, audit):
        unit = audit.unit
        match = mock.Mock(asin="B000000001", product=None)
        with (
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
        ):
            plan = encode.plan_chapters(unit, match.asin)
            # the same expression process_unit uses to turn the action into the encode mode; with
            # publish=False the verified file stays in the work dir and result.path names it
            result = encode.encode(unit, match, {"title": "T"}, self.root / "work", self.root / "out.m4b",
                                   log=lambda *_: None, copy_audio=(audit.action == "remux"), plan=plan,
                                   publish=False)
        return result.path

    def test_stereo_m4b_with_audio_channels_1_comes_out_mono(self):
        source = self.root / "book.m4b"
        make_source(source, 2, 44100)
        self.assertEqual(stream_shape(source), (2, 44100))
        ch, sr = shape(1, 44100)
        with ch, sr:
            audit = self.audit(source)
            self.assertNotEqual(audit.action, "remux", "a stereo copy cannot honour AUDIO_CHANNELS=1")
            out = self.encode(source, audit)
        self.assertEqual(stream_shape(out), (1, 44100))

    def test_48k_m4b_with_sample_rate_44100_comes_out_44100(self):
        source = self.root / "book.m4b"
        make_source(source, 2, 48000)
        ch, sr = shape(2, 44100)
        with ch, sr:
            audit = self.audit(source)
            self.assertNotEqual(audit.action, "remux", "a 48 kHz copy cannot honour SAMPLE_RATE=44100")
            out = self.encode(source, audit)
        self.assertEqual(stream_shape(out), (2, 44100))

    def test_an_m4b_already_in_the_configured_shape_is_still_copied(self):
        source = self.root / "book.m4b"
        make_source(source, 1, 22050)
        ch, sr = shape(1, 22050)
        with ch, sr:
            audit = self.audit(source)
            self.assertEqual(audit.action, "remux")
            out = self.encode(source, audit)
        self.assertEqual(stream_shape(out), (1, 22050))


if __name__ == "__main__":
    unittest.main()

"""AB-MONO / AB-MONO-CAP: AUDIO_CHANNELS and SAMPLE_RATE on the remux path are caps, not targets.

A remux is ``-c:a copy``, and ffmpeg ignores ``-ac``/``-ar`` next to a stream copy (exit 0, no
warning). AB-MONO stopped copying a stereo book under AUDIO_CHANNELS=1 and a 48 kHz book under
SAMPLE_RATE=44100, but did it by copying only an *exact* match, which re-encoded every mono, 22050 Hz
or 48 kHz book at the defaults. AB-MONO-CAP: the copy is kept whenever the source is at or below
both settings (never upmix, never raise a rate) and re-encoded only when it exceeds one.
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
    """The pure decision: remux while the probed shape is within both caps, re-encode above one."""

    def decide(self, channels, rates, config_channels=2, config_rate=44100, path="/l/a.m4b"):
        ch, sr = shape(config_channels, config_rate)
        with ch, sr:
            return choose_action(one_file(path), "HIGH", True, {"aac"},
                                 channels=channels, sample_rates=rates)

    def test_mono_source_with_stereo_configured_is_copied_not_upmixed(self):
        self.assertEqual(self.decide({"1"}, {"44100"}, config_channels=2), "remux")

    def test_22050_source_with_44100_configured_is_copied_not_resampled_up(self):
        self.assertEqual(self.decide({"2"}, {"22050"}, config_rate=44100), "remux")

    def test_a_source_below_both_caps_is_copied(self):
        self.assertEqual(self.decide({"1"}, {"22050"}, config_channels=2, config_rate=44100), "remux")
        self.assertEqual(self.decide({"1"}, {"16000"}, config_channels=2, config_rate=48000), "remux")

    def test_a_source_at_both_caps_is_copied(self):
        self.assertEqual(self.decide({"2"}, {"44100"}), "remux")
        self.assertEqual(self.decide({"1"}, {"22050"}, config_channels=1, config_rate=22050), "remux")
        self.assertEqual(self.decide({"2"}, {"44100"}, path="/l/a.m4a"), "remux")

    def test_stereo_source_with_mono_configured_is_re_encoded(self):
        self.assertEqual(self.decide({"2"}, {"44100"}, config_channels=1), "convert")
        self.assertEqual(self.decide({"6"}, {"44100"}, config_channels=2), "convert")

    def test_48k_source_with_44k_configured_is_re_encoded(self):
        self.assertEqual(self.decide({"2"}, {"48000"}, config_rate=44100), "convert")
        # one cap exceeded is enough, even while the other figure is below its cap
        self.assertEqual(self.decide({"1"}, {"48000"}, config_channels=2, config_rate=44100), "convert")
        self.assertEqual(self.decide({"2"}, {"22050"}, config_channels=1, config_rate=44100), "convert")

    def test_an_unprobed_shape_is_never_assumed_within_the_caps(self):
        # probed, but ffprobe gave no channel count / rate (or nonsense): the copy cannot be shown
        # to be right, so the conservative answer is a re-encode
        self.assertEqual(self.decide(set(), {"44100"}), "convert")
        self.assertEqual(self.decide({"2"}, set()), "convert")
        self.assertEqual(self.decide({"2"}, {"N/A"}), "convert")
        self.assertEqual(self.decide({"0"}, {"44100"}), "convert")

    def test_without_a_probe_the_four_argument_call_still_works(self):
        # the offline match bench calls choose_action with four arguments and has no audio
        ch, sr = shape(1, 22050)
        with ch, sr:
            self.assertEqual(choose_action(one_file("/l/a.m4b"), "HIGH", True, {"aac"}), "remux")

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


def audio_md5(path):
    """MD5 of the audio stream's packets as stored (``-c copy``, nothing decoded): equal for source
    and output only when the encoded bitstream was carried over untouched."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return out.strip()


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RealAuditAndEncodeTests(unittest.TestCase):
    """Real ffprobe in the audit, the real decision, the real encode, ffprobe on the output."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        root = mock.patch.object(CONFIG, "library_root", self.root)
        root.start()
        self.addCleanup(root.stop)
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

    def run_case(self, source_shape, config):
        source = self.root / "book.m4b"
        make_source(source, *source_shape)
        self.assertEqual(stream_shape(source), source_shape)
        ch, sr = shape(*config)
        with ch, sr:
            audit = self.audit(source)
            out = self.encode(source, audit)
        return source, audit, out

    def test_mono_m4b_with_audio_channels_2_is_copied_and_stays_mono(self):
        source, audit, out = self.run_case((1, 44100), (2, 44100))
        self.assertEqual(audit.action, "remux")
        self.assertEqual(stream_shape(out), (1, 44100))
        self.assertEqual(audio_md5(out), audio_md5(source), "the audio packets must be the source's")

    def test_22050_m4b_with_sample_rate_44100_is_copied_and_stays_22050(self):
        source, audit, out = self.run_case((2, 22050), (2, 44100))
        self.assertEqual(audit.action, "remux")
        self.assertEqual(stream_shape(out), (2, 22050))
        self.assertEqual(audio_md5(out), audio_md5(source), "the audio packets must be the source's")

    def test_stereo_m4b_with_audio_channels_1_is_downmixed_to_mono(self):
        source, audit, out = self.run_case((2, 44100), (1, 44100))
        self.assertEqual(audit.action, "convert", "a stereo copy cannot honour AUDIO_CHANNELS=1")
        self.assertTrue(any("not within the configured caps" in note for note in audit.notes), audit.notes)
        self.assertEqual(stream_shape(out), (1, 44100))
        self.assertNotEqual(audio_md5(out), audio_md5(source))

    def test_48k_m4b_with_sample_rate_44100_is_resampled_to_44100(self):
        source, audit, out = self.run_case((2, 48000), (2, 44100))
        self.assertEqual(audit.action, "convert", "a 48 kHz copy cannot honour SAMPLE_RATE=44100")
        self.assertEqual(stream_shape(out), (2, 44100))
        self.assertNotEqual(audio_md5(out), audio_md5(source))


if __name__ == "__main__":
    unittest.main()

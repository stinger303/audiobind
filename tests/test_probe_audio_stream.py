"""AB-MONO-b: codec, rate and channels come from the same audio stream as the duration.

ffprobe lists a cover (mjpeg video), an attachment or a chapter track at index 0 of many .m4b files.
Reading codec/rate/channels from ``streams[0]`` while the duration walked to the first audio stream made
a cover-first AAC book look like ``mjpeg`` with no layout, so a file that only needed a remux was
silently re-encoded. Nothing here shells out: ``probe.subprocess.run`` is faked.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import encode, pipeline, probe
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

COVER = {"codec_type": "video", "codec_name": "mjpeg"}
AUDIO = {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2,
         "duration": "3600.0"}


def fake_ffprobe(streams, fmt=None):
    """A ``subprocess.run`` stand-in that answers only ffprobe, with the given JSON."""
    payload = {"format": fmt or {"duration": "3601.0", "bit_rate": "64000"}, "streams": streams}

    def run(cmd, *args, **kwargs):
        assert cmd[0] == "ffprobe", cmd
        return mock.Mock(stdout=json.dumps(payload))

    return run


def probe_with(streams, fmt=None):
    with mock.patch.object(probe.subprocess, "run", side_effect=fake_ffprobe(streams, fmt)):
        return probe.probe("/lib/Book/book.m4b")


class CoverFirstProbeTests(unittest.TestCase):
    def check_aac(self, info):
        self.assertEqual(info.codec, "aac")
        self.assertEqual(info.sample_rate, "44100")
        self.assertEqual(info.channels, "2")
        self.assertEqual(info.duration, 3600.0)
        self.assertEqual(info.kbps, 64)

    def test_codec_rate_and_channels_come_from_the_audio_stream(self):
        self.check_aac(probe_with([COVER, AUDIO]))

    def test_an_attachment_at_index_0_is_skipped_too(self):
        self.check_aac(probe_with([{"codec_type": "attachment", "codec_name": "bin_data"}, AUDIO]))

    def test_no_audio_stream_leaves_the_audio_fields_empty(self):
        info = probe_with([COVER], {"duration": "12.0"})
        self.assertEqual((info.codec, info.sample_rate, info.channels), ("", "", ""))
        self.assertEqual(info.duration, 12.0)

    def test_audio_first_is_unchanged(self):
        self.check_aac(probe_with([AUDIO, COVER]))


def one_file(path):
    return BookUnit(path=str(pathlib.Path(path).parent), name="Book", parent="",
                    parts=[Part("Book", [str(path)])])


class CoverFirstRemuxDecisionTests(unittest.TestCase):
    def test_a_cover_first_aac_m4b_is_remuxed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "Book").mkdir()
            source = root / "Book" / "book.m4b"
            source.write_bytes(b"")
            match = pipeline.matching.Match(confidence="HIGH")
            with (
                mock.patch.object(CONFIG, "library_root", root),
                mock.patch.object(CONFIG, "audio_channels", 2),
                mock.patch.object(CONFIG, "sample_rate", 44100),
                mock.patch.object(pipeline.matching, "match_unit", return_value=match),
                mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
                mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
                mock.patch.object(probe.subprocess, "run", side_effect=fake_ffprobe([COVER, AUDIO])),
            ):
                audit = pipeline.audit_unit(one_file(source))
        self.assertEqual(audit.codecs, {"aac"})
        self.assertEqual(audit.channels, {"2"})
        self.assertEqual(audit.sample_rates, {"44100"})
        self.assertEqual(audit.action, "remux")
        self.assertFalse(any("configured caps" in note for note in audit.notes), audit.notes)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class CoverFirstRealRemuxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        root = mock.patch.object(CONFIG, "library_root", self.root)
        root.start()
        self.addCleanup(root.stop)
        CONTROL.clear_stop()

    def test_the_audio_packets_of_a_cover_first_m4b_survive_the_remux(self):
        from tests.test_remux_audio_shape import audio_md5, stream_shape

        # the mp4/ipod/mov muxers reorder a cover behind the audio; Matroska keeps it at index 0, and
        # ffprobe sniffs content, so a Matroska file named .m4b is the cover-first shape on disk
        source = self.root / "book.m4b"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=20", "-f", "lavfi", "-i",
             "color=c=red:s=64x64:d=1", "-frames:v", "1", "-map", "1:v", "-map", "0:a",
             "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-ar", "44100", "-c:v", "mjpeg",
             "-disposition:v:0", "attached_pic", "-f", "matroska", str(source)],
            check=True, timeout=120,
        )
        self.assertEqual(probe.probe_streams(source)[0]["codec_type"], "video")  # the fixture is cover-first
        self.assertEqual(probe.probe(source).codec, "aac")
        match = pipeline.matching.Match(confidence="HIGH")
        with (
            mock.patch.object(CONFIG, "audio_channels", 2),
            mock.patch.object(CONFIG, "sample_rate", 44100),
            mock.patch.object(pipeline.matching, "match_unit", return_value=match),
            mock.patch.object(pipeline.catalog, "chapters", return_value=[]),
            mock.patch.object(pipeline.hardcover, "evidence_for", return_value=None),
        ):
            audit = pipeline.audit_unit(one_file(source))
        self.assertEqual(audit.action, "remux")
        m = mock.Mock(asin="B000000001", product=None)
        with (
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
        ):
            plan = encode.plan_chapters(audit.unit, m.asin)
            result = encode.encode(audit.unit, m, {"title": "T"}, self.root / "work", self.root / "out.m4b",
                                   log=lambda *_: None, copy_audio=True, plan=plan, publish=False)
        self.assertEqual(stream_shape(result.path), (2, 44100))
        self.assertEqual(audio_md5(result.path), audio_md5(source))


if __name__ == "__main__":
    unittest.main()

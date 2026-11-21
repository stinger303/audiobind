"""CORE-01: a mixed-codec book must not lose audio in the concat demuxer, and a short output must fail.

A 120 s MP3 followed by a 20 s FLAC used to come out of ffmpeg as 120 s of audio with exit 0 and
hundreds of decoder errors, and still pass verification (30 s absolute slack, format duration,
return code only).
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import encode, pipeline
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = []

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def audio_stream_seconds(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "json",
         str(path)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return float(json.loads(out)["streams"][0]["duration"])


def make_tone(path, seconds, freq=440):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=f={freq}:d={seconds}", "-ac", "2", "-ar", "44100",
         str(path)],
        check=True, timeout=120,
    )


class RealFfmpegCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def unit_of(self, files):
        return BookUnit(path=str(self.src), name="Book", parent=str(self.root), parts=[Part("Book", [str(f) for f in files])])

    def run_encode(self, unit, fmt="m4b", popen=None):
        patches = [
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
        ]
        if popen is not None:
            patches.append(mock.patch.object(encode.subprocess, "Popen", side_effect=popen))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        plan = encode.plan_chapters(unit, self.match.asin)
        work = self.root / f"work-{fmt}"
        result = encode.encode(unit, self.match, {"title": "T"}, work, self.root / "out" / f"Book.{fmt}",
                               log=lambda *_: None, plan=plan, fmt=fmt, publish=False)
        return result, work, plan


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class MixedCodecRealTests(RealFfmpegCase):
    def test_mp3_then_flac_keeps_all_140_seconds(self):
        """(a) never a silent 120 s: every second of both files is in the book."""
        mp3, flac = self.src / "01.mp3", self.src / "02.flac"
        make_tone(mp3, 120)
        make_tone(flac, 20, freq=660)
        unit = self.unit_of([mp3, flac])
        for fmt in ("m4b", "mp3"):
            with self.subTest(fmt=fmt):
                result, _work, plan = self.run_encode(unit, fmt=fmt)
                self.assertAlmostEqual(plan.total, 140.0, delta=0.1)
                self.assertAlmostEqual(result.duration, 140.0, delta=0.5)
                self.assertAlmostEqual(audio_stream_seconds(result.path), 140.0, delta=0.5)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class SingleCodecFastPathTests(RealFfmpegCase):
    def test_all_mp3_unit_is_one_ffmpeg_run_over_the_original_files(self):
        """(b) the single-codec path is untouched: one ffmpeg, the sources themselves in the list,
        the same argv build_command has always produced."""
        files = [self.src / f"{n:02d}.mp3" for n in range(1, 4)]
        for n, path in enumerate(files):
            make_tone(path, 20 + 5 * n)
        calls = []
        real_popen = subprocess.Popen

        def recording_popen(cmd, **kw):
            if encode.FFMPEG in cmd:  # ffprobe runs through subprocess.Popen too
                calls.append(list(cmd))
            return real_popen(cmd, **kw)

        result, work, plan = self.run_encode(self.unit_of(files), popen=recording_popen)
        self.assertEqual(len(calls), 1, calls)
        concat = work / "concat.txt"
        self.assertEqual(concat.read_text().splitlines(), [f"file '{f}'" for f in files])
        bitrate = int(calls[0][calls[0].index("-b:a") + 1].rstrip("k"))
        expected = encode._nice_prefix() + encode.build_command(
            "m4b", concat, work / "cover.jpg", work / "meta.txt", work / "output.m4b", False,
            copy_audio=False, bitrate=bitrate, tags=None,
        )
        self.assertEqual(calls[0], expected)
        self.assertFalse((work / "parts").exists())
        self.assertAlmostEqual(result.duration, plan.total, delta=0.1)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class TruncatedEncodeRealTests(RealFfmpegCase):
    def test_an_encode_cut_15_seconds_short_fails(self):
        """(c) a truncated output is a failure (convert() records it as "failed"), never "converted"."""
        files = [self.src / "01.mp3", self.src / "02.mp3"]
        make_tone(files[0], 100)
        make_tone(files[1], 40)
        real_popen = subprocess.Popen

        def truncating_popen(cmd, **kw):
            cmd = list(cmd)
            if encode.FFMPEG in cmd:  # the encode, not the ffprobe runs that verify it
                cmd = cmd[:-1] + ["-t", "125", cmd[-1]]
            return real_popen(cmd, **kw)

        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(self.unit_of(files), popen=truncating_popen)
        self.assertIn("files were dropped during the merge", str(ctx.exception))


# ------------------------------------------------------------------ no ffmpeg needed
class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def run_encode(self, total=140.0, output=140.0, streams=None, stderr=b""):
        def popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            kw["stderr"].write(stderr)
            return FakeProc()

        plan = encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": total, "title": "a"}], source="per-file (1 chapters)",
            durations={"/src/a.mp3": total},
        )
        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=output)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=streams or [{"codec_name": "aac"}]),
        ):
            return encode.encode(self.unit, self.match, {"title": "T"}, self.root / "work", self.root / "o.m4b",
                                 log=lambda *_: None, plan=plan, publish=False)

    def test_120_of_140_seconds_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(total=140.0, output=120.025)
        self.assertIn("files were dropped during the merge", str(ctx.exception))

    def test_25_seconds_short_of_ten_minutes_is_rejected(self):
        # passed under the old 30 s absolute floor
        with self.assertRaises(RuntimeError):
            self.run_encode(total=600.0, output=575.0)

    def test_container_rounding_still_passes(self):
        for output in (140.023, 139.98, 600 * 60.0 - 1.7):
            total = 140.0 if output < 1000 else 600 * 60.0
            with self.subTest(output=output):
                self.assertEqual(self.run_encode(total=total, output=output).duration, output)

    def test_the_audio_stream_duration_is_what_is_checked(self):
        # the container says 140 s; the AAC stream inside it holds 120 s
        with self.assertRaises(RuntimeError):
            self.run_encode(total=140.0, output=140.0, streams=[{"codec_name": "aac", "duration": "120.023220"}])

    def test_a_decode_error_storm_fails_on_exit_0(self):
        storm = b"".join(
            b"[mp3float @ 0x1] Header missing\n"
            b"[aist#0:0/mp3 @ 0x2] [dec:mp3float @ 0x3] Error submitting packet to decoder: "
            b"Invalid data found when processing input\n"
            for _ in range(862)
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(stderr=storm)
        self.assertIn("decod", str(ctx.exception))

    def test_older_ffmpeg_wording_and_collapsed_repeats_count(self):
        storm = (b"[mp3 @ 0x1] Error while decoding stream #0:0: Invalid data found when processing input\n"
                 b"    Last message repeated 300 times\n")
        with self.assertRaises(RuntimeError):
            self.run_encode(stderr=storm)

    def test_a_few_damaged_frames_still_pass(self):
        few = b"[mp3 @ 0x1] Error while decoding stream #0:0: Invalid data found when processing input\n" * 3
        self.assertEqual(self.run_encode(stderr=few).duration, 140.0)


class MixedCodecArgvTests(unittest.TestCase):
    """The mixed path, without ffmpeg: every part is decoded to one uniform FLAC first."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.files = ["/src/01.mp3", "/src/02.flac"]
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", self.files)])
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    @staticmethod
    def output_only_probe(path, **kw):
        # the sources were probed once, for the plan; only the encoder's own output is probed here
        if pathlib.Path(path).name.startswith("output."):
            return mock.Mock(duration=140.0)
        raise AssertionError(f"probed again: {path}")

    def run_encode(self, codecs, fmt="m4b"):
        calls = []

        def popen(cmd, **kw):
            calls.append(list(cmd))
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return FakeProc()

        plan = encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": 120.0, "title": "1"}, {"start": 120.0, "end": 140.0, "title": "2"}],
            source="per-file (2 chapters)", durations={self.files[0]: 120.0, self.files[1]: 20.0}, codecs=codecs,
        )
        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode.id3, "write_chapters", return_value="test"),
            mock.patch.object(encode, "probe", side_effect=self.output_only_probe),
            mock.patch.object(encode, "probe_chapters", return_value=[{}, {}]),
            mock.patch.object(encode, "probe_streams",
                              return_value=[{"codec_name": "mp3" if fmt == "mp3" else "aac", "duration": "140.0"}]),
            mock.patch.object(encode.os.path, "getsize", return_value=1_000_000),
        ):
            encode.encode(self.unit, self.match, {"title": "T"}, self.root / "work", self.root / f"o.{fmt}",
                          log=lambda *_: None, plan=plan, publish=False, fmt=fmt)
        return calls

    def test_mixed_codecs_decode_each_part_then_join_the_intermediates(self):
        calls = self.run_encode({"mp3", "flac"})
        self.assertEqual(len(calls), 3, calls)
        parts = self.root / "work" / "parts"
        for call, source, dest in zip(calls, self.files, ("0001.wav", "0002.wav")):
            self.assertEqual(call[call.index("-i") + 1], source)
            self.assertEqual(call[-1], str(parts / dest))
            self.assertEqual(call[call.index("-c:a") + 1], "pcm_s16le")
            self.assertEqual(call[call.index("-ar") + 1], str(encode.CONFIG.sample_rate))
            self.assertEqual(call[call.index("-ac") + 1], str(encode.CONFIG.audio_channels))
            self.assertEqual(call[call.index("-f") + 1], "wav")
        listed = (self.root / "work" / "concat.txt").read_text().splitlines()
        self.assertEqual(listed, [f"file '{parts / '0001.wav'}'", f"file '{parts / '0002.wav'}'"])
        self.assertIn("concat", calls[2])
        self.assertFalse(parts.exists(), "the PCM parts outlived the join")

    def test_one_codec_is_a_single_run_over_the_sources(self):
        calls = self.run_encode({"mp3"})
        self.assertEqual(len(calls), 1)
        listed = (self.root / "work" / "concat.txt").read_text().splitlines()
        self.assertEqual(listed, [f"file '{f}'" for f in self.files])

    def test_plan_chapters_records_the_codecs_it_probed(self):
        answers = {"/src/01.mp3": mock.Mock(duration=120.0, codec="mp3"),
                   "/src/02.flac": mock.Mock(duration=20.0, codec="flac")}
        with (
            mock.patch.object(encode, "probe", side_effect=lambda path, **kw: answers[path]),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
        ):
            plan = encode.plan_chapters(self.unit, "B000000001")
        self.assertEqual(plan.codecs, {"mp3", "flac"})

    def test_convert_unit_hands_the_audit_codecs_to_the_plan(self):
        audit = pipeline.Audit(unit=self.unit)
        audit.match = mock.Mock(asin="B000000001", product=mock.Mock(authors=["A"], title="T"))
        audit.durations = {self.files[0]: 120.0, self.files[1]: 20.0}
        audit.codecs = {"mp3", "flac"}
        with (
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "T"}),
            mock.patch.object(pipeline.library, "m4b_path", return_value=self.root / "nope" / "T.m4b"),
            mock.patch.object(pipeline.CONFIG, "work_dir", self.root / "work"),
            mock.patch.object(pipeline.CONTROL, "_dry_run", False),
            mock.patch.object(encode, "probe", side_effect=AssertionError("probed again")),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=RuntimeError("stop here")) as enc,
            self.assertRaises(RuntimeError),
        ):
            pipeline.convert_unit(audit, log=lambda *_: None)
        self.assertEqual(enc.call_args.kwargs["plan"].codecs, {"mp3", "flac"})


if __name__ == "__main__":
    unittest.main()

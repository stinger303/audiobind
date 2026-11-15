"""0.19.0-dev1: output format (M4B / MP3 / both) and the ID3 chapter writer. No ffmpeg, no network."""

import pathlib
import struct
import tempfile
import unittest
from unittest import mock

from audiobind import config, encode, id3, pipeline, web
from audiobind.config import CONFIG
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

CHAPTERS = [
    {"title": "One", "start": 0.0, "end": 3.0},
    {"title": "Zwei über – 中", "start": 3.0, "end": 6.5},
]


# ---------------------------------------------------------------- a small ID3v2.3 parser
def parse_tag(data: bytes):
    """(header dict, [(id, body)] real frames, padding bytes, audio offset)."""
    assert data[:3] == b"ID3"
    major, flags = data[3], data[5]
    size = id3.unsyncsafe(data[6:10])
    body = data[10 : 10 + size]
    frames, pos = [], 0
    while pos + 10 <= len(body) and body[pos] != 0:
        fid = body[pos : pos + 4].decode("latin-1")
        (n,) = struct.unpack(">I", body[pos + 4 : pos + 8])
        frames.append((fid, body[pos + 10 : pos + 10 + n]))
        pos += 10 + n
    return {"major": major, "flags": flags, "size": size}, frames, body[pos:], 10 + size


def decode_text(body: bytes) -> str:
    enc, raw = body[0], body[1:]
    if enc == 0:
        return raw.decode("latin-1")
    assert raw[:2] == b"\xff\xfe"
    return raw[2:].decode("utf-16-le").rstrip("\x00")


def parse_chap(body: bytes):
    element, rest = body.split(b"\x00", 1)
    start, end, off1, off2 = struct.unpack(">IIII", rest[:16])
    sub_id = rest[16:20].decode("latin-1")
    (n,) = struct.unpack(">I", rest[20:24])
    return element.decode(), start, end, off1, off2, sub_id, decode_text(rest[26 : 26 + n])


def make_tag(frames: bytes, padding: int = 0, version: int = 3, flags: int = 0) -> bytes:
    body = frames + b"\x00" * padding
    return b"ID3" + bytes([version, 0, flags]) + id3.syncsafe(len(body)) + body


AUDIO = b"\xff\xfb\x90\x00" + b"\x11\x22\x33" * 400
APIC = id3.frame("APIC", b"\x00image/jpeg\x00\x03Album cover\x00" + b"\xff\xd8\xff\xe0JFIF" * 20)
TIT2 = id3.text_frame("TIT2", "The Book")


class ChapWriterTests(unittest.TestCase):
    def write(self, content: bytes, chapters=CHAPTERS):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "book.mp3"
        path.write_bytes(content)
        mode = id3.write_chapters(path, chapters)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["book.mp3"])  # no temp left behind
        return mode, path.read_bytes()

    def test_appended_frames_round_trip(self):
        mode, out = self.write(make_tag(TIT2 + APIC, padding=10) + AUDIO)
        self.assertEqual(mode, "appended")
        header, frames, padding, audio_at = parse_tag(out)
        self.assertEqual((header["major"], header["flags"]), (3, 0))
        self.assertEqual([f[0] for f in frames], ["TIT2", "APIC", "CHAP", "CHAP", "CTOC"])
        self.assertEqual(out[audio_at:], AUDIO)
        self.assertEqual(padding, b"")

    def test_existing_frames_survive_byte_for_byte(self):
        _, out = self.write(make_tag(TIT2 + APIC, padding=10) + AUDIO)
        body = out[10 : 10 + id3.unsyncsafe(out[6:10])]
        self.assertTrue(body.startswith(TIT2 + APIC))  # APIC sits before the CHAP frames, intact

    def test_chap_fields(self):
        _, out = self.write(make_tag(TIT2 + APIC) + AUDIO)
        _, frames, _, _ = parse_tag(out)
        chaps = [parse_chap(b) for fid, b in frames if fid == "CHAP"]
        self.assertEqual(
            chaps,
            [
                ("ch1", 0, 3000, 0xFFFFFFFF, 0xFFFFFFFF, "TIT2", "One"),
                ("ch2", 3000, 6500, 0xFFFFFFFF, 0xFFFFFFFF, "TIT2", "Zwei über – 中"),
            ],
        )

    def test_ctoc_lists_children_in_order(self):
        chapters = [{"title": f"C{i}", "start": i, "end": i + 1} for i in range(12)]
        _, out = self.write(make_tag(TIT2) + AUDIO, chapters)
        _, frames, _, _ = parse_tag(out)
        ctoc = [b for fid, b in frames if fid == "CTOC"]
        self.assertEqual(len(ctoc), 1)
        element, rest = ctoc[0].split(b"\x00", 1)
        self.assertEqual(element, b"toc")
        self.assertEqual(rest[0], 0x03)
        self.assertEqual(rest[1], 12)
        self.assertEqual(rest[2:].split(b"\x00")[:-1], [f"ch{i}".encode() for i in range(1, 13)])

    def test_frame_sizes_are_consistent(self):
        _, out = self.write(make_tag(TIT2 + APIC) + AUDIO)
        header, frames, _, audio_at = parse_tag(out)
        self.assertEqual(sum(10 + len(b) for _, b in frames), header["size"])

    def test_no_tag_gets_a_fresh_v23_tag(self):
        mode, out = self.write(AUDIO)
        self.assertEqual(mode, "fresh")
        header, frames, _, audio_at = parse_tag(out)
        self.assertEqual(header["major"], 3)
        self.assertEqual([f[0] for f in frames], ["CHAP", "CHAP", "CTOC"])
        self.assertEqual(out[audio_at:], AUDIO)

    def test_extended_header_falls_back_instead_of_corrupting(self):
        original = make_tag(b"\x00\x00\x00\x06\x00\x00\x00\x00\x00\x00" + TIT2, flags=0x40)
        mode, out = self.write(original + AUDIO)
        self.assertEqual(mode, "fresh")
        header, frames, _, audio_at = parse_tag(out)
        self.assertEqual(header["flags"], 0)
        self.assertEqual([f[0] for f in frames], ["CHAP", "CHAP", "CTOC"])
        self.assertEqual(out[audio_at:], AUDIO)  # the old tag is gone, the audio untouched

    def test_unsynchronised_tag_falls_back(self):
        mode, out = self.write(make_tag(TIT2, flags=0x80) + AUDIO)
        self.assertEqual(mode, "fresh")
        self.assertEqual(parse_tag(out)[3], len(out) - len(AUDIO))

    def test_v24_tag_and_malformed_frames_fall_back(self):
        mode, out = self.write(make_tag(TIT2, version=4) + AUDIO)
        self.assertEqual(mode, "fresh")
        bad = b"TIT2" + struct.pack(">I", 9999) + b"\x00\x00\x00abc"  # claims more bytes than exist
        mode, out = self.write(make_tag(bad) + AUDIO)
        self.assertEqual(mode, "fresh")
        self.assertEqual(out[-len(AUDIO):], AUDIO)

    def test_ascii_titles_are_latin1_and_others_utf16(self):
        self.assertEqual(id3.text_frame("TIT2", "abc")[10], 0)
        self.assertEqual(id3.text_frame("TIT2", "中")[10], 1)


# ---------------------------------------------------------------- the setting
class SettingTests(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.saved = CONFIG.output_format
        config._EFFECTIVE.clear()

    def tearDown(self):
        CONFIG.output_format = self.saved
        config._EFFECTIVE.clear()
        super().tearDown()

    def test_default_is_m4b_and_listed(self):
        self.assertEqual(config.SETTING_CHOICES["output_format"], ("M4B", "MP3", "M4B + MP3"))
        self.assertEqual(config.setting_env_defaults()["output_format"], "M4B")
        self.assertEqual(encode.formats_for(config.effective_val("output_format")), ["m4b"])

    def test_renders_beside_the_profile_with_the_player_note(self):
        page = web.render_settings(self.state)
        self.assertIn('data-setting="output_format"', page)
        self.assertLess(page.index('id="ab-profile"'), page.index('data-setting="output_format"'))
        self.assertLess(page.index('data-setting="output_format"'), page.index('data-setting="auto_convert"'))
        self.assertIn('<option value="M4B + MP3"', page)
        self.assertIn("Audiobookshelf", page)
        self.assertIn("Apple", page)

    def test_saves_each_choice(self):
        for value, expect in (("MP3", ["mp3"]), ("M4B + MP3", ["m4b", "mp3"]), ("M4B", ["m4b"])):
            status, body = self.post({"settings": {"output_format": value}}, path="/api/settings")
            self.assertEqual(status, 200, body)
            self.assertEqual(encode.formats_for(config.effective_val("output_format")), expect)
        status, _ = self.post({"settings": {"output_format": "mp3"}}, path="/api/settings")
        self.assertEqual(status, 200)  # case-folded to the choice
        self.assertEqual(config.effective_val("output_format"), "MP3")

    def test_rejects_a_value_outside_the_choices(self):
        status, body = self.post({"settings": {"output_format": "FLAC"}}, path="/api/settings")
        self.assertEqual(status, 400, body)
        self.assertEqual(config.effective_val("output_format"), "M4B")

    def test_unknown_env_value_falls_back_to_m4b_files(self):
        self.assertEqual(encode.formats_for("banana"), ["m4b"])


# ---------------------------------------------------------------- the argv
M4B_ARGV_GOLDEN = [
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1", "-nostats",
    "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", "/w/concat.txt",
    "-protocol_whitelist", "file,pipe", "-i", "/w/cover.jpg",
    "-protocol_whitelist", "file,pipe", "-i", "/w/meta.txt", "-map", "0:a:0",
    "-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic",
    "-map_metadata", "2", "-map_chapters", "-1", "-map_chapters", "2",
    "-c:a", "aac", "-b:a", "128k", "-ac", "1", "-ar", "44100",
    "-movflags", "+faststart", "-f", "mp4", "/w/output.m4b",
]


class ArgvTests(unittest.TestCase):
    def cmd(self, fmt, **kw):
        kw.setdefault("has_cover", True)
        with mock.patch.object(CONFIG, "audio_channels", 1), mock.patch.object(CONFIG, "sample_rate", 44100):
            return encode.build_command(
                fmt, "/w/concat.txt", "/w/cover.jpg", "/w/meta.txt", f"/w/output.{fmt}", **kw
            )

    def test_m4b_argv_is_unchanged(self):
        self.assertEqual(self.cmd("m4b", bitrate=128), M4B_ARGV_GOLDEN)

    def test_m4b_remux_and_no_cover_variants_unchanged(self):
        no_cover = self.cmd("m4b", has_cover=False, bitrate=192)
        self.assertNotIn("1:v:0", no_cover)
        self.assertEqual(no_cover[no_cover.index("-map_metadata") + 1], "1")
        self.assertIn("192k", no_cover)
        remux = self.cmd("m4b", copy_audio=True)
        self.assertEqual(remux[remux.index("-c:a") + 1], "copy")

    def test_mp3_argv(self):
        cmd = self.cmd("mp3", bitrate=192, tags={"title": "T", "artist": "A"})
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "libmp3lame")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "192k")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "44100")
        self.assertEqual((cmd[-3], cmd[-2]), ("-f", "mp3"))
        self.assertEqual(cmd[-1], "/w/output.mp3")
        self.assertEqual(cmd[cmd.index("-id3v2_version") + 1], "3")
        for pair in (("-c:v", "mjpeg"), ("-map", "1:v:0"), ("-metadata:s:v", "title=Album cover"),
                     ("-metadata:s:v", "comment=Cover (front)"), ("-metadata", "title=T"),
                     ("-metadata", "artist=A"), ("-map_chapters", "-1"), ("-map_metadata", "-1")):
            self.assertIn(pair, list(zip(cmd, cmd[1:])), pair)
        self.assertNotIn("meta.txt", cmd)  # chapters cannot ride an ffmetadata input for mp3
        self.assertNotIn("aac", cmd)
        self.assertNotIn("attached_pic", cmd)

    def test_mp3_without_cover_has_no_picture_stream(self):
        cmd = self.cmd("mp3", has_cover=False)
        self.assertNotIn("-c:v", cmd)
        self.assertNotIn("/w/cover.jpg", cmd)
        self.assertEqual(cmd[cmd.index("-id3v2_version") + 1], "3")

    def test_mp3_inputs_are_whitelisted(self):
        cmd = self.cmd("mp3")
        for i, arg in enumerate(cmd):
            if arg == "-i":
                self.assertIn("-protocol_whitelist", cmd[max(0, i - 7) : i])

    def test_mp3_bitrates_snap_to_valid_values(self):
        for ok in (64, 128, 192, 256, 320):
            self.assertEqual(encode.mp3_bitrate(ok), ok)
        self.assertEqual(encode.mp3_bitrate(97), 96)
        self.assertEqual(encode.mp3_bitrate(300), 320)

    def test_mp3_tags_follow_the_m4b_values(self):
        tags = {"title": "T", "album": "T", "artist": "A", "album_artist": "A", "composer": "N",
                "genre": "Audiobook", "comment": "line1\nline2\x00", "date": "2020", "asin": "B0"}
        out = encode.mp3_tags(tags, "3")
        self.assertEqual(out["genre"], "Audiobook")
        self.assertEqual(out["track"], "3")
        self.assertEqual(out["comment"], "line1\nline2")
        self.assertNotIn("asin", out)
        self.assertNotIn("track", encode.mp3_tags(tags, "2.5"))


# ---------------------------------------------------------------- encode(): verification and fmt
class FakeProc:
    returncode = 0
    stdout = []

    class stderr:
        @staticmethod
        def read():
            return ""

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


class EncodeMp3Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = pathlib.Path(self.dir.name) / "work"
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.plan = encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": 30.0, "title": "a"}, {"start": 30.0, "end": 60.0, "title": "b"}],
            source="per-file (2 chapters)",
            durations={"/src/a.mp3": 60.0},
        )
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def run_encode(self, fmt="mp3", chapters_found=2, cover_found=True, codec="mp3", publish=False):
        out = pathlib.Path(self.dir.name) / "out" / f"Book.{fmt}"
        written = {}

        def fake_popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            written["cmd"] = cmd
            return FakeProc()

        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(encode.catalog, "download", side_effect=lambda url, dest: pathlib.Path(dest).write_bytes(b"jpg") or True),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode.id3, "write_chapters", return_value="appended") as chap,
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=60.0)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}] * chapters_found),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": codec}]),
            mock.patch.object(encode, "has_embedded_cover", return_value=cover_found),
        ):
            result = encode.encode(
                self.unit, self.match, {"title": "T"}, self.work, out, log=lambda *_: None,
                plan=self.plan, fmt=fmt, publish=publish,
            )
        return result, written, chap, out

    def test_mp3_writes_chapters_then_verifies(self):
        result, written, chap, _ = self.run_encode()
        chap.assert_called_once()
        self.assertEqual(chap.call_args.args[1], self.plan.chapters)
        self.assertEqual(result.chapters, 2)
        self.assertIn("libmp3lame", written["cmd"])
        self.assertTrue(result.path.name == "output.mp3" and result.path.exists())  # not published yet

    def test_wrong_chapter_count_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(chapters_found=1)
        self.assertIn("expected 2 chapters, found 1", str(ctx.exception))

    def test_missing_cover_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(cover_found=False)
        self.assertIn("cover", str(ctx.exception))

    def test_wrong_codec_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(codec="aac")
        self.assertIn("MP3", str(ctx.exception))

    def test_publish_moves_the_file_and_the_cover(self):
        result, _, _, out = self.run_encode(publish=True)
        self.assertEqual(result.path, out)
        self.assertTrue(out.exists())
        self.assertTrue((out.parent / "cover.jpg").exists())

    def test_m4b_never_touches_the_id3_writer(self):
        _, written, chap, _ = self.run_encode(fmt="m4b", codec="aac")
        chap.assert_not_called()
        self.assertIn("aac", written["cmd"])


# ---------------------------------------------------------------- the pipeline: both
class BothTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.audit = pipeline.Audit(unit=unit)
        self.audit.match = mock.Mock(asin="B000000001", product=mock.Mock(authors=["A"], title="T"))
        self.audit.durations = {"/src/a.mp3": 60.0}
        self.encodes = []

    def run_convert(self, setting, fail_on=None):
        out_root = self.root / "out"
        self.published = []

        def fake_encode(unit, match, book, work_dir, out_path, **kw):
            self.encodes.append((kw["fmt"], kw["publish"], pathlib.Path(out_path).name))
            if kw["fmt"] == fail_on:
                raise RuntimeError(f"{fail_on} verification failed")
            return encode.EncodeResult(pathlib.Path(work_dir) / f"o.{kw['fmt']}", 60.0, 10, 1, "x")

        def fake_publish(result, out_path):
            self.published.append(pathlib.Path(out_path).name)
            return result

        with (
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "T"}),
            mock.patch.object(pipeline.CONFIG, "output_root", out_root),
            mock.patch.object(pipeline.CONFIG, "work_dir", self.root / "work"),
            mock.patch.object(pipeline.CONTROL, "_dry_run", False),
            mock.patch.object(pipeline, "effective_val", side_effect=lambda k: setting if k == "output_format" else None),
            mock.patch.object(encode, "plan_chapters", return_value=encode.ChapterPlan([], "x", {"/src/a.mp3": 60.0})),
            mock.patch.object(encode, "encode", side_effect=fake_encode),
            mock.patch.object(encode, "publish_result", side_effect=fake_publish),
            mock.patch.object(encode, "write_abs_metadata"),
        ):
            return pipeline.convert_unit(self.audit, log=lambda *_: None)

    def test_m4b_only(self):
        self.run_convert("M4B")
        self.assertEqual([e[0] for e in self.encodes], ["m4b"])
        self.assertEqual(self.published, ["T.m4b"])
        self.assertTrue(self.audit.out_path.endswith("T.m4b"))

    def test_mp3_only(self):
        self.run_convert("MP3")
        self.assertEqual(self.published, ["T.mp3"])
        self.assertTrue(self.audit.out_path.endswith("T.mp3"))

    def test_both_publish_m4b_first_and_output_is_the_m4b(self):
        result = self.run_convert("M4B + MP3")
        self.assertEqual([e[0] for e in self.encodes], ["m4b", "mp3"])
        self.assertTrue(all(not e[1] for e in self.encodes))  # nothing published during the encode
        self.assertEqual(self.published, ["T.m4b", "T.mp3"])
        self.assertTrue(self.audit.out_path.endswith("T.m4b"))
        self.assertEqual(result.path.name, "o.m4b")

    def test_a_failed_second_encode_publishes_nothing(self):
        with self.assertRaises(RuntimeError):
            self.run_convert("M4B + MP3", fail_on="mp3")
        self.assertEqual([e[0] for e in self.encodes], ["m4b", "mp3"])
        self.assertEqual(self.published, [])

    def test_an_existing_main_file_is_not_encoded_again(self):
        out = self.root / "out" / "A" / "T"
        out.mkdir(parents=True)
        (out / "T.m4b").write_bytes(b"x")
        self.assertIsNone(self.run_convert("M4B + MP3"))
        self.assertEqual(self.encodes, [])
        self.assertTrue(self.audit.already_published)


if __name__ == "__main__":
    unittest.main()

"""v0.15.1: untrusted media hardening - protocol whitelist on every ffmpeg/ffprobe call,
names with line breaks / NUL held, ffmetadata escaping of \\r and '#'."""

import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from audiobind import encode, pipeline, probe
from audiobind.advice import explain
from audiobind.encode import ChapterPlan
from audiobind.matching import Match
from audiobind.state import State
from audiobind.units import BookUnit, Part, unsafe_path_reason

WL = ["-protocol_whitelist", "file,pipe"]


def book_unit(files, rel="Author/Book", part="Book"):
    return BookUnit(
        path="/library/" + rel, name="Book", parent="/library/Author", parts=[Part(part, files)], rel_path=rel
    )


class Captured(Exception):
    pass


class EscapeTests(unittest.TestCase):
    def test_carriage_return_cannot_start_a_section(self):
        out = encode._escape("Title\r[CHAPTER]\rTIMEBASE=1/1")
        self.assertNotIn("\r", out)
        self.assertNotIn("\n", out)
        self.assertIn("\\=", out)

    def test_hash_and_specials_are_escaped(self):
        self.assertEqual(encode._escape("Book #1; a=b \\ c"), "Book \\#1\\; a\\=b \\\\ c")

    def test_all_control_chars_removed(self):
        out = encode._escape("a\x00b\x01c\td\x1fe\x7ff\nG")
        self.assertFalse(any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in out))
        self.assertEqual(out.replace(" ", ""), "abcdefG")

    def test_written_file_has_only_real_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "meta.txt"
            book = {"title": "Evil\r[CHAPTER]\rTIMEBASE=1/1000\rSTART=0", "authors": [{"name": "A #1"}]}
            chapters = [{"start": 0, "end": 1, "title": "One\r[CHAPTER]"}]
            encode.write_ffmetadata(path, book, Match(), chapters)
            raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        lines = raw.decode().split("\n")
        self.assertEqual(sum(1 for line in lines if line == "[CHAPTER]"), 1)
        self.assertIn("artist=A \\#1", lines)


class UnsafePathTests(unittest.TestCase):
    def test_clean_unit_is_safe(self):
        self.assertEqual(unsafe_path_reason(book_unit(["/library/Author/Book/01 - It's fine.mp3"])), "")

    def test_newline_in_file_name(self):
        reason = unsafe_path_reason(book_unit(["/library/Author/Book/01\nfile x.mp3"]))
        self.assertIn("line break", reason)
        self.assertTrue(reason.startswith("unsafe file name"))
        self.assertNotIn("\n", reason)  # shown escaped, safe for the note / log

    def test_carriage_return_in_folder(self):
        unit = book_unit(["/library/Author/Bo\rok/01.mp3"], rel="Author/Bo\rok")
        self.assertIn("carriage return", unsafe_path_reason(unit))

    def test_nul_byte(self):
        self.assertIn("NUL", unsafe_path_reason(book_unit(["/library/Author/Book/0\x001.mp3"])))

    def test_advice_for_unsafe_hold(self):
        why, what = explain("held", "NONE", "unsafe file name: 01\\nx.mp3 contains a line break")
        self.assertIn("line break", why)
        self.assertIn("Rename", what)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.tmp.name))

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_unsafe_unit_is_held_before_any_ffprobe(self):
        unit = book_unit(["/library/Author/Book/01\ninpoint 5.mp3"])
        for force in (False, True):
            with self.subTest(force=force):
                with mock.patch.object(pipeline, "audit_unit") as audit, mock.patch.object(
                    pipeline, "probe"
                ) as prb:
                    pipeline.process_unit(self.state, unit, log=lambda *_: None, force=force)
                audit.assert_not_called()
                prb.assert_not_called()
                row = self.state.get("Author/Book")
                self.assertEqual(row.status, "held")
                self.assertIn("unsafe file name", row.note)

    def test_encode_refuses_unsafe_paths(self):
        unit = book_unit(["/library/Author/Book/a\nb.mp3"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(encode.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                encode.encode(
                    unit, Match(), {}, pathlib.Path(tmp), pathlib.Path(tmp) / "o.m4b",
                    log=lambda *_: None, plan=ChapterPlan([], "x", {}),
                )
            popen.assert_not_called()
            self.assertFalse((pathlib.Path(tmp) / "concat.txt").exists())


class CommandLineTests(unittest.TestCase):
    def _command(self, has_cover):
        unit = book_unit(["/library/Author/Book/01.mp3", "/library/Author/Book/02 it's.mp3"])
        seen = {}

        def capture(cmd, **kw):
            seen["cmd"] = cmd
            raise Captured

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            encode.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(encode.catalog, "download", return_value=has_cover), mock.patch.object(
            encode.catalog, "cover_url", return_value=""
        ):
            with self.assertRaises(Captured):
                encode.encode(
                    unit, Match(), {"title": "T"}, pathlib.Path(tmp), pathlib.Path(tmp) / "o.m4b",
                    log=lambda *_: None, plan=ChapterPlan([], "x", {}),
                )
            concat = (pathlib.Path(tmp) / "concat.txt").read_text()
        return seen["cmd"], concat

    def test_every_ffmpeg_input_is_whitelisted(self):
        for has_cover in (True, False):
            with self.subTest(cover=has_cover):
                cmd, _ = self._command(has_cover)
                inputs = [i for i, arg in enumerate(cmd) if arg == "-i"]
                self.assertEqual(len(inputs), 3 if has_cover else 2)
                for i in inputs:
                    window = cmd[max(0, i - 7) : i]
                    self.assertIn("-protocol_whitelist", window, cmd)
                    self.assertEqual(window[window.index("-protocol_whitelist") + 1], "file,pipe")

    def test_concat_keeps_safe_0_and_quotes_paths(self):
        cmd, concat = self._command(False)
        self.assertEqual(cmd[cmd.index("-safe") + 1], "0")
        self.assertIn("file '/library/Author/Book/02 it'\\''s.mp3'", concat)
        self.assertEqual(concat.count("\n"), 2)

    def test_every_ffprobe_call_is_whitelisted(self):
        result = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(probe.subprocess, "run", return_value=result) as run:
            probe.probe("/x.mp3")
            probe.probe_chapters("/x.mp3")
            probe.probe_streams("/x.mp3")
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], "ffprobe")
            self.assertEqual(argv[1:3], WL)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealFfmpegTests(unittest.TestCase):
    def test_whitelisted_probe_reads_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "a b.mp3"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(path)], check=True, timeout=60
            )
            self.assertAlmostEqual(probe.probe(path).duration, 1.0, delta=0.1)

    def test_whitelist_stops_a_concat_list_reaching_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = pathlib.Path(tmp) / "list.txt"
            listing.write_text("file 'http://127.0.0.1:9/x.mp3'\n")
            result = subprocess.run(
                ["ffmpeg", "-v", "error", *WL, "-f", "concat", "-safe", "0", "-i", str(listing), "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on whitelist", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""B3: an ffmpeg killed by a signal says so, instead of a generic "ffmpeg failed".

Red on 9c9cae1: both ffmpeg call sites checked only ``returncode != 0``, so an OOM kill (SIGKILL)
or a segfault read exactly like a damaged source.
"""

import pathlib
import unittest
from unittest import mock

from audiobind import encode
from audiobind.control import CONTROL, Stopped
from tests.test_encode_correctness import EncodeHarness, FakeProc


class KillableProc(FakeProc):
    """CONTROL.stop() terminates the attached process; this one has already exited."""

    def terminate(self):
        pass

    def kill(self):
        pass


def killed_by(rc):
    def popen(cmd, **kw):
        pathlib.Path(cmd[-1]).write_bytes(b"audio")
        kw["stderr"].write(b"size=  1024kB time=00:10:00.00 bitrate=  64.0kbits/s speed=40x\n")
        return KillableProc(returncode=rc)

    return popen


class TestB3EncodeSiteNamesTheSignal(EncodeHarness):
    def test_sigkill_is_named_with_the_memory_hint(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(popen=killed_by(-9))
        message = str(ctx.exception)
        self.assertIn("SIGKILL", message)
        self.assertIn("memory", message)
        self.assertNotRegex(message, r"^ffmpeg failed: ")
        self.assertLessEqual(len(message), 300)

    def test_sigsegv_is_named(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(popen=killed_by(-11))
        self.assertIn("SIGSEGV", str(ctx.exception))
        self.assertLessEqual(len(str(ctx.exception)), 300)

    def test_an_unknown_signal_number_is_still_reported_as_a_signal(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(popen=killed_by(-200))
        self.assertIn("signal 200", str(ctx.exception))

    def test_a_plain_non_zero_exit_is_unchanged(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(popen=killed_by(1))
        self.assertRegex(str(ctx.exception), r"^ffmpeg failed: ")

    def test_a_requested_stop_is_still_stopped(self):
        self.addCleanup(CONTROL.resume)
        self.addCleanup(CONTROL.clear_stop)

        def popen(cmd, **kw):
            CONTROL.stop()  # the operator's Stop kills ffmpeg: it exits on a signal too
            return killed_by(-9)(cmd, **kw)

        with self.assertRaises(Stopped):
            self.run_encode(popen=popen)


class TestB3DecodeSiteNamesTheSignal(unittest.TestCase):
    """The per-file PCM decode of a mixed-codec join (_normalise_parts)."""

    def setUp(self):
        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = pathlib.Path(self.dir.name)
        (self.work / "parts").mkdir()
        CONTROL.clear_stop()

    def decode(self, rc):
        with mock.patch.object(encode.subprocess, "Popen", side_effect=killed_by(rc)):
            return encode._normalise_parts(["/src/Book/01 Opening.flac"], self.work, 1, 44100)

    def test_sigkill_is_named_and_the_file_too(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.decode(-9)
        message = str(ctx.exception)
        self.assertIn("SIGKILL", message)
        self.assertIn("01 Opening.flac", message)
        self.assertNotRegex(message, r"^ffmpeg could not decode ")
        self.assertLessEqual(len(message), 300)

    def test_a_plain_non_zero_exit_is_unchanged(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.decode(1)
        self.assertRegex(str(ctx.exception), r"^ffmpeg could not decode 01 Opening\.flac: ")


if __name__ == "__main__":
    unittest.main()

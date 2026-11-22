"""CORE-01c: the mixed-codec PCM join must not fill the scratch disk, nor strand its parts.

Decoding a 40-hour book to 16-bit stereo 44.1 kHz PCM is 25.4 GB. The join used to start that decode
with no look at the free space, and its parts were removed only by ``finally`` blocks: a hard kill (or
an early raise in a direct encoder call) left them under the work root for good.
"""

import fcntl
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import namedtuple
from unittest import mock

from audiobind import encode, pipeline
from audiobind.control import CONTROL
from audiobind.state import State
from audiobind.units import BookUnit, Part

Usage = namedtuple("Usage", "total used free")
GB = 1_000_000_000


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = []

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class MixedCase(unittest.TestCase):
    files = ["/src/01.mp3", "/src/02.flac"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", self.files)])
        self.match = mock.Mock(asin="B000000001", product=None)
        self.calls = []
        CONTROL.clear_stop()

    def plan(self, hours):
        seconds = hours * 3600.0
        return encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": seconds, "title": "1"}], source="per-file (1 chapters)",
            durations={self.files[0]: seconds - 20.0, self.files[1]: 20.0}, codecs={"mp3", "flac"},
        )

    def popen(self, cmd, **kw):
        self.calls.append(list(cmd))
        pathlib.Path(cmd[-1]).write_bytes(b"audio")
        return FakeProc()

    def run_encode(self, hours, free, popen=None, download=None):
        plan = self.plan(hours)
        total = plan.total
        with (
            mock.patch.object(encode.shutil, "disk_usage", return_value=Usage(10 * free, 9 * free, free)),
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen or self.popen),
            mock.patch.object(encode.catalog, "download", side_effect=download, return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=total)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": "aac", "duration": str(total)}]),
            mock.patch.object(encode.os.path, "getsize", return_value=1_000_000),
        ):
            return encode.encode(self.unit, self.match, {"title": "T"}, self.work, self.root / "o.m4b",
                                 log=lambda *_: None, plan=plan, publish=False)

    @property
    def work(self):
        return self.root / "work" / "abcd1234" / "m4b"


class PreflightTests(MixedCase):
    def test_a_book_whose_pcm_would_not_fit_is_refused_before_any_decode(self):
        """(a) 40 h of PCM is 25.4 GB; with 20 GB free nothing is started and nothing is written."""
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(hours=40, free=20 * GB)
        message = str(ctx.exception)
        self.assertEqual(self.calls, [], "a decode was started that could not finish")
        self.assertFalse((self.work / "parts").exists())
        self.assertFalse((self.work / "concat.txt").exists())
        self.assertIn("scratch space", message)
        self.assertIn("25.4 GB", message)
        self.assertIn("20.0 GB free", message)
        # process_unit records the first 300 characters as the failed row's note
        self.assertLessEqual(len(message), 300, message)

    def test_the_budget_is_80_percent_of_the_free_space(self):
        need = encode.pcm_bytes(10 * 3600.0)  # 6.35 GB
        with self.assertRaises(RuntimeError):
            self.run_encode(hours=10, free=int(need / 0.8) - 1_000_000)
        self.calls.clear()
        self.run_encode(hours=10, free=int(need / 0.8) + 1_000_000)
        self.assertEqual(len(self.calls), 3, "two decodes and the join")

    def test_one_codec_needs_no_scratch_and_is_never_refused(self):
        plan = self.plan(40)
        plan.codecs = {"mp3"}
        with (
            mock.patch.object(encode.shutil, "disk_usage", return_value=Usage(1, 1, 1)),
            mock.patch.object(encode.subprocess, "Popen", side_effect=self.popen),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=plan.total)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": "aac"}]),
            mock.patch.object(encode.os.path, "getsize", return_value=1_000_000),
        ):
            encode.encode(self.unit, self.match, {"title": "T"}, self.work, self.root / "o.m4b",
                          log=lambda *_: None, plan=plan, publish=False)
        self.assertEqual(len(self.calls), 1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealMixedJoinTests(unittest.TestCase):
    def test_a_mixed_unit_inside_its_budget_still_converts_whole(self):
        """(b) 120 s MP3 + 20 s FLAC with just enough room for its 24.7 MB of PCM: all 140 s, no scratch left."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        files = [root / "01.mp3", root / "02.flac"]
        for path, seconds in zip(files, (120, 20)):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=d={seconds}", "-ac", "2",
                            "-ar", "44100", str(path)], check=True, timeout=120)
        unit = BookUnit(path=str(root), name="Book", parent="", parts=[Part("Book", [str(f) for f in files])])
        match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()
        work = root / "work" / "m4b"
        free = int(encode.pcm_bytes(140.0) / encode.PCM_SPACE_FRACTION) + 2_000_000
        with (
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode.catalog, "download", return_value=False),
            mock.patch.object(encode.catalog, "cover_url", return_value=""),
            mock.patch.object(encode.shutil, "disk_usage", return_value=Usage(10 * free, 9 * free, free)),
        ):
            plan = encode.plan_chapters(unit, match.asin)
            result = encode.encode(unit, match, {"title": "T"}, work, root / "o.m4b", log=lambda *_: None,
                                   plan=plan, publish=False)
        self.assertAlmostEqual(result.duration, 140.0, delta=0.5)
        self.assertEqual(sorted(p.name for p in work.iterdir()),
                         ["concat.txt", "ffmpeg-stderr.log", "meta.txt", "output.m4b"])


class PartsCleanupTests(MixedCase):
    def test_a_failed_decode_leaves_no_parts_behind(self):
        """A direct encoder call has no pipeline finally around it: the parts go with the failure."""
        def popen(cmd, **kw):
            self.calls.append(list(cmd))
            pathlib.Path(cmd[-1]).write_bytes(b"pcm")
            return FakeProc(returncode=1 if len(self.calls) == 2 else 0)

        with self.assertRaises(RuntimeError):
            self.run_encode(hours=1, free=100 * GB, popen=popen)
        self.assertFalse((self.work / "parts").exists())

    def test_a_failure_between_the_decode_and_the_join_leaves_no_parts_behind(self):
        with self.assertRaises(OSError):
            self.run_encode(hours=1, free=100 * GB, download=OSError("cover fetch blew up"))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.work / "parts").exists())


class OrphanReclaimTests(MixedCase):
    def stranded(self, key, age=3600.0, owner=False):
        parts = self.root / "work" / key / "m4b" / "parts"
        parts.mkdir(parents=True)
        (parts / "0001.wav").write_bytes(b"pcm" * 100)
        if owner:
            parts.with_name("parts.owner").write_bytes(b"")
        old = time.time() - age
        os.utime(parts, (old, old))
        return parts

    def test_a_stranded_parts_directory_is_left_alone_on_the_next_conversion(self):
        """The new owner cleans only the scratch directory for its own attempt."""
        legacy = self.stranded("deadbeef")  # no owner file: a run from before ownership locks
        unowned = self.stranded("feedface", owner=True)  # an owner file nobody holds: the run died
        audit = pipeline.Audit(unit=self.unit)
        audit.match = mock.Mock(asin="B000000001", product=mock.Mock(authors=["A"], title="T"))
        audit.durations = {self.files[0]: 120.0, self.files[1]: 20.0}
        audit.codecs = {"mp3", "flac"}
        state = State(pathlib.Path(self.dir.name) / "state")
        self.addCleanup(state.close)
        with (
            mock.patch.object(pipeline.catalog, "book", return_value={"title": "T"}),
            mock.patch.object(pipeline.library, "m4b_path", return_value=self.root / "nope" / "T.m4b"),
            mock.patch.object(pipeline.CONFIG, "work_dir", self.root / "work"),
            mock.patch.object(pipeline.CONTROL, "_dry_run", False),
            mock.patch.object(encode.catalog, "chapters", return_value=[]),
            mock.patch.object(encode, "probe_chapters", return_value=[]),
            mock.patch.object(encode, "encode", side_effect=RuntimeError("stop here")),
            self.assertRaises(RuntimeError),
        ):
            pipeline.convert_unit(audit, state=state, log=lambda *_: None)
        self.assertTrue(legacy.exists())
        self.assertTrue(unowned.exists())
        self.assertTrue(unowned.with_name("parts.owner").exists())

    def test_parts_a_running_conversion_holds_are_left_alone(self):
        held = self.stranded("c0ffee00", owner=True)
        fd = os.open(held.with_name("parts.owner"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # what a live run (any process) holds
        young = self.stranded("0ddba11", age=1.0)  # being created this very moment
        stale = self.stranded("deadbeef")
        removed = encode.reclaim_stale_parts(self.root / "work")
        self.assertEqual(removed, [stale])
        self.assertTrue(held.exists())
        self.assertTrue(young.exists())
        self.assertFalse(stale.exists())

    def test_the_running_join_owns_its_parts_while_it_decodes(self):
        """The sweep, run mid-decode, must not take the live conversion's parts."""
        seen = []

        def popen(cmd, **kw):
            self.calls.append(list(cmd))
            pathlib.Path(cmd[-1]).write_bytes(b"pcm")
            if len(self.calls) == 1:
                seen.append(encode.reclaim_stale_parts(self.root / "work"))
                seen.append((self.work / "parts").exists())
            return FakeProc()

        self.run_encode(hours=1, free=100 * GB, popen=popen)
        self.assertEqual(seen, [[], True])
        self.assertFalse((self.work / "parts").exists())

    def test_a_missing_work_root_is_nothing_to_reclaim(self):
        self.assertEqual(encode.reclaim_stale_parts(self.root / "nowhere"), [])

    def test_the_daemon_leaves_other_attempts_alone_at_startup(self):
        source = pathlib.Path(pipeline.__file__).with_name("daemon.py").read_text(encoding="utf-8")
        run_body = source.split("def run()", 1)[1]
        self.assertNotIn("reclaim_stale_parts", run_body.split("web.serve", 1)[0])


if __name__ == "__main__":
    unittest.main()

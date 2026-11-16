"""encode.py correctness: the duration check, atomic publish, stderr handling, chapters."""

import pathlib
import tempfile
import unittest
from unittest import mock

from audiobind import encode
from audiobind.control import CONTROL
from audiobind.units import BookUnit, Part


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = []
        self.stderr = None

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class EncodeHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.work = self.root / "work"
        self.unit = BookUnit(path="/src", name="Book", parent="", parts=[Part("Book", ["/src/a.mp3"])])
        self.match = mock.Mock(asin="B000000001", product=None)
        CONTROL.clear_stop()

    def plan(self, total):
        return encode.ChapterPlan(
            chapters=[{"start": 0.0, "end": total, "title": "a"}],
            source="per-file (1 chapters)",
            durations={"/src/a.mp3": total},
        )

    def run_encode(self, total=60.0, output=60.0, popen=None, download=None, url="https://m.media-amazon.com/c.jpg"):
        def default_popen(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(b"audio")
            return FakeProc()

        def default_download(u, dest):
            pathlib.Path(dest).write_bytes(b"j" * 4000)
            return True

        with (
            mock.patch.object(encode.subprocess, "Popen", side_effect=popen or default_popen),
            mock.patch.object(encode.catalog, "download", side_effect=download or default_download) as dl,
            mock.patch.object(encode.catalog, "cover_url", return_value=url),
            mock.patch.object(encode, "probe", return_value=mock.Mock(duration=output)),
            mock.patch.object(encode, "probe_chapters", return_value=[{}]),
            mock.patch.object(encode, "probe_streams", return_value=[{"codec_name": "aac"}]),
            mock.patch.object(encode, "has_embedded_cover", return_value=True),
        ):
            result = encode.encode(
                self.unit, self.match, {"title": "T"}, self.work, self.root / "out" / "Book.m4b",
                log=lambda *_: None, plan=self.plan(total), publish=False,
            )
        self.download_mock = dl
        return result


# ---------------------------------------------------------------- 1. duration check
class DurationCheckTests(EncodeHarness):
    def test_fifteen_minutes_short_of_ten_hours_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_encode(total=600 * 60.0, output=585 * 60.0)
        self.assertIn("files were dropped during the merge", str(ctx.exception))

    def test_two_minutes_short_of_ten_hours_is_rejected(self):
        # passed under the old max(120, 2%) rule (12 minutes of slack)
        with self.assertRaises(RuntimeError):
            self.run_encode(total=600 * 60.0, output=598 * 60.0)

    def test_a_second_or_two_of_container_rounding_passes(self):
        for output in (600 * 60.0 - 1.7, 600 * 60.0 + 1.2):
            with self.subTest(output=output):
                result = self.run_encode(total=600 * 60.0, output=output)
                self.assertEqual(result.duration, output)

    def test_short_book_keeps_the_thirty_second_floor(self):
        self.run_encode(total=600.0, output=575.0)
        with self.assertRaises(RuntimeError):
            self.run_encode(total=600.0, output=565.0)


if __name__ == "__main__":
    unittest.main()

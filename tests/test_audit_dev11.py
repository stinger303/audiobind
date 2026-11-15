"""0.19.0-dev11: catalog read caps, cover-cache pruning, _attr quoting, CSP, _start_manual."""

import io
import os
import pathlib
import re
import tempfile
import time
import unittest
from unittest import mock

from audiobind import catalog, covers, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCatalogReadCap(unittest.TestCase):
    def test_oversized_json_is_refused_without_retry(self):
        body = b"{" + b" " * (catalog.MAX_BODY + 10) + b"}"
        catalog.cache_clear()
        with mock.patch.object(catalog.urllib.request, "urlopen", side_effect=lambda *a, **k: _Response(body)) as op:
            with mock.patch.object(catalog.time, "sleep"):
                self.assertIsNone(catalog._get_json("https://api.example.invalid/x"))
        self.assertEqual(op.call_count, 1)

    def test_normal_json_still_reads(self):
        catalog.cache_clear()
        with mock.patch.object(catalog.urllib.request, "urlopen", side_effect=lambda *a, **k: _Response(b'{"a": 1}')):
            with mock.patch.object(catalog.time, "sleep"):
                self.assertEqual(catalog._get_json("https://api.example.invalid/ok"), {"a": 1})

    def test_oversized_image_is_refused(self):
        big = b"x" * (catalog.MAX_BODY + 1)
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "c.jpg"
            with mock.patch.object(catalog._image_opener, "open", side_effect=lambda *a, **k: _Response(big)):
                self.assertFalse(catalog.download("https://m.media-amazon.com/x.jpg", str(dest)))
            self.assertFalse(dest.exists())
            with mock.patch.object(catalog._image_opener, "open", side_effect=lambda *a, **k: _Response(b"y" * 5000)):
                self.assertTrue(catalog.download("https://m.media-amazon.com/x.jpg", str(dest)))


class TestCoverPrune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.dir.name)
        self.state = State(root / "cfg")
        self._state_dir = CONFIG.state_dir
        CONFIG.state_dir = root / "cfg"
        covers.cache_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        CONFIG.state_dir = self._state_dir
        self.state.close()
        self.dir.cleanup()

    def touch(self, asin, suffix=".jpg", age=0):
        path = covers.cache_dir() / (asin + suffix)
        path.write_bytes(b"x" * 10)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_asin_that_left_the_seen_set_is_removed_and_one_still_there_kept(self):
        self.state.catalog_seen_put([{"asin": "B0SEEN0001", "image": ""}])
        gone, kept, miss = self.touch("B0GONE0001"), self.touch("B0SEEN0001"), self.touch("B0GONE0001", ".miss")
        covers.prune(self.state)
        self.assertFalse(gone.exists())
        self.assertFalse(miss.exists())
        self.assertTrue(kept.exists())

    def test_a_library_books_cover_survives(self):
        self.state.save(UnitState(path="A/B", status="review", asin="B0LIB00001"))
        lib = self.touch("B0LIB00001")
        covers.prune(self.state)
        self.assertTrue(lib.exists())

    def test_ceiling_drops_oldest_unprotected_but_never_library(self):
        self.state.save(UnitState(path="A/B", status="review", asin="B0LIB00001"))
        self.state.catalog_seen_put([{"asin": f"B0SEEN000{i}"} for i in range(3)])
        lib = self.touch("B0LIB00001", age=9999)
        files = [self.touch(f"B0SEEN000{i}", age=100 - i) for i in range(3)]
        with mock.patch.object(covers, "MAX_CACHED_COVERS", 2):
            covers.prune(self.state)
        self.assertTrue(lib.exists())
        self.assertFalse(files[0].exists())
        self.assertFalse(files[1].exists())  # total ceiling 2: library + newest
        self.assertTrue(files[2].exists())


class TestAttrAndCsp(unittest.TestCase):
    def test_attr_escapes_all_five(self):
        self.assertEqual(web._attr("""a'b"c<d&e"""), "a&#x27;b&quot;c&lt;d&amp;e")

    def test_csp_has_new_directives_and_no_script_src(self):
        csp = dict(web.SECURITY_HEADERS)["Content-Security-Policy"]
        for part in ("frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'self'"):
            self.assertIn(part, csp)
        self.assertNotIn("script-src", csp)

    def test_page_still_has_one_inline_script(self):
        page = web.PAGE.replace("{script}", web.SCRIPT)
        self.assertEqual(len(re.findall(r"<script\b", page)), 1)


class TestStartManualReturnsString(unittest.TestCase):
    def test_every_return_in_start_manual_has_a_value(self):
        import inspect

        self.assertNotRegex(inspect.getsource(web.Handler._start_manual), r"\n\s+return\n")


if __name__ == "__main__":
    unittest.main()

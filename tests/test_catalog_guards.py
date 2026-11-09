"""v0.14.4: ASIN validation and quoting, image-host allowlist, covers only for known ASINs."""

import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from audiobind import catalog, covers
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class TestAsinInUrls(unittest.TestCase):
    def test_asin_is_quoted_into_audnex_urls(self):
        with mock.patch.object(catalog, "_get_json", return_value={}) as get:
            catalog.book("../../admin?x=1#")
            catalog.chapters("../evil")
            catalog.product_images("a/b")
        urls = [c.args[0] for c in get.call_args_list]
        self.assertTrue(urls[0].startswith(catalog.AUDNEX + "/books/..%2F..%2Fadmin%3Fx%3D1%23?region="))
        self.assertIn("/books/..%2Fevil/chapters?", urls[1])
        self.assertIn("/products/a%2Fb?", urls[2])

    def test_a_normal_asin_is_unchanged(self):
        with mock.patch.object(catalog, "_get_json", return_value={}) as get:
            catalog.book("B005EHN61O")
        self.assertIn("/books/B005EHN61O?region=", get.call_args.args[0])


class TestSetAsinValidation(LiveServerCase):
    def setUp(self):
        super().setUp()
        (self.lib / "A" / "B").mkdir(parents=True)
        (self.lib / "A" / "B" / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="A/B", status="review"))

    def test_non_asin_strings_are_refused(self):
        for bad in ("../../x/abcdefgh", "B005EHN61O?x", "abcdefgh ijk", "B00", "B005EHN61OXXXXX"):
            status, body = self.post({"action": "set_asin", "path": "A/B", "asin": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("ASIN", body["error"])
        self.assertEqual(self.state.get_asin_override("A/B"), "")


class TestImageAllowlist(unittest.TestCase):
    def test_allowed_hosts(self):
        for url in (
            "https://m.media-amazon.com/images/I/51x.jpg",
            "https://images-na.ssl-images-amazon.com/images/I/x.jpg",
            "https://www.audible.com/x.jpg",
        ):
            self.assertTrue(catalog.allowed_image_url(url), url)

    def test_refused_urls(self):
        for url in (
            "file:///etc/passwd",
            "http://m.media-amazon.com/images/I/x.jpg",  # not https
            "ftp://m.media-amazon.com/x.jpg",
            "https://evil.example/x.jpg",
            "https://media-amazon.com.evil.example/x.jpg",
            "https://evilmedia-amazon.com/x.jpg",
            "",
        ):
            self.assertFalse(catalog.allowed_image_url(url), url)

    def test_download_never_opens_a_refused_url(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(catalog, "_image_opener") as opener:
            self.assertFalse(catalog.download("file:///etc/passwd", str(pathlib.Path(d) / "c.jpg")))
            opener.open.assert_not_called()

    def test_a_redirect_off_the_allowlist_is_refused(self):
        handler = catalog._AllowlistRedirects()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd")
        ctx.exception.close()


class TestCoversOnlyForKnownAsins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        self.state = State(self.root / "cfg")
        self.saved = CONFIG.state_dir
        CONFIG.state_dir = self.root / "cfg"

    def tearDown(self):
        CONFIG.state_dir = self.saved
        self.state.close()
        self.dir.cleanup()

    def test_unknown_asin_triggers_no_fetch_and_no_miss_file(self):
        with mock.patch.object(catalog, "download", side_effect=AssertionError("fetched")):
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0NOTMINE1"))
        self.assertFalse((covers.cache_dir() / "B0NOTMINE1.miss").exists())

    def test_known_asin_is_fetched(self):
        detail = json.dumps({"match": {"image": "https://m.media-amazon.com/images/I/x.jpg"}})
        self.state.save(UnitState(path="A/B", status="review", asin="B005EHN61O", detail=detail))

        def fake(url, dest):
            pathlib.Path(dest).write_bytes(b"x" * 3000)
            return True

        with mock.patch.object(catalog, "download", side_effect=fake):
            self.assertIsNotNone(covers.fetch_by_asin(self.state, "B005EHN61O"))

    def test_a_pinned_asin_counts_as_known(self):
        self.state.save(UnitState(path="A/B", status="review"))
        self.state.set_asin_override("A/B", "B00PINNED1")
        self.assertTrue(self.state.has_asin("B00PINNED1"))
        self.assertFalse(self.state.has_asin("B00OTHER11"))


if __name__ == "__main__":
    unittest.main()

"""Cover art: cache once, serve from disk, and never need the network to render."""
import json
import pathlib
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from audiobind import catalog, covers, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 4000 + b"\xff\xd9"


class CoverBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        self.state = State(self.root / "cfg")
        self._state_dir, self._output_root = CONFIG.state_dir, CONFIG.output_root
        CONFIG.state_dir = self.root / "cfg"
        CONFIG.output_root = self.root / "out"
        CONFIG.output_root.mkdir(parents=True, exist_ok=True)
        self._download = catalog.download
        self.downloads = []

    def tearDown(self):
        catalog.download = self._download
        CONFIG.state_dir, CONFIG.output_root = self._state_dir, self._output_root
        self.state.close()
        self.dir.cleanup()

    def fake_download(self, ok=True, payload=JPEG):
        def _download(url, destination):
            self.downloads.append(url)
            if not ok:
                return False
            pathlib.Path(destination).write_bytes(payload)
            return True
        catalog.download = _download


class TestCoverCache(CoverBase):
    def test_asin_is_validated_before_touching_the_filesystem(self):
        self.assertTrue(covers.valid_asin("B005EHN61O"))
        self.assertFalse(covers.valid_asin("../../etc/passwd"))
        self.assertFalse(covers.valid_asin("short"))
        self.assertFalse(covers.valid_asin(""))
        self.fake_download()
        self.assertIsNone(covers.fetch_by_asin(self.state, "../../etc/passwd"))
        self.assertEqual(self.downloads, [])

    def test_fetches_once_then_serves_from_disk(self):
        self.fake_download()
        first = covers.fetch_by_asin(self.state, "B005EHN61O")
        self.assertIsNotNone(first)
        self.assertEqual(first.read_bytes(), JPEG)
        second = covers.fetch_by_asin(self.state, "B005EHN61O")
        self.assertEqual(first, second)
        self.assertEqual(len(self.downloads), 1, "the cover was fetched more than once")

    def test_cached_cover_is_served_with_no_network_at_all(self):
        covers.cache_dir().mkdir(parents=True, exist_ok=True)
        covers.cached_path("B005EHN61O").write_bytes(JPEG)

        def explode(url, destination):
            raise AssertionError("the network was used for a cached cover")
        catalog.download = explode
        self.assertEqual(covers.fetch_by_asin(self.state, "B005EHN61O").read_bytes(), JPEG)

    def test_a_failed_fetch_is_remembered_and_not_retried(self):
        self.fake_download(ok=False)
        self.assertIsNone(covers.fetch_by_asin(self.state, "B00FAILFAIL"))
        self.assertIsNone(covers.fetch_by_asin(self.state, "B00FAILFAIL"))
        self.assertEqual(len(self.downloads), 1, "a dead cover was re-requested")

    def test_a_stale_miss_marker_allows_a_retry(self):
        self.fake_download(ok=False)
        covers.fetch_by_asin(self.state, "B00FAILFAIL")
        marker = covers.cache_dir() / "B00FAILFAIL.miss"
        old = time.time() - covers.MISS_TTL_SECONDS - 60
        import os
        os.utime(marker, (old, old))
        self.fake_download(ok=True)
        self.assertIsNotNone(covers.fetch_by_asin(self.state, "B00FAILFAIL"))

    def test_no_partial_file_is_left_behind_on_failure(self):
        self.fake_download(ok=False)
        covers.fetch_by_asin(self.state, "B00FAILFAIL")
        self.assertEqual(list(covers.cache_dir().glob("*.part")), [])

    def test_stored_catalog_url_is_preferred_over_the_asin_fallback(self):
        detail = json.dumps({"match": {"asin": "B005EHN61O",
                                       "image": "https://example.test/cover-500.jpg"}})
        self.state.save(UnitState(path="A/B", status="converted", asin="B005EHN61O",
                                  detail=detail))
        self.assertEqual(covers.stored_image_url(self.state, "B005EHN61O"),
                         "https://example.test/cover-500.jpg")

    def test_falls_back_to_the_asin_url_when_nothing_was_stored(self):
        url = covers.stored_image_url(self.state, "B005EHN61O")
        self.assertIn("B005EHN61O", url)
        self.assertTrue(url.startswith("https://"))

    def test_published_cover_is_used_for_converted_books(self):
        book_dir = CONFIG.output_root / "Michael Lewis" / "Moneyball"
        book_dir.mkdir(parents=True)
        (book_dir / "cover.jpg").write_bytes(JPEG)
        self.state.save(UnitState(path="src/Moneyball", status="converted",
                                  output=str(book_dir / "Moneyball.m4b")))
        found = covers.resolve(self.state, rel_path="src/Moneyball")
        self.assertEqual(found, (book_dir / "cover.jpg").resolve())

    def test_a_cover_outside_the_output_tree_is_refused(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "cover.jpg").write_bytes(JPEG)
        self.state.save(UnitState(path="src/Escape", status="converted",
                                  output=str(outside / "Escape.m4b")))
        self.assertIsNone(covers.published_cover(self.state, "src/Escape"))

    def test_unknown_unit_has_no_cover(self):
        self.assertIsNone(covers.resolve(self.state, rel_path="nope/not/here"))


class TestCoverEndpointAndMarkup(CoverBase):
    def test_endpoint_serves_bytes_then_404s_for_the_unknown(self):
        self.fake_download()
        CONFIG.web_port = 0                   # kernel picks a free port: no contention
        web.Handler.state = self.state
        self.state.save(UnitState(path="A/B", status="ready", asin="B005EHN61O", title="B"))
        server = web.serve(self.state)
        url = f"http://127.0.0.1:{CONFIG.web_port}/cover"
        try:
            with urllib.request.urlopen(f"{url}?asin=B005EHN61O",
                                        timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                self.assertEqual(response.read(), JPEG)
            self.fake_download(ok=False)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                with urllib.request.urlopen(f"{url}?asin=B00NOTHING1",
                                            timeout=10) as response:
                    response.read()
            caught.exception.close()
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_table_and_panel_carry_cover_boxes(self):
        self.state.save(UnitState(path="A/B", status="ready", asin="B005EHN61O", title="B"))
        page = web.render(self.state)
        self.assertIn('class="ab-covcell"', page)
        self.assertIn('class="ab-thumb"', page)
        self.assertIn("ab-covwrap ab-covbig", page)       # bigger one in the panel
        self.assertIn("/cover?asin=B005EHN61O", page)

    def test_a_unit_with_nothing_to_show_gets_a_bare_placeholder(self):
        self.state.save(UnitState(path="A/Unmatched", status="review", title="Unmatched"))
        page = web.render(self.state)
        self.assertIn('<div class="ab-covwrap"></div>', page)
        self.assertNotIn("/cover?asin=", page)

    def test_converted_books_ask_for_the_published_cover(self):
        self.state.save(UnitState(path="A/Done", status="converted", asin="B005EHN61O",
                                  title="Done", output="/output/A/Done/Done.m4b"))
        page = web.render(self.state)
        self.assertIn("/cover?path=A%2FDone", page)

    def test_colspan_counts_the_cover_column(self):
        self.state.save(UnitState(path="A/B", status="ready", title="B"))
        page = web.render(self.state)
        self.assertEqual(web.TOTAL_COLUMNS, len(web.COLUMNS) + 1)
        self.assertIn(f'colspan="{web.TOTAL_COLUMNS}"', page)
        self.assertEqual(page.count("<col "), 0 + page.count('<col style='))


class TestCatalogImage(unittest.TestCase):
    def test_product_image_prefers_the_largest(self):
        raw = {"product_images": {"500": "small.jpg", "1000": "big.jpg"}}
        self.assertEqual(catalog.product_image(raw), "big.jpg")

    def test_product_image_takes_whatever_is_there(self):
        self.assertEqual(catalog.product_image({"product_images": {"252": "tiny.jpg"}}),
                         "tiny.jpg")
        self.assertEqual(catalog.product_image({}), "")

    def test_search_results_carry_the_image(self):
        raw = {"asin": "B1", "title": "T", "product_images": {"1000": "big.jpg"}}
        self.assertEqual(catalog.Product.from_audible(raw).image, "big.jpg")


if __name__ == "__main__":
    unittest.main()

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobind import catalog, daemon, ingest
from audiobind.control import CONTROL
from audiobind.state import State
from audiobind.units import BookUnit


class TestCatalogueProductValidation(unittest.TestCase):
    def setUp(self):
        catalog.cache_clear()
        self.addCleanup(catalog.cache_clear)

    def test_invalid_cached_product_is_evicted_and_reported(self):
        url = "https://example.test/catalog"
        product = {"asin": "B005EHN61O", "title": "Dune", "runtime_length_min": 60}
        for field, value in (
            ("asin", "../bad"),
            ("title", None),
            ("runtime_length_min", "inf"),
            ("runtime_length_min", 0),
            ("authors", ["wrong shape"]),
        ):
            with self.subTest(field=field, value=value):
                catalog._cache[url] = (time.time(), {"products": [{**product, field: value}]})
                with self.assertLogs("audiobind", level="WARNING") as logs:
                    with self.assertRaisesRegex(RuntimeError, field):
                        catalog.cache_get(url)
                self.assertEqual(len(logs.output), 1)
                self.assertIsNone(catalog.cache_get(url))

    def test_a_product_without_a_runtime_is_still_usable(self):
        # real catalogue responses omit the runtime for some entries; unknown duration is not poison
        url = "https://example.test/catalog"
        for missing in ({"runtime_length_min": None}, {"runtime_length_min": ""}, {}):
            with self.subTest(missing=missing):
                catalog.cache_clear()
                product = {"asin": "B005EHN61O", "title": "Dune", **missing}
                catalog._cache[url] = (time.time(), {"products": [product]})
                self.assertIsNotNone(catalog.cache_get(url))

    def test_a_malformed_product_puts_one_book_in_review_and_scan_continues(self):
        CONTROL.resume()
        CONTROL.clear_stop()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = State(Path(tmp.name) / "state")
        self.addCleanup(state.close)
        units = [BookUnit(name, name, "", [], title_guess=name, rel_path=name)
                 for name in ("Broken", "Healthy")]

        def response(request, **_):
            if "Broken" in request.full_url:
                return io.BytesIO(
                    b'{"products":[{"asin":"B005EHN61O","title":"Broken","runtime_length_min":"bad"}]}'
                )
            return io.BytesIO(b'{"products":[]}')

        opener, method = (catalog._json_opener, "open") if hasattr(catalog, "_json_opener") else (
            catalog.urllib.request, "urlopen"
        )
        with (
            patch.object(daemon, "discover_units", return_value=units),
            patch.object(ingest, "discover", return_value=[]),
            patch.object(daemon.PREFETCH, "schedule"),
            patch.object(daemon, "refresh_series"),
            patch.object(opener, method, side_effect=response),
            patch.object(catalog.time, "sleep"),
        ):
            daemon.scan_once(state)

        self.assertEqual(state.get("Broken").status, "review")
        self.assertIn("runtime_length_min", state.get("Broken").note)
        self.assertIsNotNone(state.get("Healthy"))

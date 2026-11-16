"""0.21.0-dev14: a catalog answer that is valid JSON but not an object is a failed lookup.

Audible / Audnex occasionally answer with a bare list, a string or ``null``. Every caller of
``_get_json`` treats the result as a dict, so the guard lives at that one boundary.
"""

import io
import unittest
from unittest import mock

from audiobind import catalog


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answering(body: bytes):
    return mock.patch.object(catalog.urllib.request, "urlopen", side_effect=lambda *a, **k: _Response(body))


class TestNonObjectPayloads(unittest.TestCase):
    BODIES = (b'["a", "b"]', b'"just a string"', b"null", b"42")

    def setUp(self):
        catalog.cache_clear()
        self.addCleanup(catalog.cache_clear)
        # no politeness gap or backoff sleeps: the shape check must not cost a retry
        sleeper = mock.patch.object(catalog.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_get_json_returns_none_and_caches_nothing(self):
        for raw in self.BODIES:
            with answering(raw) as opened:
                self.assertIsNone(catalog._get_json("https://example.test/x"), raw)
            self.assertEqual(opened.call_count, 1, f"{raw!r} was retried")
            self.assertIsNone(catalog.cache_get("https://example.test/x"), raw)

    def test_every_caller_sees_an_empty_result_instead_of_raising(self):
        for raw in self.BODIES:
            with answering(raw):
                self.assertEqual(catalog.search(title="Dune"), [], raw)
                self.assertEqual(catalog.book("B005EHN61O"), {}, raw)
                self.assertEqual(catalog.chapters("B005EHN61O"), [], raw)
                self.assertEqual(catalog.product_images("B005EHN61O"), {}, raw)
                # the Discover queries keep telling "failed" (None) from "nothing found" ([])
                self.assertIsNone(catalog.query(title="Dune"), raw)
                self.assertIsNone(catalog.series_products("Dune"), raw)
            catalog.cache_clear()

    def test_an_object_still_comes_through_and_is_cached(self):
        with answering(b'{"products": [{"asin": "B005EHN61O", "title": "Dune"}]}'):
            found = catalog.search(title="Dune")
        self.assertEqual([p.asin for p in found], ["B005EHN61O"])


if __name__ == "__main__":
    unittest.main()

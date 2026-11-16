"""0.21.0-dev14: method and path contracts on the web layer.

* a GET on a POST-only route is 405 with ``Allow: POST`` - never the page shell;
* an unknown ``/api/...`` path is a JSON 404 - never the page shell;
* routes that take both methods still answer a GET.
"""

import json
import unittest
import urllib.error
import urllib.request

from audiobind import web

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class MethodCase(LiveServerCase):
    def get(self, path):
        """(status, headers, body bytes) for a signed-in GET."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            out = (exc.code, exc.headers, exc.read())
            exc.close()
            return out


class TestPostOnlyRoutes(MethodCase):
    def test_every_post_only_route_answers_405_with_allow_post(self):
        self.assertGreaterEqual(len(web.POST_ONLY_ROUTES), 11)
        for route in web.POST_ONLY_ROUTES:
            for variant in (route, route + "/", route + "?x=1"):
                status, headers, body = self.get(variant)
                self.assertEqual(status, 405, variant)
                self.assertEqual(headers.get("Allow"), "POST", variant)
                self.assertTrue(headers.get("Content-Type", "").startswith("application/json"), variant)
                self.assertIn("POST", json.loads(body)["error"], variant)

    def test_the_post_only_table_matches_what_do_post_serves(self):
        # each of these is a route do_POST handles; a GET-and-POST route must not be in the table
        for both in ("/api/series", "/api/wishlist", "/api/watcher", "/api/discover/recommendations"):
            self.assertNotIn(both, web.POST_ONLY_ROUTES)
            self.assertEqual(self.get(both)[0], 200, both)

    def test_get_logout_still_keeps_the_session(self):
        self.assertEqual(self.get("/logout")[0], 405)
        self.assertEqual(self.get("/api/state")[0], 200)


class TestUnknownApiPaths(MethodCase):
    def test_unknown_api_path_is_a_json_404(self):
        for path in ("/api/nope", "/api/nope/deeper?x=1", "/api/", "/api", "/api/watcher/test-nothing"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertTrue(headers.get("Content-Type", "").startswith("application/json"), path)
            self.assertEqual(json.loads(body), {"error": "not found"}, path)
            self.assertNotIn(b"<html", body.lower(), path)

    def test_known_get_routes_and_pages_are_unchanged(self):
        for path in ("/api/units", "/api/state", "/api/wishlist", "/"):
            self.assertEqual(self.get(path)[0], 200, path)
        status, headers, _body = self.get("/some/page")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/html"))


if __name__ == "__main__":
    unittest.main()

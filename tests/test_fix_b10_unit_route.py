"""B10: POST /api/unit matches its own path segment, never a bare prefix.

Red on 9c9cae1: ``self.path.startswith("/api/unit")`` also took ``/api/units`` (and ``/api/unitX``) to
the single-unit handler, so the next route added under that name would have been swallowed.
"""

import unittest
from unittest import mock

from audiobind import web
from tests.live import LiveServerCase


class TestB10UnitRouteIsExact(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.routed = []

        def fake_handle_unit(handler):
            self.routed.append(handler.path)
            handler._send(200, b'{"routed":true}', "application/json")

        patcher = mock.patch.object(web.Handler, "_handle_unit", autospec=True, side_effect=fake_handle_unit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_api_units_does_not_reach_the_single_unit_handler(self):
        for path in ("/api/units", "/api/units/", "/api/units?status=held", "/api/unitx"):
            with self.subTest(path=path):
                status, body = self.post({"action": "retry", "path": "A/B"}, path=path)
                self.assertEqual(status, 404, body)
        self.assertEqual(self.routed, [])

    def test_api_unit_and_its_sub_paths_still_do(self):
        paths = ("/api/unit", "/api/unit/", "/api/unit?x=1", "/api/unit/A%2FB")
        for path in paths:
            with self.subTest(path=path):
                status, body = self.post({"action": "retry", "path": "A/B"}, path=path)
                self.assertEqual((status, body), (200, {"routed": True}))
        self.assertEqual(self.routed, list(paths))


if __name__ == "__main__":
    unittest.main()

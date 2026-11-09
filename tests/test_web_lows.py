"""v0.14.4 web LOWs: single URL decode, Edit ASIN fallback, one action function."""

import unittest

from audiobind import web
from audiobind.state import UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 4000 + b"\xff\xd9"


class TestSingleDecode(LiveServerCase):
    def test_cover_path_with_a_literal_percent_resolves(self):
        book = self.out / "A" / "100%41 Proof"
        book.mkdir(parents=True)
        (book / "cover.jpg").write_bytes(JPEG)
        self.state.save(
            UnitState(path="A/100%41 Proof", status="converted", output=str(book / "100%41 Proof.m4b"))
        )
        status, body = self.request("/cover?path=" + "A%2F100%2541%20Proof", headers={}, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, JPEG)

    def test_search_query_is_decoded_once(self):
        self.state.save(UnitState(path="X/Plan", status="converted", title="Plan %41"))
        self.state.save(UnitState(path="X/Banana", status="converted", title="Banana"))
        status, body = self.request("/?q=%2541", headers={}, raw=True)
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn('data-path="X/Plan"', page)
        self.assertNotIn('data-path="X/Banana"', page)  # "%41" decoded twice would be "A"
        self.assertIn('value="%41"', page)


class TestActionScript(unittest.TestCase):
    def test_one_action_function(self):
        self.assertIn("function abUnitAction(btn, action)", web.SCRIPT)
        self.assertNotIn("function rowAction(", web.SCRIPT)
        self.assertNotIn("function unitAction(", web.SCRIPT)

    def test_every_button_uses_it(self):
        import pathlib
        import tempfile

        from audiobind.state import State

        with tempfile.TemporaryDirectory() as d:
            st = State(pathlib.Path(d))
            try:
                st.save(UnitState(path="A/B", status="review", title="B", confidence="LOW"))
                page = web.render(st)
            finally:
                st.close()
        self.assertNotIn("rowAction(", page)
        self.assertNotIn("onclick=\"unitAction(", page)
        self.assertIn("abUnitAction(this,'retry')", page)
        self.assertIn("abUnitAction(this,'force')", page)
        self.assertIn("abUnitAction(this,'set_asin')", page)

    def test_action_errors_are_shown(self):
        # a refused ASIN (400) used to reload silently; now the error reaches the toast
        self.assertIn("abToast(out.res.error", web.SCRIPT)


class TestEditAsinFallback(unittest.TestCase):
    def test_shelf_cards_carry_their_status(self):
        html = web._render_shelf([UnitState(path="A/B", status="held", title="B")])
        self.assertIn('data-path="A/B" data-status="held"', html)

    def test_edit_asin_falls_back_to_a_view_with_the_row(self):
        script = web.SCRIPT
        self.assertIn("'&open=' + encodeURIComponent(path)", script)
        self.assertIn("'&q=' + encodeURIComponent(path)", script)
        self.assertIn("get('open')", script)  # the reloaded page opens that panel


if __name__ == "__main__":
    unittest.main()

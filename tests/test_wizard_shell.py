"""The "Set up downloading" wizard, pass A: the page, the stored progress, the guard, the Host pre-fill."""

import re
import unittest
import unittest.mock
import urllib.error
import urllib.request

from audiobind import wizard

try:
    from live import LiveServerCase, seed_account
except ImportError:
    from tests.live import LiveServerCase, seed_account


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WizardCase(LiveServerCase):
    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode()
            finally:
                exc.close()

    def set_mode(self, mode):
        self.state.set_meta(wizard.META_MODE, mode)


class TestPage(WizardCase):
    def test_the_page_is_served_with_the_rail_and_the_step_indicator(self):
        status, page = self.get(wizard.PATH)
        self.assertEqual(status, 200)
        self.assertIn("<title>Audiobind &#183; Set up downloading</title>", page)
        self.assertIn('<aside class="ab-rail">', page)
        for name in ("How books arrive", "Download client", "The address the tracker sees", "Your tracker", "Done"):
            self.assertIn(name, page)
        visible = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", page, flags=re.S)
        self.assertNotRegex(visible, r"\{[a-z_]+\}", "no unexpanded placeholder")

    def test_in_the_tracker_flow_every_other_step_and_done_is_a_link(self):
        self.set_mode(wizard.MODE_TRACKER)
        _status, page = self.get(wizard.PATH + "?step=2")
        indicator = re.search(r'<ol class="ab-wiz-steps">.*?</ol>', page, re.S).group(0)
        self.assertEqual(indicator.count("<a "), 4)
        self.assertIn('aria-current="step"', indicator)
        self.assertNotIn("aria-disabled", indicator)
        for target in ("1", "3", "4", "done"):
            self.assertIn(f'href="{wizard.PATH}?step={target}"', indicator)

    def test_no_step_says_it_is_not_built(self):
        self.set_mode(wizard.MODE_TRACKER)
        for step in ("1", "2", "3", "4", "done"):
            with self.subTest(step=step):
                _status, page = self.get(f"{wizard.PATH}?step={step}")
                self.assertNotIn("is not built yet", page)

    def test_the_wizard_stores_no_setting_and_is_not_on_the_settings_page(self):
        from audiobind import web
        from audiobind.config import SETTING_KEYS

        self.set_mode(wizard.MODE_TRACKER)
        self.get(wizard.PATH + "?step=2")
        settings_page = web.render_settings(self.state)
        for key in (wizard.META_STEP, wizard.META_MODE, wizard.META_DISMISSED):
            self.assertFalse(key.startswith("setting_"))
            self.assertNotIn(key, SETTING_KEYS)
            self.assertNotIn(key, settings_page)


class TestProgress(WizardCase):
    def test_a_fresh_install_starts_at_step_one(self):
        self.assertEqual(wizard.read(self.state), {"mode": "", "step": 1, "dismissed": False})
        _status, page = self.get(wizard.PATH)
        self.assertIn("Step 1 of 4", page)

    def test_the_step_shown_is_stored_and_a_reload_resumes_there(self):
        self.set_mode(wizard.MODE_TRACKER)
        self.get(wizard.PATH + "?step=3")
        self.assertEqual(wizard.read(self.state)["step"], 3)
        # a restart: nothing in memory, only the settings store. The page with no ?step= resumes at 3.
        from audiobind.state import State
        from audiobind import web

        self.state.close()
        self.state = State(self.root / "state")
        web.Handler.state = self.state
        self.assertEqual(wizard.read(self.state)["step"], 3)
        _status, page = self.get(wizard.PATH)
        self.assertIn("Step 3 of 4", page)

    def test_nonsense_in_the_store_or_the_address_bar_lands_on_a_real_step(self):
        self.state.set_meta(wizard.META_STEP, "seven")
        self.state.set_meta(wizard.META_MODE, "teleport")
        self.assertEqual(wizard.read(self.state), {"mode": "", "step": 1, "dismissed": False})
        self.set_mode(wizard.MODE_TRACKER)
        for bad in ("0", "9", "-1", "x", "2.5", "%00"):
            with self.subTest(step=bad):
                status, page = self.get(f"{wizard.PATH}?step={bad}")
                self.assertEqual(status, 200)
                self.assertRegex(page, r"Step \d of 4")

    def test_only_the_tracker_answer_reaches_later_steps(self):
        for mode in ("", wizard.MODE_LIBRARY, wizard.MODE_FILES):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                _status, page = self.get(wizard.PATH + "?step=2")
                self.assertIn("Step 1 of 4", page)
                self.assertEqual(wizard.read(self.state)["step"], 1)

    def test_a_reload_writes_nothing(self):
        self.set_mode(wizard.MODE_TRACKER)
        self.get(wizard.PATH + "?step=2")
        with unittest.mock.patch.object(self.state, "set_meta") as write:
            self.get(wizard.PATH + "?step=2")
            self.get(wizard.PATH)
        write.assert_not_called()


class TestSignedOut(WizardCase):
    signed_in = False

    def test_the_page_is_not_reachable_before_an_account_exists(self):
        opener = urllib.request.build_opener(_NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(self.base + wizard.PATH, timeout=10)
        self.assertEqual(caught.exception.code, 303)
        self.assertEqual(caught.exception.headers["Location"], "/setup")
        caught.exception.close()

    def test_the_page_is_not_reachable_when_signed_out(self):
        seed_account(self.state)  # an account exists; this request carries no session
        opener = urllib.request.build_opener(_NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(self.base + wizard.PATH + "?step=2", timeout=10)
        self.assertEqual(caught.exception.code, 303)
        self.assertEqual(caught.exception.headers["Location"], "/login")
        caught.exception.close()
        self.assertEqual(wizard.read(self.state)["step"], 1, "a refused request stores nothing")


class TestHostPrefill(unittest.TestCase):
    def test_the_address_is_the_host_the_browser_used_with_the_default_port(self):
        self.assertEqual(wizard.default_client_url("192.168.1.10:8686"), "http://192.168.1.10:8080")
        self.assertEqual(wizard.default_client_url("Tower.local"), "http://tower.local:8080")
        self.assertEqual(wizard.default_client_url("[fd00::5]:8686"), "http://[fd00::5]:8080")
        self.assertEqual(wizard.default_client_url("tower.local."), "http://tower.local:8080")

    def test_a_hostile_host_header_gives_nothing(self):
        hostile = (
            "evil.example/path", "evil.example/../x", "user@evil.example", "evil.example?x=1", "evil.example#f",
            "http://evil.example", "javascript:alert(1)", "a b", "a\tb", "evil.example\r\nX: 1", "evil.example:80/x",
            "evil.example:abc", '"><script>x</script>', "-bad.example", "bad-.example", "a..b", ".", "",
            "x" * 300, "[::1", "[::1]junk", "evil.example:8080:9090", "%65vil.example", "evil_host.example",
        )
        for value in hostile:
            with self.subTest(host=value):
                self.assertEqual(wizard.safe_host(value), "")
                self.assertEqual(wizard.default_client_url(value), "")

    def test_a_host_the_server_would_refuse_gives_nothing(self):
        only_tower = lambda name: name == "tower.local"  # noqa: E731
        self.assertEqual(wizard.default_client_url("tower.local:8686", only_tower), "http://tower.local:8080")
        self.assertEqual(wizard.default_client_url("other.example", only_tower), "")
        self.assertEqual(wizard.default_client_url(None), "")

    def test_the_loopback_retry_keeps_the_port(self):
        self.assertEqual(wizard.loopback_url("http://192.168.1.10:8080"), "http://127.0.0.1:8080")
        self.assertEqual(wizard.loopback_url("http://tower.local:9000/qb"), "http://127.0.0.1:9000")
        self.assertEqual(wizard.loopback_url("https://tower.local"), "https://127.0.0.1:443")
        for same in ("http://127.0.0.1:8080", "http://localhost:8080", "", "scgi://x:5000", "not a url", "http://:80"):
            with self.subTest(url=same):
                self.assertEqual(wizard.loopback_url(same), "")


if __name__ == "__main__":
    unittest.main()

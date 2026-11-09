"""Detail panel: narrator shown as a book fact, other editions with one-click pins."""

import json
import pathlib
import tempfile
import unittest

from audiobind import web
from audiobind.state import State, UnitState


def review_unit(**kw):
    kw.setdefault("path", "Michael Lewis/Moneyball")
    kw.setdefault("status", "review")
    kw.setdefault("confidence", "MEDIUM")
    detail = {
        "reopen": False,
        "action": "review",
        "hours": 10.43,
        "match": {
            "asin": "B00BEST",
            "title": "Moneyball",
            "authors": ["Michael Lewis"],
            "narrators": ["Scott Brick"],
            "hours": 10.4,
            "confidence": "MEDIUM",
            "image": "",
            "asin_pin_failed": "",
        },
        "source": {"parts": 1, "files": 3, "mb": 300, "hours": 10.43, "codecs": [], "channels": []},
        "output": None,
    }
    kw.setdefault("detail", json.dumps(detail))
    kw.setdefault("asin", "B00BEST")
    kw.setdefault(
        "alternates",
        json.dumps(
            [
                {
                    "asin": "B00ALT1",
                    "title": "Moneyball (Unabridged)",
                    "authors": ["Michael Lewis"],
                    "score": 0.91,
                },
                {
                    "asin": "B00ALT2",
                    "title": "Moneyball (Abridged)",
                    "authors": ["Michael Lewis"],
                    "score": 0.88,
                },
            ]
        ),
    )
    return UnitState(**kw)


class TestEditions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(review_unit())

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def page(self):
        return web.render(self.state)

    def test_narrator_shown_as_a_book_fact(self):
        self.assertIn("Narrated by <b>Scott Brick</b>", self.page())

    def test_editor_narrator_field_prefilled_from_catalog(self):
        page = self.page()
        self.assertIn('data-field="narrator"', page)
        self.assertIn('placeholder="Scott Brick"', page)

    def test_other_editions_block_lists_alternates(self):
        page = self.page()
        self.assertIn("Other editions seen", page)
        self.assertIn("Moneyball (Unabridged)", page)
        self.assertIn("Moneyball (Abridged)", page)
        self.assertEqual(page.count("Use this edition"), 2)

    def test_current_asin_is_not_offered_again(self):
        self.assertNotIn('data-asin="B00BEST"', self.page())

    def test_pin_button_uses_the_existing_asin_endpoint(self):
        page = self.page()
        self.assertIn("abUseEdition(this)", page)
        self.assertIn("action:'set_asin'", page)

    def test_no_alternates_no_block(self):
        self.state.save(review_unit(alternates=""))
        self.assertNotIn("Other editions seen", self.page())

    def test_corrupt_alternates_json_is_ignored(self):
        self.state.save(review_unit(alternates="not json"))
        self.assertNotIn("Other editions seen", self.page())


class TestSettingsPageProfiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def page(self):
        return web.render_settings(self.state)

    def test_profile_select_present_with_three_named_profiles(self):
        page = self.page()
        self.assertIn('id="ab-profile"', page)
        for label in ("Balanced", "High", "Source-match", "Custom"):
            self.assertIn(label, page)

    def test_profile_is_not_a_stored_setting(self):
        # the select must not carry data-setting, or the POST would send it
        self.assertNotIn('data-setting="ab-profile"', self.page())

    def test_profile_script_defines_the_presets(self):
        page = self.page()
        self.assertIn("abApplyProfile", page)
        self.assertIn("abSyncProfile", page)
        self.assertIn("audio_bitrate: '64k'", page)  # source-match floor
        self.assertIn("audio_bitrate: '128k'", page)  # balanced floor
        self.assertIn("audio_bitrate: '256k'", page)  # high floor

    def test_settings_script_is_valid_javascript(self):
        import re
        import shutil
        import subprocess

        page = self.page()
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertEqual(len(scripts), 1)
        if shutil.which("node"):
            js = pathlib.Path(self.dir.name) / "settings.js"
            js.write_text(scripts[0], encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[:400])

    def test_existing_six_settings_unchanged(self):
        page = self.page()
        for key in ("auto_convert", "audio_bitrate", "runtime_delta", "scan_interval", "nice", "region"):
            self.assertIn(f'data-setting="{key}"', page)


if __name__ == "__main__":
    unittest.main()

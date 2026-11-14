"""v0.14.4: the settings page reflects the real environment, not a hardcoded copy."""

import re
import unittest

from audiobind import config, web
from audiobind.config import CONFIG

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


class EnvCase(LiveServerCase):
    ENV = {}

    def setUp(self):
        super().setUp()
        self.saved_env = {k: getattr(CONFIG, k) for k in self.ENV}
        for key, value in self.ENV.items():
            setattr(CONFIG, key, value)
        config._EFFECTIVE.clear()

    def tearDown(self):
        for key, value in self.saved_env.items():
            setattr(CONFIG, key, value)
        config._EFFECTIVE.clear()
        super().tearDown()

    def setting_html(self, key):
        page = web.render_settings(self.state)
        found = re.search(rf'<div class="ab-setting" data-setting="{key}">(.*?)</div></div>|'
                          rf'<div class="ab-setting" data-setting="{key}">(.*?)</select></div>', page, re.S)
        self.assertIsNotNone(found, key)
        return found.group(0)


class TestEnvDefaultsAreLive(EnvCase):
    ENV = {"max_runtime_delta": 0.40, "auto_convert": "medium", "scan_interval_min": 2, "nice": 5}

    def test_runtime_delta_shows_the_env_value(self):
        self.assertEqual(config.setting_effective("runtime_delta"), 40)
        html = self.setting_html("runtime_delta")
        self.assertIn('value="40"', html)
        self.assertIn('data-initial="40"', html)

    def test_env_defaults_are_derived_from_config(self):
        self.assertEqual(
            config.setting_env_defaults(),
            {
                "auto_convert": "MEDIUM",
                "audio_bitrate": CONFIG.audio_bitrate.lower(),
                "runtime_delta": 40,
                "scan_interval": 2,
                "nice": 5,
                "region": CONFIG.region,
                "expected_egress": "",
                "vip": "YES" if CONFIG.mam_vip else "NO",
            },
        )

    def test_reset_returns_select_cased_tier(self):
        status, body = self.post({"reset": True}, path="/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["env_defaults"]["auto_convert"], "MEDIUM")
        self.assertIn('<option value="MEDIUM" selected>', self.setting_html("auto_convert"))
        self.assertEqual(body["env_defaults"]["runtime_delta"], 40)

    def test_low_env_scan_interval_renders_and_saves(self):
        html = self.setting_html("scan_interval")
        self.assertIn('min="1"', html)
        self.assertIn('value="2"', html)
        status, body = self.post({"settings": {"scan_interval": 2}}, path="/api/settings")
        self.assertEqual(status, 200, body)
        status, body = self.post({"settings": {"scan_interval": 1}}, path="/api/settings")
        self.assertEqual(status, 200, body)
        status, body = self.post({"settings": {"scan_interval": 0}}, path="/api/settings")
        self.assertEqual(status, 400)

    def test_saving_one_field_leaves_the_others_following_the_env(self):
        status, body = self.post({"settings": {"nice": 12}}, path="/api/settings")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["custom"], ["nice"])
        self.assertIsNone(self.state.get_meta("setting_runtime_delta"))
        self.assertEqual(config.effective_val("max_runtime_delta"), 0.40)
        # a later save of another field still reports every custom one
        status, body = self.post({"settings": {"region": "uk"}}, path="/api/settings")
        self.assertEqual(sorted(body["custom"]), ["nice", "region"])


class TestUnusualEnvValues(EnvCase):
    ENV = {"audio_bitrate": "96k", "region": "fr", "max_runtime_delta": 0.60}

    def test_unusual_env_values_are_offered_and_accepted(self):
        self.assertIn('<option value="96k" selected>', self.setting_html("audio_bitrate"))
        self.assertIn('<option value="fr" selected>', self.setting_html("region"))
        self.assertIn('max="60"', self.setting_html("runtime_delta"))
        status, body = self.post(
            {"settings": {"audio_bitrate": "96k", "region": "fr", "runtime_delta": 60}}, path="/api/settings"
        )
        self.assertEqual(status, 200, body)

    def test_values_outside_the_choices_are_still_refused(self):
        status, body = self.post({"settings": {"audio_bitrate": "1000k"}}, path="/api/settings")
        self.assertEqual(status, 400)
        self.assertIn("audio_bitrate", body["error"])


class TestSettingsScript(unittest.TestCase):
    def test_save_reads_the_control_inside_each_wrapper(self):
        # [data-setting] is the wrapper div - reading .value off it posted nothing at all
        self.assertIn("wrap.querySelector('select, input')", web.SETTINGS_SCRIPT)
        self.assertIn("el.dataset.initial", web.SETTINGS_SCRIPT)


if __name__ == "__main__":
    unittest.main()

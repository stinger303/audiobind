"""D6 item 1: Settings > Watcher > Search category is a dropdown when the preset file supplies the tracker's
own list, a text field when it does not, and a saved value that is not in the list is never lost."""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from audiobind import config, preset, web

try:
    from test_setup_checks import GOOD, PresetWebCase
except ImportError:
    from tests.test_setup_checks import GOOD, PresetWebCase

CATS = {"13": "Audiobooks", "14": "Ebooks"}


def field(page: str) -> str:
    """The Search category block of the page."""
    start = page.index('data-setting="tracker_search_category"')
    return page[start:page.index('data-setting="tracker_download_path"', start)]


class TestCategoriesParse(unittest.TestCase):
    def parse(self, categories):
        data = {**GOOD} if categories is None else {**GOOD, "categories": categories}
        return preset.parse(json.dumps(data))

    def test_absent_and_empty_mean_no_dropdown(self):
        for cats in (None, {}):
            with self.subTest(cats=cats):
                found, why = self.parse(cats)
                self.assertEqual((why, found.categories), ("", {}))

    def test_a_list_is_kept_in_order(self):
        found, why = self.parse(CATS)
        self.assertEqual((why, found.categories), ("", CATS))
        self.assertEqual(list(found.categories), ["13", "14"])

    def test_bad_shapes_make_the_whole_file_unusable(self):
        for cats in (["13"], "13", {"13": 5}, {"13": ""}, {"": "Nothing"}, {"13": "a\x01b"}, {"1\n3": "Name"},
                     {str(i): "x" for i in range(preset.MAX_CATEGORIES + 1)}):
            with self.subTest(cats=cats):
                found, why = self.parse(cats)
                self.assertIsNone(found)
                self.assertTrue(why)


class TestCategoryField(PresetWebCase):
    def page(self):
        return web.render_settings(self.state, "watcher")

    def test_with_categories_it_is_a_select_showing_name_and_value_saving_the_bare_key(self):
        self.put({**GOOD, "categories": CATS})
        block = field(self.page())
        self.assertIn("<select", block)
        self.assertNotIn('type="text"', block)
        self.assertIn('<option value="13">Audiobooks (13)</option>', block)
        self.assertIn('<option value="14">Ebooks (14)</option>', block)
        self.assertNotIn('value="Audiobooks (13)"', block)  # the label is never the value
        # saving what the select would post stores the bare key, not the label
        self.save({"settings": {"tracker_search_category": "13"}})
        self.assertEqual(config.setting_effective("tracker_search_category"), "13")
        self.assertIn('<option value="13" selected>Audiobooks (13)</option>', field(self.page()))
        self.assertIn('data-initial="13"', field(self.page()))

    def test_empty_value_is_the_none_option_and_is_selected(self):
        self.put({**GOOD, "categories": CATS})
        block = field(self.page())
        self.assertIn('<option value="" selected>None (leave {{category}} out)</option>', block)
        self.assertNotIn("not in the preset list", block)

    def test_a_saved_value_outside_the_list_survives_as_its_own_option(self):
        self.put({**GOOD, "categories": CATS})
        self.save({"settings": {"tracker_search_category": "99"}})
        block = field(self.page())
        self.assertIn('<option value="99" selected>99 (not in the preset list)</option>', block)
        self.assertEqual(len(re.findall(r" selected", block)), 1)
        self.assertEqual(config.setting_effective("tracker_search_category"), "99")

    def test_no_preset_keeps_the_text_field_with_an_honest_hint(self):
        for data in (None, GOOD, {**GOOD, "categories": {}}):
            with self.subTest(preset=data):
                if data is not None:
                    self.put(data)
                self.save({"settings": {"tracker_search_category": "13"}})
                block = field(self.page())
                self.assertIn('type="text"', block)
                self.assertNotIn("<select", block)
                self.assertIn('value="13"', block)
                self.assertIn("tracker's own value", block)
                self.assertIn("not one of Audiobind", block)
                self.assertIn("preset file can supply the list", block)
                self.assertIn("Empty = {{category}} is left out", block)

    def test_a_broken_preset_file_falls_back_to_the_text_field(self):
        self.put({**GOOD, "categories": ["13"]})
        self.assertIn('type="text"', field(self.page()))

    def test_applying_the_preset_does_not_change_the_saved_category(self):
        self.put({**GOOD, "categories": CATS})
        self.save({"settings": {"tracker_search_category": "99"}})
        status, body = self.fill(overwrite=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(config.setting_effective("tracker_search_category"), "99")

    def test_the_shipped_example_has_two_fictional_categories(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        found, why = preset.parse((root / "docs" / "tracker-preset.example.json").read_text())
        self.assertEqual(why, "")
        self.assertEqual(len(found.categories), 2)


class TestCategoriesOnlyPreset(PresetWebCase):
    """D7: a preset may carry fillable settings, a category list, or both - never neither."""

    ONLY = {"label": "Categories only", "categories": CATS}
    BUTTON = "Fill the tracker fields from preset file"

    def page(self):
        return web.render_settings(self.state, "watcher")

    def test_parse_accepts_absent_or_empty_settings_with_a_list(self):
        for data in (self.ONLY, {**self.ONLY, "settings": {}}, {**self.ONLY, "settings": {"TRACKER_URL": ""}}):
            with self.subTest(data=data):
                found, why = preset.parse(json.dumps(data))
                self.assertEqual((why, found.values, found.categories), ("", {}, CATS))

    def test_categories_only_shows_the_dropdown_names_the_preset_and_offers_no_button(self):
        self.put(self.ONLY)
        page = self.page()
        self.assertIn("<select", field(page))
        self.assertIn('<option value="13">Audiobooks (13)</option>', field(page))
        self.assertIn("Tracker preset: Categories only", page)
        self.assertNotIn(self.BUTTON, page)
        self.assertNotIn('id="ab-preset-fill"', page)
        self.assertNotIn('id="ab-preset-error"', page)
        self.assertNotIn("cannot be used", page)

    def test_settings_only_is_unchanged_a_text_field_and_the_button(self):
        self.put(GOOD)
        page = self.page()
        self.assertIn('type="text"', field(page))
        self.assertNotIn("<select", field(page))
        self.assertIn(self.BUTTON, page)
        self.assertNotIn('id="ab-preset-error"', page)

    def test_both_gives_the_dropdown_and_the_button(self):
        self.put({**GOOD, "categories": CATS})
        page = self.page()
        self.assertIn("<select", field(page))
        self.assertIn(self.BUTTON, page)
        self.assertNotIn('id="ab-preset-error"', page)

    def test_neither_keeps_the_existing_rejection(self):
        for data, message in (({"label": "x"}, "needs"), ({"label": "x", "settings": {}}, "needs"),
                              ({"label": "x", "settings": {}, "categories": {}}, "needs"),
                              ({"label": "x", "settings": {"TRACKER_URL": ""}}, "nothing to fill")):
            with self.subTest(data=data):
                found, why = preset.parse(json.dumps(data))
                self.assertIsNone(found)
                self.assertIn(message, why)
        self.put({"label": "x", "settings": {}})
        page = self.page()
        self.assertIn('id="ab-preset-error"', page)
        self.assertIn("cannot be used", page)
        self.assertIn('type="text"', field(page))

    def test_a_label_and_the_other_rules_still_apply_to_a_categories_only_file(self):
        for data in ({"categories": CATS}, {"label": "  ", "categories": CATS},
                     {**self.ONLY, "settings": {"TRACKER_SESSION": "x"}},
                     {**self.ONLY, "settings": {"TRACKER_NOPE": "x"}},
                     {**self.ONLY, "settings": {"TRACKER_URL": "not a url"}},
                     {**self.ONLY, "categories": ["13"]}):
            with self.subTest(data=data):
                found, why = preset.parse(json.dumps(data))
                self.assertIsNone(found)
                self.assertTrue(why)

    def test_the_apply_endpoint_refuses_a_categories_only_preset_and_writes_nothing(self):
        self.put(self.ONLY)
        self.save({"settings": {"tracker_url": "https://kept.example", "tracker_search_category": "99"}})
        keys = list(config.SETTING_KEYS)
        before = {k: self.state.get_meta(f"setting_{k}") for k in keys}
        effective = {k: config.setting_effective(k) for k in keys}
        for payload in ({}, {"overwrite": True}):
            with self.subTest(payload=payload):
                status, body = self.fill(**payload)
                self.assertEqual(status, 400, body)
                self.assertFalse(body["ok"])
                self.assertIn("no fields to fill", body["error"])
        self.assertEqual({k: self.state.get_meta(f"setting_{k}") for k in keys}, before)
        self.assertEqual({k: config.setting_effective(k) for k in keys}, effective)
        self.assertEqual(before["tracker_url"], "https://kept.example")
        self.assertEqual(before["tracker_search_category"], "99")


class TestMembershipLabel(PresetWebCase):
    def test_the_select_reads_vip_slash_premium_and_names_both_words(self):
        page = web.render_settings(self.state, "watcher")
        start = page.index('data-setting="vip"')
        block = page[start:page.index("</select>", start)]
        self.assertIn(">VIP / Premium member<", block)
        self.assertNotIn(">Premium member<", page)
        self.assertIn("Some trackers call it VIP, others Premium", block)
        self.assertIn("skipped instead of attempted", block)
        self.assertIn("TRACKER_VIP", block)

    def test_template_and_docs_use_the_same_words(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        xml = (root / "templates" / "audiobind.xml").read_text()
        self.assertIn('Name="Tracker VIP / Premium member" Target="TRACKER_VIP"', xml)
        self.assertIn("VIP / Premium member", (root / "docs" / "acquisition.md").read_text())
        for text in (xml, (root / "docs" / "acquisition.md").read_text()):
            self.assertNotIn("**Premium member**", text)
            self.assertNotIn("Tracker premium member", text)


if __name__ == "__main__":
    unittest.main()

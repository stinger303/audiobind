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

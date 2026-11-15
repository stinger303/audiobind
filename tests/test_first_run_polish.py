"""0.20.0-dev1: first-run polish - template, channel default, one-book series, clickable rows."""

import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from audiobind import config as config_mod, series, web
from audiobind.state import State

try:
    from test_series import FIVE, book, entry, store
except ImportError:
    from tests.test_series import FIVE, book, entry, store

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "templates" / "audiobind.xml").read_text()


def config_line(target):
    return re.search(rf'<Config [^>]*Target="{target}"[^>]*>', TEMPLATE).group(0)


class TestTemplate(unittest.TestCase):
    def test_access_token_is_advanced_masked_and_leads_with_the_upgrade_case(self):
        line = config_line("AUTH_TOKEN")
        self.assertIn('Display="advanced"', line)
        self.assertIn('Mask="true"', line)
        description = re.search(r'Description="([^"]*)"', line).group(1)
        self.assertTrue(description.startswith("Only for upgrading"))
        self.assertNotIn("Deprecated", description)

    def test_channels_default_to_stereo(self):
        line = config_line("AUDIO_CHANNELS")
        self.assertIn('Default="2"', line)
        self.assertIn("1 = mono, 2 = stereo. Stereo keeps the source layout; mono halves the file size.", line)
        self.assertRegex(TEMPLATE, r'Target="AUDIO_CHANNELS"[^>]*>2</Config>')

    def test_repository_is_pinned_to_a_release_the_changes_announce(self):
        repo = re.search(r"<Repository>([^<]+)</Repository>", TEMPLATE).group(1)
        self.assertRegex(repo, r"^ghcr\.io/stinger303/audiobind:\d+\.\d+\.\d+$")
        self.assertNotIn(":latest", repo)
        self.assertIn(f"### {repo.rsplit(':', 1)[1]}\n", TEMPLATE)


class TestChannelsDefault(unittest.TestCase):
    def load(self, **env):
        clean = {k: v for k, v in os.environ.items() if k != "AUDIO_CHANNELS"}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return config_mod.Config.from_env()

    def test_default_is_stereo(self):
        self.assertEqual(self.load().audio_channels, 2)
        self.assertEqual(config_mod.Config().audio_channels, 2)

    def test_env_one_still_wins(self):
        self.assertEqual(self.load(AUDIO_CHANNELS="1").audio_channels, 1)

    def test_compose_default_matches(self):
        self.assertIn("AUDIO_CHANNELS=2", (ROOT / "docker-compose.yml").read_text())


class OneBookCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        patcher = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.scheduled = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.state.close)

    def names(self):
        return [v["name"] for v in series.aggregate(self.state)["series"]]


class TestOneBookSeries(OneBookCase):
    def test_one_book_with_a_multi_entry_catalog_appears(self):
        self.state.save(book("Lee Child/Reacher/1", "Book 1", seq="1"))
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual((view["owned"], view["total"], view["have"]), (1, 5, 1))

    def test_one_book_with_a_one_entry_catalog_is_hidden(self):
        self.state.save(book("Jo Nesbo/Solo/1", "Solo", "Jo Nesbo", "Solo", "1"))
        store(self.state, "Solo", "Jo Nesbo", [entry("1", "B0SOLO0001", "Solo")])
        self.assertEqual(self.names(), [])

    def test_unfetched_listing_still_shows_the_card_without_a_total(self):
        self.state.save(book("Jo Nesbo/Harry Hole/4", "Redbreast", "Jo Nesbo", "Harry Hole", "4"))
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual(view["source"], series.SOURCE_PENDING)
        self.assertFalse(view["known"])
        self.assertIsNone(view["total"])

    def test_one_book_not_found_or_unreachable_is_hidden(self):
        self.state.save(book("a/b/1", "Only", "Auth", "Guessed Name", "1"))
        key = series.group_key("Guessed Name", "Auth")
        self.state.series_cache_put(key, False, {"reason": "not_found"}, [])
        self.assertEqual(self.names(), [])
        self.state.series_cache_put(key, False, {"reason": "unreachable"}, [])
        self.assertEqual(self.names(), [])

    def test_two_books_without_a_listing_stay(self):
        self.state.save(book("a/b/1", "One", "Auth", "Guessed Name", "1"))
        self.state.save(book("a/b/2", "Two", "Auth", "Guessed Name", "2"))
        self.state.series_cache_put(series.group_key("Guessed Name", "Auth"), False, {"reason": "not_found"}, [])
        self.assertEqual(self.names(), ["Guessed Name"])

    def test_multi_book_series_sort_before_single_book_ones(self):
        self.state.save(book("a/1", "A1", "Zed", "Alpha Single", "1"))
        store(self.state, "Alpha Single", "Zed", [entry(str(n), f"B0A00000{n}", f"A{n}") for n in (1, 2, 3, 4)])
        for n in (1, 2):
            self.state.save(book(f"b/{n}", f"B{n}", "Yan", "Zulu Pair", str(n)))
        store(self.state, "Zulu Pair", "Yan", [entry(str(n), f"B0Z00000{n}", f"Z{n}") for n in (1, 2)])
        self.assertEqual(self.names(), ["Zulu Pair", "Alpha Single"])

    def test_every_held_series_is_queued_for_a_lookup(self):
        self.state.save(book("Jo Nesbo/Harry Hole/4", "Redbreast", "Jo Nesbo", "Harry Hole", "4"))
        series.aggregate(self.state)
        jobs = self.scheduled.call_args[0][1]
        self.assertEqual([j["name"] for j in jobs], ["Harry Hole"])


class TestRowMarkup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.state.close)
        self.state.save(book("Author/Book", "Book", "Author", "", ""))

    def test_row_is_keyboard_reachable_and_collapsed(self):
        page = web.render(self.state)
        row = page.split('<tr class="urow"')[1].split(">", 1)[0]
        for fragment in ('tabindex="0"', 'role="button"', 'aria-expanded="false"',
                         'onkeydown="abRowKey(event,this)"', 'onclick="toggleRow(this, event)"'):
            self.assertIn(fragment, row)
        self.assertIn('class="ab-rowcaret"', page.split('<tr class="urow"')[1].split("</td>")[0])
        self.assertRegex(page, r'<tr class="drow"[^>]*\bhidden>')

    def test_css_and_script_keep_aria_in_sync(self):
        page = web.render(self.state)
        self.assertIn(".ab-tbl tbody tr.urow{cursor:pointer}", page)
        self.assertIn('tr.urow[aria-expanded="true"] .ab-rowcaret{transform:rotate(90deg)}', page)
        toggle = page.split("function toggleRow(row, ev)")[1].split("function abRowKey")[0]
        self.assertIn("abRowSync(row)", toggle)
        self.assertNotIn("window.event", toggle)  # non-standard; Firefox has no window.event
        self.assertIn("toggleRow(row, ev)", page.split("function abRowKey")[1].split("\n}\n")[0])
        self.assertIn("aria-expanded", page.split("function abRowSync(row)")[1].split("function toggleRow")[0])
        key = page.split("function abRowKey")[1].split("\n}\n")[0]
        self.assertIn("'Enter'", key)
        self.assertIn("' '", key)
        self.assertIn("ev.target !== row", key)  # a key on the kebab does not toggle the row
        self.assertIn("closest('a,button,input,select,textarea')", toggle)
        self.assertIn('class="ab-kebabwrap" onclick="event.stopPropagation()"', page)

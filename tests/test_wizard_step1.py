"""Set up downloading, step 1: how do books arrive? Each of the three answers, and the ingest folder status."""

import json
import os
import re
import unittest
from unittest import mock

from audiobind import ingest, wizard
from audiobind.config import CONFIG

try:
    from test_wizard_entry import EntryCase
except ImportError:
    from tests.test_wizard_entry import EntryCase

UNRAID_PATH = ("In Unraid: Docker → Audiobind → Edit → Show more settings → <b>Ingest path</b> → "
               "pick the folder your downloader finishes audiobooks into → Apply.")


class Step1Case(EntryCase):
    def setUp(self):
        super().setUp()
        self.ingest_dir = self.root / "downloads"
        saved = (CONFIG.ingest_dir, ingest._DISABLED_AT_START)
        self.addCleanup(lambda: setattr(CONFIG, "ingest_dir", saved[0]))
        self.addCleanup(lambda: setattr(ingest, "_DISABLED_AT_START", saved[1]))
        CONFIG.ingest_dir = ""
        ingest._DISABLED_AT_START = ""

    def answer(self, mode):
        status, body = self.post(wizard.API, {"action": "mode", "mode": mode})
        self.assertEqual(status, 200, body)
        return self.get(wizard.PATH + "?step=1")[1]

    def mount_ingest(self, folders=3):
        self.ingest_dir.mkdir(exist_ok=True)
        for n in range(folders):
            (self.ingest_dir / f"Book {n}").mkdir(exist_ok=True)
        CONFIG.ingest_dir = str(self.ingest_dir)


class TestTheThreeAnswers(Step1Case):
    def test_the_page_offers_exactly_the_three_choices_and_none_is_picked_yet(self):
        page = self.get(wizard.PATH)[1]
        radios = re.findall(r'<input type="radio" name="ab-arrive" value="(\w+)"( checked)?>', page)
        self.assertEqual([value for value, _ in radios], ["library", "files", "tracker"])
        self.assertEqual([checked for _, checked in radios], ["", "", ""])
        for text in ("Only my library: nothing to fetch", "My own downloader, or I copy files into a folder",
                     "A private tracker, through a download client"):
            self.assertIn(text, page)
        self.assertIn("<fieldset", page)
        self.assertIn("<legend", page)

    def test_a_only_my_library_closes_the_flow_with_the_plain_sentence(self):
        page = self.answer("library")
        self.assertIn("Nothing to fetch.</b> Audiobind will convert what is already in your library.", page)
        self.assertRegex(page, r'<a class="btn ab-save" id="ab-wiz-done" href="/\?status=all">Back to the Library</a>')
        self.assertIn('value="library" checked', page)
        self.assertNotIn("ab-wiz-ingest", page, "no ingest check for someone who fetches nothing")
        self.assertNotIn("Next: the download client", page)
        state = wizard.read(self.state)
        self.assertEqual((state["mode"], state["dismissed"]), ("library", True))
        self.assertNotIn('id="ab-wizcard"', self.get("/")[1], "answered: the card does not ask again")

    def test_b_my_own_downloader_shows_the_ingest_status_and_finishes(self):
        self.mount_ingest(3)
        page = self.answer("files")
        self.assertIn('id="ab-wiz-ingest"', page)
        self.assertIn("3 folders seen", page)
        self.assertIn("That is all.", page)
        self.assertIn('id="ab-wiz-done"', page)
        self.assertNotIn("Next: the download client", page)
        self.assertEqual(wizard.read(self.state)["dismissed"], True)

    def test_c_a_private_tracker_shows_the_ingest_status_and_goes_on_to_step_two(self):
        self.mount_ingest(1)
        page = self.answer("tracker")
        self.assertIn('id="ab-wiz-ingest"', page)
        self.assertIn("1 folder seen", page)
        self.assertIn(f'<a class="btn ab-save" id="ab-wiz-next" href="{wizard.PATH}?step=2">', page)
        self.assertNotIn('id="ab-wiz-done"', page)
        self.assertFalse(wizard.read(self.state)["dismissed"], "the flow is not over")

    def test_only_c_reaches_step_two(self):
        for mode, reaches in (("library", False), ("files", False), ("tracker", True)):
            with self.subTest(mode=mode):
                self.answer(mode)
                page = self.get(wizard.PATH + "?step=2")[1]
                self.assertEqual("Step 2 of 4" in page, reaches)

    def test_a_changed_mind_replaces_the_answer(self):
        self.answer("tracker")
        page = self.answer("library")
        self.assertIn("Nothing to fetch.", page)
        self.assertNotIn("Next: the download client", page)
        self.assertEqual(wizard.read(self.state)["mode"], "library")

    def test_a_bad_answer_is_refused_and_changes_nothing(self):
        self.answer("tracker")
        for bad in ("", "teleport", None, 3, ["tracker"], {"x": 1}):
            with self.subTest(mode=bad):
                status, body = self.post(wizard.API, {"action": "mode", "mode": bad})
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertEqual(wizard.read(self.state)["mode"], "tracker")

    def test_the_choice_is_a_labelled_form_the_keyboard_can_submit(self):
        page = self.get(wizard.PATH)[1]
        self.assertIn('onsubmit="abWizardMode(); return false;"', page)
        self.assertEqual(len(re.findall(r'<label class="ab-wiz-choice"><input type="radio"', page)), 3)
        self.assertIn('type="submit" id="ab-wiz-continue"', page)
        self.assertIn('id="ab-wiz-fb" role="status"', page)


class TestIngestStatus(Step1Case):
    def test_missing_ingest_prints_the_unraid_click_path_verbatim_for_b_and_c(self):
        for mode in ("files", "tracker"):
            with self.subTest(mode=mode):
                page = self.answer(mode)
                self.assertIn(UNRAID_PATH, page)
                self.assertIn("no ingest folder yet", page)
                self.assertIn("it comes back to this step", page)

    def test_a_working_folder_shows_every_check_and_no_click_path(self):
        self.mount_ingest(2)
        page = self.answer("tracker")
        for text in ("is mounted.", "Audiobind can read it.", "does not overlap your library or output folder.",
                     "2 folders seen"):
            self.assertIn(text, page)
        self.assertNotIn(UNRAID_PATH, page)
        self.assertNotIn("ab-bad", re.search(r'<ul class="ab-wiz-checks">.*?</ul>', page, re.S).group(0))

    def test_an_empty_folder_is_fine_and_says_why(self):
        self.mount_ingest(0)
        page = self.answer("tracker")
        self.assertIn("0 folders seen yet. That is fine", page)

    def test_a_folder_that_is_not_mounted_is_a_problem_with_the_fix(self):
        CONFIG.ingest_dir = str(self.root / "nowhere")
        page = self.answer("files")
        self.assertIn("the mount is missing", page)
        self.assertIn(UNRAID_PATH, page)

    def test_a_folder_that_overlaps_the_library_is_a_problem(self):
        inside = self.lib / "incoming"
        inside.mkdir()
        CONFIG.ingest_dir = str(inside)
        status = ingest.status()
        self.assertTrue(status["overlap"])
        self.assertFalse(status["ok"])
        page = self.answer("tracker")
        self.assertIn("It overlaps your library or output folder", page)
        self.assertIn("LIBRARY_ROOT", page)

    def test_a_folder_that_overlaps_the_output_is_a_problem(self):
        CONFIG.ingest_dir = str(self.out)
        self.assertIn("OUTPUT_ROOT", ingest.status()["overlap"])

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_an_unreadable_folder_is_a_problem(self):
        self.mount_ingest(1)
        self.ingest_dir.chmod(0)
        try:
            page = self.answer("files")
        finally:
            self.ingest_dir.chmod(0o755)  # before the temp directory is removed
        self.assertIn("Audiobind cannot read it", page)

    def test_a_half_set_up_folder_names_the_half(self):
        self.ingest_dir.mkdir()
        with mock.patch.object(ingest, "MOUNT_PATH", str(self.ingest_dir)):
            page = self.answer("files")
        self.assertIn("is mounted but Ingest dir is empty", page)

    def test_a_folder_the_daemon_switched_off_says_restart(self):
        self.mount_ingest(1)
        ingest._DISABLED_AT_START = "INGEST_DIR was missing at start"
        page = self.answer("files")
        self.assertIn("Restart the container to turn it on.", page)

    def test_every_status_line_has_a_glyph_and_words_not_only_colour(self):
        self.mount_ingest(1)
        page = self.answer("files")
        items = re.findall(r'<li class="ab-(?:good|bad|warn)">.*?</li>', page, re.S)
        self.assertTrue(items)
        for item in items:
            self.assertIn('aria-hidden="true">', item)
            self.assertRegex(item, r'ab-wiz-sr">(OK|Problem|Note): ')

    def test_looking_writes_nothing_inside_the_folder(self):
        self.mount_ingest(2)
        before = sorted(os.listdir(self.ingest_dir))
        self.answer("tracker")
        self.assertEqual(sorted(os.listdir(self.ingest_dir)), before)

    def test_the_startup_check_still_refuses_an_overlap(self):
        inside = self.lib / "incoming"
        inside.mkdir()
        CONFIG.ingest_dir = str(inside)
        with self.assertLogs("audiobind", level="WARNING") as logs:
            self.assertFalse(ingest.check_at_start())
        self.assertIn("overlaps LIBRARY_ROOT", logs.output[0])


if __name__ == "__main__":
    unittest.main()

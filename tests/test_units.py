import os
import tempfile
import unittest

from audiobind.units import author_like, discover_units, group_key, split_author_title


class TestGroupKey(unittest.TestCase):
    def test_camel_case_parts_collapse(self):
        self.assertEqual(group_key("StoneColdUnabridgedPart1"), group_key("StoneColdUnabridgedPart2"))

    def test_pure_marker_keys_empty_so_caller_uses_parent(self):
        self.assertEqual(group_key("Disc1"), "")
        self.assertEqual(group_key("Part 02"), "")

    def test_distinct_books_stay_distinct(self):
        self.assertNotEqual(group_key("The Enemy"), group_key("Killing Floor"))


class TestSplitAuthorTitle(unittest.TestCase):
    def test_author_then_title(self):
        self.assertEqual(split_author_title("David Baldacci - Stone Cold"), ("David Baldacci", "Stone Cold"))

    def test_title_then_author_is_corrected(self):
        self.assertEqual(split_author_title("Influx - Daniel Suarez"), ("Daniel Suarez", "Influx"))
        self.assertEqual(
            split_author_title("The Easy Way To Stop Smoking - Allen Carr"),
            ("Allen Carr", "The Easy Way To Stop Smoking"),
        )

    def test_by_construction(self):
        self.assertEqual(split_author_title("The Martian by Andy Weir"), ("Andy Weir", "The Martian"))

    def test_bare_title(self):
        self.assertEqual(split_author_title("World War Z"), (None, "World War Z"))

    def test_leading_sequence_number_is_dropped_but_decimal_kept_in_number(self):
        self.assertEqual(split_author_title("02 Die Trying"), (None, "Die Trying"))
        self.assertEqual(split_author_title("15.5 Second Son"), (None, "Second Son"))
        self.assertEqual(split_author_title("14 61 Hours"), (None, "14 61 Hours"))

    def test_numeric_titles_survive(self):
        self.assertEqual(split_author_title("1984"), (None, "1984"))
        self.assertEqual(split_author_title("2001 A Space Odyssey"), (None, "2001 A Space Odyssey"))

    def test_abbreviation_is_not_an_author(self):
        self.assertGreater(author_like("Michael Lewis"), author_like("Private L.A"))


class TestDiscovery(unittest.TestCase):
    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()

    def test_disc_folders_form_one_book(self):
        with tempfile.TemporaryDirectory() as root:
            for disc in (1, 2, 3):
                self._touch(os.path.join(root, "George Orwell - 1984", f"Disc{disc}", "track1.mp3"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 1)
            self.assertEqual(len(units[0].parts), 3)
            self.assertEqual(units[0].author_guess, "George Orwell")

    def test_separate_books_stay_separate(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, "Jack Reacher Series", "01 Killing Floor", "a.mp3"))
            self._touch(os.path.join(root, "Jack Reacher Series", "02 Die Trying", "a.mp3"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 2)
            self.assertEqual(units[0].title_guess, "Killing Floor")

    def test_loose_file_is_a_unit(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, "Nora Roberts - The Guardians.m4b"))
            units = discover_units(root, [".*"])
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0].author_guess, "Nora Roberts")

    def test_hidden_and_system_dirs_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(os.path.join(root, ".bookfix-work", "x", "a.mp3"))
            self._touch(os.path.join(root, "@eaDir", "a.mp3"))
            self.assertEqual(discover_units(root, [".*", "@eaDir"]), [])


class TestActionChoice(unittest.TestCase):
    def _unit(self, files, parts=1):
        from audiobind.units import BookUnit, Part

        return BookUnit(
            path="/lib/x",
            name="x",
            parent="/lib",
            parts=[Part(name=f"p{i}", files=fs) for i, fs in enumerate(files)],
            rel_path="x",
        )

    def test_multi_part_is_merged(self):
        from audiobind.pipeline import choose_action

        unit = self._unit([["/a.mp3"], ["/b.mp3"]], parts=2)
        self.assertEqual(choose_action(unit, "HIGH", True, {"mp3"}), "merge+convert")

    def test_single_mp3_is_converted(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.mp3"]]), "HIGH", True, {"mp3"}), "convert")

    def test_single_m4b_is_remuxed_not_re_encoded(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.m4b"]]), "HIGH", True, {"aac"}), "remux")

    def test_m4a_with_aac_is_also_remuxed(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.m4a"]]), "HIGH", True, {"aac"}), "remux")

    def test_m4b_that_is_not_aac_is_re_encoded(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.m4b"]]), "HIGH", True, {"alac"}), "convert")

    def test_low_confidence_is_reviewed(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.m4b"]]), "LOW", True, {"aac"}), "review")

    def test_runtime_mismatch_is_held(self):
        from audiobind.pipeline import choose_action

        self.assertEqual(choose_action(self._unit([["/a.mp3"]]), "HIGH", False, {"mp3"}), "hold")


if __name__ == "__main__":
    unittest.main()


class TestParsingHygiene(unittest.TestCase):
    """0.21.0-dev7: numeric titles, loose-file extensions, narrators, series prefixes, orientation."""

    def guess(self, rel, ingest=False):
        from audiobind.units import BookUnit, _guess

        unit = BookUnit(path="X", name=os.path.basename(rel), parent="", parts=[], rel_path=rel)
        _guess(unit, ingest=ingest)
        return unit

    def test_a_number_that_is_the_title_survives(self):
        self.assertEqual(split_author_title("13 Hours"), (None, "13 Hours"))
        self.assertEqual(split_author_title("3 Body Problem"), (None, "3 Body Problem"))
        self.assertEqual(self.guess("13 Hours").title_guess, "13 Hours")

    def test_a_sequence_number_still_strips(self):
        self.assertEqual(split_author_title("01 - Convergence"), (None, "Convergence"))
        self.assertEqual(split_author_title("3. Title"), (None, "Title"))
        self.assertEqual(split_author_title("20 Make Me"), (None, "Make Me"))
        self.assertEqual(split_author_title("04 - The Gate of the Feral Gods"), (None, "The Gate of the Feral Gods"))

    def test_a_loose_library_file_loses_its_extension(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "Nora Roberts - Island of Glass.m4b"), "wb").close()
            (unit,) = discover_units(root, [".*"])
        self.assertEqual(unit.title_guess, "Island of Glass")
        self.assertEqual(self.guess("Island of Glass.m4b").title_guess, "Island of Glass")

    def test_a_narrator_is_not_the_author(self):
        self.assertEqual(split_author_title("Killing Floor (Read by Jeff Hays)"), (None, "Killing Floor"))
        unit = self.guess("Killing Floor (Read by Jeff Hays)")
        self.assertEqual((unit.author_guess, unit.title_guess, unit.narrator_guess), ("", "Killing Floor", "Jeff Hays"))
        unit = self.guess("The Martian (Narrated by R. C. Bray)")
        self.assertEqual((unit.author_guess, unit.narrator_guess), ("", "R. C. Bray"))

    def test_a_series_prefix_is_not_the_author(self):
        unit = self.guess("Dungeon Crawler Carl - 04 - The Gate of the Feral Gods")
        self.assertEqual(unit.series_guess, "Dungeon Crawler Carl")
        self.assertEqual(unit.title_guess, "The Gate of the Feral Gods")
        self.assertEqual(unit.author_guess, "")
        # a parent folder named for the series is not an author either
        unit = self.guess("Dungeon Crawler Carl/Dungeon Crawler Carl - 04 - The Gate of the Feral Gods")
        self.assertEqual(unit.author_guess, "")

    def test_an_ambiguous_orientation_keeps_the_swapped_reading(self):
        unit = self.guess("Past Tense - Lee Child")
        self.assertEqual((unit.alt_author_guess, unit.alt_title_guess), ("Lee Child", "Past Tense"))
        unit = self.guess("Influx - Daniel Suarez")
        self.assertEqual((unit.author_guess, unit.title_guess), ("Daniel Suarez", "Influx"))
        self.assertEqual((unit.alt_author_guess, unit.alt_title_guess), ("", ""))

    def test_a_bracketed_asin_in_an_ingest_name_is_a_hint(self):
        self.assertEqual(self.guess("In Too Deep [B0CLGF5CFH]", ingest=True).asin_hint, "B0CLGF5CFH")
        self.assertEqual(self.guess("In Too Deep [B0CLGF5CFH]", ingest=True).title_guess, "In Too Deep")
        self.assertEqual(self.guess("In Too Deep [CA]", ingest=True).asin_hint, "")
        self.assertEqual(self.guess("In Too Deep [B0CLGF5CFH]").asin_hint, "")  # library names: never

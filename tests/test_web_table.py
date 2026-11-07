"""Search filtering and column sorting - the rules the page mirrors in JS."""
import pathlib
import re
import tempfile
import unittest

from audiobind import web
from audiobind.state import State, UnitState


def unit(**kw):
    kw.setdefault("path", "Author/Book")
    kw.setdefault("status", "ready")
    return UnitState(**kw)


class TestSearchFilter(unittest.TestCase):
    def test_empty_query_matches_everything(self):
        self.assertTrue(web.search_matches(unit(title="Anything"), ""))
        self.assertTrue(web.search_matches(unit(title="Anything"), "   "))

    def test_matches_title_case_insensitively(self):
        u = unit(title="The Burning Room")
        self.assertTrue(web.search_matches(u, "burning"))
        self.assertTrue(web.search_matches(u, "BURNING"))
        self.assertFalse(web.search_matches(u, "freezing"))

    def test_matches_author(self):
        u = unit(title="The Burning Room", author="Michael Connelly")
        self.assertTrue(web.search_matches(u, "connelly"))

    def test_matches_path(self):
        u = unit(path="Lee Child/61 Hours", title="")
        self.assertTrue(web.search_matches(u, "61 hours"))
        self.assertTrue(web.search_matches(u, "lee"))

    def test_all_terms_must_match_and_order_does_not_matter(self):
        u = unit(title="The Burning Room", author="Michael Connelly")
        self.assertTrue(web.search_matches(u, "connelly burning"))
        self.assertTrue(web.search_matches(u, "burning connelly"))
        self.assertFalse(web.search_matches(u, "connelly dragons"))

    def test_partial_words_match(self):
        self.assertTrue(web.search_matches(unit(title="Persepolis Rising"), "sepol"))

    def test_missing_fields_do_not_explode(self):
        self.assertFalse(web.search_matches(UnitState(path="", status="ready"), "x"))


class TestSortComparator(unittest.TestCase):
    def test_title_sorts_case_insensitively_on_title_then_path(self):
        self.assertEqual(web.sort_value(unit(title="Zebra"), "title"), "zebra")
        self.assertEqual(web.sort_value(unit(path="A/b", title=""), "title"), "a/b")

    def test_tier_ranks_best_match_first(self):
        tiers = ["NONE", "HIGH", "LOW", "MEDIUM"]
        ranked = sorted(tiers, key=lambda t: web.tier_rank(t))
        self.assertEqual(ranked, ["HIGH", "MEDIUM", "LOW", "NONE"])

    def test_unknown_tier_sorts_last(self):
        self.assertGreater(web.tier_rank("WHAT"), web.tier_rank("NONE"))
        self.assertGreater(web.tier_rank(""), web.tier_rank("NONE"))

    def test_status_rank_follows_the_chip_order(self):
        self.assertLess(web.status_rank("converted"), web.status_rank("failed"))
        self.assertGreater(web.status_rank("nonsense"), web.status_rank("interrupted"))

    def test_runtime_and_scan_sort_numerically(self):
        self.assertEqual(web.sort_value(unit(hours=12.5), "runtime"), 12.5)
        self.assertEqual(web.sort_value(unit(updated_at=99.0), "scan"), 99.0)
        self.assertEqual(web.sort_value(unit(), "runtime"), 0.0)

    def test_sort_units_ascending_and_descending(self):
        units = [unit(path="a", title="B", hours=3), unit(path="b", title="A", hours=9)]
        self.assertEqual([u.title for u in web.sort_units(units, "title", "asc")], ["A", "B"])
        self.assertEqual([u.title for u in web.sort_units(units, "title", "desc")], ["B", "A"])
        self.assertEqual([u.hours for u in web.sort_units(units, "runtime", "desc")], [9, 3])

    def test_ties_break_on_title_ascending(self):
        units = [unit(path="c", title="Mango", hours=5),
                 unit(path="a", title="Apple", hours=5),
                 unit(path="b", title="Zuch", hours=5)]
        ordered = web.sort_units(units, "runtime", "desc")
        self.assertEqual([u.title for u in ordered], ["Apple", "Mango", "Zuch"])

    def test_unknown_column_falls_back_to_last_scan(self):
        units = [unit(path="a", updated_at=1), unit(path="b", updated_at=2)]
        ordered = web.sort_units(units, "nonsense", "desc")
        self.assertEqual([u.path for u in ordered], ["b", "a"])


class TestRenderedSearchAndSort(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(path="Connelly/Burning Room", status="ready",
                                  title="The Burning Room", author="Michael Connelly",
                                  confidence="HIGH", hours=12.0))
        self.state.save(UnitState(path="Child/61 Hours", status="review",
                                  title="61 Hours", author="Lee Child",
                                  confidence="LOW", hours=14.0))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_search_box_and_sortable_headers_are_rendered(self):
        page = web.render(self.state)
        self.assertIn('id="ab-search"', page)
        self.assertIn('class="ab-search"', page)
        for column in web.SORT_COLUMNS:
            self.assertIn(f'data-column="{column}"', page)

    def test_rows_carry_search_and_sort_data(self):
        page = web.render(self.state)
        self.assertIn("Michael Connelly", page)          # in the data-search haystack
        self.assertIn('data-sort-runtime="12.0"', page)
        self.assertIn('data-sort-tier="0"', page)        # HIGH

    def test_server_side_query_narrows_the_table(self):
        page = web.render(self.state, "", "connelly")
        self.assertIn("The Burning Room", page)
        self.assertNotIn("61 Hours</td>", page)

    def test_stored_sort_preference_is_restored(self):
        self.state.set_meta("sort_column", "title")
        self.state.set_meta("sort_dir", "asc")
        self.assertEqual(web.read_sort(self.state), ("title", "asc"))
        page = web.render(self.state)
        self.assertLess(page.index("61 Hours"), page.index("The Burning Room"))
        self.assertIn('window.AB_SORT_COLUMN = "title"', page)

    def test_bad_stored_sort_falls_back(self):
        self.state.set_meta("sort_column", "evil")
        self.state.set_meta("sort_dir", "sideways")
        self.assertEqual(web.read_sort(self.state), ("scan", "desc"))

    def test_quotes_in_titles_do_not_break_attributes(self):
        self.state.save(UnitState(path='Odd/Book "Quoted"', status="ready",
                                  title='A "Quoted" Title'))
        page = web.render(self.state)
        self.assertNotIn('data-search="A "Quoted"', page)
        self.assertIn("&quot;Quoted&quot;", page)

    def test_fixed_column_widths_are_declared(self):
        page = web.render(self.state)
        self.assertIn("<colgroup>", page)
        self.assertIn('class="ab-tbl"', page)
        self.assertIn("table-layout:fixed", page)

    def test_new_classes_do_not_collide_with_existing_ones(self):
        """Every class added in 0.10.0 is ab- prefixed (the .bar collision bug)."""
        existing = {"sub", "toolbar", "working", "wline", "pct", "ptrack", "wmeta", "pill",
                    "grid", "card", "ok", "warn", "err", "dim", "urow", "drow", "dd",
                    "explain", "todo", "acts", "chips", "chip", "primary", "armed", "danger",
                    "run", "paused", "on"}
        declared = set(re.findall(r"\.([a-zA-Z][\w-]*)\s*\{", web.STYLES))
        for name in declared - existing:
            self.assertTrue(name.startswith("ab-"), f"{name} is neither pre-existing nor ab-")


if __name__ == "__main__":
    unittest.main()

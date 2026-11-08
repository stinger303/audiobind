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

    def test_book_cell_has_max_width_and_actcol_is_fixed(self):
        page = web.render(self.state)
        self.assertIn("max-width:520px", web.STYLES)
        self.assertIn('style="width:min(560px,30vw)"', page)
        self.assertIn("text-overflow:ellipsis", web.STYLES)

    def test_the_table_layout_is_fixed_so_columns_never_grow(self):
        """Auto table layout let long titles push columns past any max-width."""
        page = web.render(self.state)
        self.assertIn("table-layout:fixed", page)
        self.assertIn("white-space:nowrap", web.STYLES.split(".ab-btitle")[1][:200])
        self.assertNotIn("max-width:38%", web.STYLES)

    def test_the_sticky_header_spans_every_column(self):
        """A .ab-bookcell th once got display:flex from the cell rule and broke the bar."""
        page = web.render(self.state)
        self.assertNotIn('<th class="ab-sort ab-bookcell"', page)
        self.assertIn("td.ab-bookcell{", page)
        self.assertNotIn("\n.ab-bookcell{", page)

    def test_rail_glyphs_are_real_html_entities(self):
        page = web.render(self.state)
        self.assertNotIn("&flag;", page)
        self.assertNotIn("&boxur;", page)

    def test_the_rail_footer_labels_its_paths(self):
        page = web.render(self.state)
        self.assertIn("<b>library</b>", page)
        self.assertIn("<b>output</b>", page)

    def test_the_rail_footer_prefers_the_host_paths(self):
        saved = (web.CONFIG.host_library, web.CONFIG.host_output)
        web.CONFIG.host_library = "/mnt/user/data/media/audiobooks"
        web.CONFIG.host_output = "/mnt/user/data/media/audiobooks-fixed"
        try:
            page = web.render(self.state)
        finally:
            web.CONFIG.host_library, web.CONFIG.host_output = saved
        self.assertIn("/mnt/user/data/media/audiobooks", page)
        self.assertIn("/mnt/user/data/media/audiobooks-fixed", page)
        self.assertNotIn(">&nbsp;/library<", page)

    def test_rail_footer_paths_wrap_and_carry_title_attributes(self):
        saved = (web.CONFIG.host_library, web.CONFIG.host_output)
        web.CONFIG.host_library = "/mnt/user/data/media/audiobooks-extra-long-path"
        web.CONFIG.host_output = "/mnt/user/data/media/audiobooks-fixed-long-path"
        try:
            page = web.render(self.state)
        finally:
            web.CONFIG.host_library, web.CONFIG.host_output = saved
        self.assertIn("overflow-wrap:anywhere", web.STYLES)
        self.assertIn('title="/mnt/user/data/media/audiobooks-extra-long-path"', page)
        self.assertIn('title="/mnt/user/data/media/audiobooks-fixed-long-path"', page)

    def test_the_action_toast_exists(self):
        page = web.render(self.state)
        self.assertIn('id="ab-toast"', page)
        self.assertIn("function abToast(", page)

    def test_control_buttons_carry_hover_tooltips(self):
        page = web.render(self.state)
        self.assertIn('title="Re-check every book in the library', page)
        self.assertIn('title="Finish the book that is encoding', page)
        self.assertIn('title="Abort the current conversion immediately', page)
        self.assertIn('title="Preview mode: reports what it would do', page)
        self.assertIn('title="Re-check this book against the catalog', page)
        self.assertIn('title="Convert this book even though', page)
        self.assertIn('title="Pin this catalog entry to the book', page)
        self.assertIn('title="Permanently delete this book', page)

    def test_dry_run_sits_with_the_other_controls(self):
        page = web.render(self.state)
        controls = page.split('aria-label="Controls"')[1].split("</section>")[0]
        dry_at = controls.find(">Dry run")
        spacer_at = controls.find("ab-spacer")
        self.assertGreater(dry_at, 0)
        self.assertGreater(spacer_at, dry_at)

    def test_new_classes_do_not_collide_with_existing_ones(self):
        """Every class added in 0.10.0 is ab- prefixed (the .bar collision bug)."""
        existing = {"sub", "toolbar", "working", "wline", "pct", "ptrack", "wmeta", "pill",
                    "grid", "card", "ok", "warn", "err", "dim", "urow", "drow", "dd",
                    "explain", "todo", "acts", "chips", "chip", "primary", "armed", "danger",
                    "run", "paused", "on", "held", "active", "encoding", "interrupted",
                    "converted", "ready", "review", "failed", "dry", "btn", "n", "pdot"}
        declared = set(re.findall(r"\.([a-zA-Z][\w-]*)\s*\{", web.STYLES))
        for name in declared - existing:
            self.assertTrue(name.startswith("ab-"), f"{name} is neither pre-existing nor ab-")


if __name__ == "__main__":
    unittest.main()


class TestSortAttributes(unittest.TestCase):
    """A shadowed variable once rendered rows with a literal {} instead of the
    data-sort-* attributes - clicking any column header then sorted nothing."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self.state.save(UnitState(path="A/One", status="converted",
                                  confidence="HIGH", hours=6, files=1))
        self.state.save(UnitState(path="A/Two", status="review",
                                  confidence="LOW", hours=1, files=1,
                                  note="runtime 1.0h vs catalog 9.0h"))

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_every_row_carries_its_sort_attributes(self):
        page = web.render(self.state)
        for column in ("title", "status", "tier", "runtime", "scan"):
            self.assertIn("data-sort-%s=" % column, page)
        self.assertNotIn("{}=", page)

    def test_sort_attributes_survive_the_reason_line(self):
        """The reason block used to shadow the sort-attribute string."""
        page = web.render(self.state)
        row = [seg for seg in page.split('<tr class="urow"')
               if 'data-path="A/Two"' in seg.split(">")[0] or 'data-path="A/Two"' in seg[:120]]
        self.assertTrue(row, "review row not found")
        review_row = row[-1].split("</tr>")[0]
        self.assertIn("data-sort-status=", review_row)
        self.assertIn("data-sort-tier=", review_row)
        self.assertNotIn("{}=", review_row)


if __name__ == "__main__":
    unittest.main()

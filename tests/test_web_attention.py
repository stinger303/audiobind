"""0.21.0-dev3: the "Needs you" default view - one triage list, grouped by reason, no shelf."""

import pathlib
import re
import tempfile
import unittest
from unittest import mock

from audiobind import web
from audiobind.state import State, UnitState


def render_with(units, status=""):
    with tempfile.TemporaryDirectory() as tmp:
        state = State(pathlib.Path(tmp))
        try:
            for u in units:
                state.save(u)
            return web.render(state, status)
        finally:
            state.close()


def body(page):
    return page.split('<tbody id="ab-body">')[1].split("</tbody>")[0]


def chips(page):
    return page.split('<div class="chips">')[1].split("</div>")[0]


HELD = UnitState(
    path="Zuckoff/13 Hours",
    status="held",
    confidence="MEDIUM",
    title="13 Hours",
    hours=1.38,
    files=18,
    note="1 unreadable file(s); 2 parts combined; runtime 1.38h vs catalog 7.72h",
)
REVIEW = UnitState(path="Child/Persuader", status="review", confidence="MEDIUM", title="Persuader", hours=13.1)
DONE = UnitState(path="Child/61 Hours", status="converted", confidence="HIGH", title="61 Hours", hours=13.18)


class TestNeedsYouView(unittest.TestCase):
    def test_default_view_is_needs_you_grouped_by_reason(self):
        page = render_with([HELD, REVIEW, DONE])
        self.assertIn('<a class="chip on ab-alert" href="/?status=attention">Needs your review <span class="n">2</span></a>', page)
        self.assertIn('<a class="chip" href="/?status=all">All <span class="n">3</span></a>', page)
        tbody = body(page)
        self.assertIn('<span class="ab-group-t">Held</span><span class="ab-group-n">1</span>', tbody)
        self.assertIn('<span class="ab-group-t">Review</span><span class="ab-group-n">1</span>', tbody)
        # most severe group first
        self.assertLess(tbody.index('data-group="held"'), tbody.index('data-group="review"'))
        self.assertIn('class="ab-why"', tbody)
        self.assertIn('<span class="ab-why-l">Damaged files</span>', tbody)
        self.assertIn("1 file unreadable · 1.38 h vs 7.72 h · 82% short", tbody)
        self.assertIn('<span class="ab-why-l">Medium match</span>', tbody)
        self.assertNotIn("61 Hours", tbody)  # converted is not in Needs you
        self.assertNotIn("ab-shelf", page)
        self.assertNotIn("ab-breason", page)
        self.assertNotIn('class="ab-allclear"', page)

    def test_alias_and_explicit_attention(self):
        for status in ("attention", "needs-you"):
            page = render_with([HELD, DONE], status)
            self.assertIn('class="ab-group"', page)
            self.assertNotIn("61 Hours", body(page))

    def test_status_all_is_the_flat_table(self):
        page = render_with([HELD, REVIEW, DONE], "all")
        tbody = body(page)
        self.assertNotIn("ab-group", tbody)
        for title in ("13 Hours", "Persuader", "61 Hours"):
            self.assertIn(title, tbody)
        self.assertIn('<a class="chip on" href="/?status=all">All', page)
        self.assertIn('<a class="chip ab-alert" href="/?status=attention">Needs your review', page)

    def test_a_single_status_filter_is_flat(self):
        page = render_with([HELD, REVIEW, DONE], "held")
        self.assertNotIn("ab-group", body(page))
        self.assertIn('<a class="chip on" href="/?status=held">Held', page)

    def test_all_converted_library_shows_all_and_the_all_clear(self):
        page = render_with([DONE, UnitState(path="A/2", status="converted", title="Two", hours=1)])
        self.assertNotIn("ab-group", body(page))
        self.assertNotIn("Needs your review", chips(page))
        self.assertIn('<a class="chip on" href="/?status=all">All <span class="n">2</span></a>', page)
        self.assertRegex(page, r'<p class="ab-allclear">All clear - last scan [^<]+, nothing needs you\.</p>')
        self.assertEqual(page.count('class="ab-allclear"'), 1)

    def test_chip_order_needs_you_first_all_last(self):
        names = re.findall(r'href="/\?status=(\w+)"', chips(render_with([HELD, REVIEW, DONE])))
        self.assertEqual(names, ["attention", "review", "held", "converted", "all"])

    def test_a_long_group_pages_behind_one_show_more_button(self):
        units = [
            UnitState(path=f"A/{i:02d}", status="held", confidence="HIGH", title=f"Book {i:02d}",
                      note="runtime 12.95h vs catalog 7.50h")
            for i in range(13)
        ]
        tbody = body(render_with(units))
        self.assertEqual(tbody.count('class="ab-more"'), 1)
        self.assertIn(">Show 1 more held</button>", tbody)
        self.assertEqual(len(re.findall(r'<tr class="urow"[^>]* hidden>', tbody)), 1)
        self.assertEqual(tbody.count('<tr class="urow"'), 13)
        self.assertIn('<span class="ab-group-n">13</span>', tbody)
        self.assertIn("more than your 15 % tolerance", tbody)
        # the more row is the group's last row
        self.assertGreater(tbody.index('class="ab-more-row"'), tbody.rindex('<tr class="drow"'))

    def test_the_runtime_group_sentence_names_the_configured_tolerance(self):
        # advice.GROUP_REASONS carries the default wording; web._group_sentence swaps in the real
        # setting whatever it is. If the advice wording ever changes, this notices the swap broke.
        units = [UnitState(path="A/01", status="held", confidence="HIGH", title="Book",
                           note="runtime 12.95h vs catalog 7.50h")]
        for tolerance, shown in ((0.25, "25"), (0.35, "35"), (0.4, "40")):
            with mock.patch.object(web, "_tolerance", return_value=tolerance):
                tbody = body(render_with(units))
            self.assertIn(f"more than your {shown} % tolerance", tbody, tolerance)
            if shown != "25":
                self.assertNotIn("25 % tolerance", tbody)

    def test_a_short_group_has_no_button(self):
        self.assertNotIn("ab-more", body(render_with([HELD, REVIEW])))

    def test_converted_row_cells(self):
        page = render_with([HELD, DONE], "all")
        self.assertEqual([c[1] for c in web.COLUMNS], ["Book", "Why", "Match"])
        row = page.split('data-path="Child/61 Hours"')[1].split("</tr>")[0]
        self.assertIn('<td class="ab-whycell"></td>', row)  # converted: the WHY cell stays empty
        self.assertNotIn("ab-st", row)  # no Waiting cell (0.21.0-dev4)
        self.assertIn('title="Converted · &lt;1m"', row)  # the age is the row's tooltip

    def test_rail_needs_you_item_above_library(self):
        page = render_with([HELD, REVIEW])
        rail = page.split('<aside class="ab-rail">')[1].split("</aside>")[0]
        # the default view here is attention, so the item is also the active one
        self.assertIn('class="ab-rail-item ab-alert ab-active" href="/?status=attention" aria-current="page"', rail)
        self.assertLess(rail.index("> Library"), rail.index("Needs your review"))
        quiet = render_with([DONE])
        rail = quiet.split('<aside class="ab-rail">')[1].split("</aside>")[0]
        self.assertIn('<a class="ab-rail-item" href="/?status=attention">', rail)

    def test_show_more_is_plain_js(self):
        self.assertIn("function abShowMore(btn)", web.SCRIPT)
        fn = web.SCRIPT.split("function abShowMore(btn)")[1].split("\n}\n")[0]
        self.assertNotIn("fetch(", fn)


class TestMockupFixPass(unittest.TestCase):
    """0.21.0-dev3 fix pass: the rendered page matches the approved mockup."""

    SERIES = '{"match": {"series": "Jack Reacher", "sequence": "26"}}'

    #: meter + "Medium" + cell padding, from the real 1280 render (100 px clipped it by 2 px)
    MATCH_NEED = 102
    #: the page's vertical scrollbar; calibrated on the 0.21.0-dev3 1440 render
    SCROLLBAR = 9

    @classmethod
    def book_budget(cls, viewport: int) -> float:
        """Pixels left for Book (the one column without a width) at a viewport width, the way
        table-layout:fixed hands them out."""
        def px(width: str) -> float:
            found = re.fullmatch(r"min\((\d+)px,(\d+)vw\)", width)
            if found:
                return min(float(found.group(1)), viewport * int(found.group(2)) / 100)
            return float(width[:-2])
        widths = {k: w for k, _l, w, _c in web.COLUMNS}
        if viewport < 1360:  # the stylesheet's narrow-screen Why width
            widths["why"] = re.search(r"col\.ab-col-why\{width:([^!;}]+)", web.STYLES).group(1)
        table = viewport - 212 - 1 - 48 - cls.SCROLLBAR  # rail, its border, the wrap's padding
        fixed = px(web.COVER_WIDTH) + 48 + sum(px(w) for k, w in widths.items() if k != "title")
        return table - fixed

    def test_the_slack_goes_to_book_not_match(self):
        widths = {k: w for k, _l, w, _c in web.COLUMNS}
        self.assertEqual(set(widths), {"title", "why", "tier"})  # no Length, no Waiting
        self.assertEqual(widths["title"], "")  # Book absorbs what is left
        self.assertEqual(widths["why"], "min(300px,22vw)")
        self.assertEqual(widths["tier"], "150px")  # Match: a meter and one word
        self.assertGreaterEqual(150, self.MATCH_NEED)
        self.assertAlmostEqual(self.book_budget(1440), 622, delta=1)
        self.assertGreaterEqual(self.book_budget(1440), 380)
        self.assertAlmostEqual(self.book_budget(1280), 506, delta=1)
        # below 820 px Match's cells drop and its col gives its width back to Book
        self.assertIn("@media(max-width:820px){.ab-hidemd{display:none}.ab-tbl col.ab-col-tier{width:0!important}}",
                      web.STYLES)
        page = render_with([HELD], "all")
        cols = page.split("<colgroup>")[1].split("</colgroup>")[0]
        self.assertIn('<col style="width:50px"><col><col class="ab-col-why"', cols)
        self.assertIn('<col class="ab-col-tier" style="width:150px">', cols)

    def test_the_meter_shrinks_and_the_word_does_not(self):
        def rule(selector):
            return web.STYLES.split(selector + "{")[1].split("}")[0]

        meter = rule(".ab-mmeter")
        for part in ("flex:0 1 auto", "overflow:hidden", "min-width:0"):
            self.assertIn(part, meter)
        for selector in (".ab-match-word", ".ab-match-sep"):
            self.assertIn("flex:none", rule(selector))
            self.assertNotIn("ellipsis", rule(selector))
        self.assertIn("text-overflow:ellipsis", rule(".ab-match-lead"))  # only the series prefix
        self.assertIn("min-width:0", rule(".ab-match"))
        self.assertIn("@media (max-width:1359px){.ab-tbl col.ab-col-why{width:min(300px,20vw)!important}}", web.STYLES)
        page = render_with([HELD], "all")
        self.assertIn('<col class="ab-col-why" style="width:min(300px,22vw)">', page)

    def test_a_held_row_at_1280_shows_medium_in_full(self):
        # no browser here: the served cell holds the whole word in its non-shrinking span, and
        # Match's fixed 150 px is more than the measured need of the full meter and its word
        page = render_with([HELD])
        cell = re.search(r'<td class="ab-hidemd ab-match"[^>]*>(.*?)</td>', page).group(1)
        self.assertIn('<span class="ab-match-word ab-conf-medium">Medium</span>', cell)
        self.assertEqual(dict((k, w) for k, _l, w, _c in web.COLUMNS)["tier"], "150px")
        self.assertGreaterEqual(150, self.MATCH_NEED + 20)

    def test_a_rendered_match_cell_has_its_word_beside_the_meter(self):
        page = render_with([REVIEW, HELD])
        cells = re.findall(r'<td class="ab-hidemd ab-match"[^>]*>(.*?)</td>', page)
        self.assertEqual(len(cells), 2)
        for cell in cells:
            self.assertRegex(cell, r'^<span class="ab-match-in"><span class="ab-mmeter" aria-hidden="true">'
                                   r'(<b[^>]*></b>){3}</span><span class="ab-match-word ab-conf-medium">Medium</span></span>$')

    def test_a_held_rows_match_cell_is_the_bare_word(self):
        page = render_with(
            [UnitState(path="A/H", status="held", confidence="MEDIUM", title="H", detail=self.SERIES,
                       note="runtime 12.95h vs catalog 7.50h")]
        )
        attrs, cell = re.search(r'<td class="ab-hidemd ab-match"([^>]*)>(.*?)</td>', page).groups()
        self.assertEqual(re.sub(r"<[^>]+>", "", cell), "Medium")
        self.assertNotIn("Jack Reacher", cell)
        self.assertIn('title="Jack Reacher #26 · Medium Confidence"', attrs)
        self.assertEqual(dict((k, w) for k, _l, w, _c in web.COLUMNS)["why"], "min(300px,22vw)")

    def test_a_converted_row_keeps_the_series_position(self):
        page = render_with(
            [UnitState(path="A/C", status="converted", confidence="HIGH", title="C", detail=self.SERIES)]
        )
        attrs, cell = re.search(r'<td class="ab-hidemd ab-match"([^>]*)>(.*?)</td>', page).groups()
        self.assertEqual(re.sub(r"<[^>]+>", "", cell).replace("&middot;", " · "), "Jack Reacher #26 · High")
        self.assertIn('title="Jack Reacher #26 · High Confidence"', attrs)

    def test_chip_labels_are_capitalised_in_mockup_order(self):
        labels = re.findall(r'<a class="chip[^"]*" href="[^"]+">([^<]+?) <span class="n">', chips(render_with([HELD, REVIEW, DONE])))
        self.assertEqual(labels, ["Needs your review", "Review", "Held", "Converted", "All"])
        self.assertEqual(web.STATUS_CHIP_LABELS["interrupted"], "Stopped")
        self.assertEqual(web.STATUS_CHIP_LABELS["failed"], "Failed")

    def test_rail_has_one_triage_item_below_library(self):
        for page in (render_with([HELD, REVIEW]),):
            rail = page.split('<aside class="ab-rail">')[1].split("</aside>")[0]
            self.assertNotIn("Attention", rail)
            self.assertNotIn("ATTENTION", rail)
            for name in ("review", "held", "failed"):
                self.assertNotIn(f'href="/?status={name}"', rail)
            self.assertLess(rail.index("> Library"), rail.index("> Needs your review"))
            self.assertLess(rail.index("> Needs your review"), rail.index("> Settings"))
        for template in (web.PAGE, web.SETTINGS_PAGE, web.SERIES_PAGE, web.DISCOVER_PAGE):
            self.assertNotIn('aria-label="Attention"', template)
            self.assertNotIn("rail_review_count", template)
            self.assertLess(template.index("</span> Library"), template.index("</span> Needs your review"))

    def test_why_label_never_shrinks_and_the_numbers_ellipsize(self):
        label = web.STYLES.split(".ab-why-l{")[1].split("}")[0]
        self.assertIn("flex:none", label)
        sub = web.STYLES.split(".ab-why-sub{")[1].split("}")[0]
        self.assertIn("min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap", sub)
        self.assertEqual(dict((k, w) for k, _l, w, _c in web.COLUMNS)["why"], "min(300px,22vw)")

    def test_match_cell_shows_the_short_word(self):
        page = render_with([REVIEW], "all")
        cell = re.search(r'<td class="ab-hidemd ab-match"([^>]*)>(.*?)</td>', page).groups()
        self.assertIn('<span class="ab-match-word ab-conf-medium">Medium</span>', cell[1])
        self.assertNotIn("Medium Confidence", cell[1])
        self.assertIn("Medium Confidence", cell[0])  # the long form is the title
        panel = page.split('<tr class="drow"')[1].split("</tr>")[0]
        self.assertIn("Medium confidence", panel)

    def test_match_short_words(self):
        for conf, word in (("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low"), ("COLLECTION", "Collection")):
            html, title = web.match_cell(UnitState(path="x", status="review", confidence=conf), {}, {})
            self.assertTrue(html.endswith(f">{word}</span></span>"), html)
        html, title = web.match_cell(
            UnitState(path="x", status="review", confidence=""), {"match": {"asin_pin_failed": "B0X"}}, {}, "B0X"
        )
        self.assertIn(">Not on Audible</span>", html)

    def test_waiting_is_always_a_relative_short_age(self):
        now = 1_800_000_000.0
        cases = (
            (10, "<1m"), (4 * 60, "4m"), (3 * 3600, "3h"), (6 * 86400, "6d"), (12 * 86400, "12d"),
            (21 * 86400, "3w"), (40 * 86400, "5w"), (70 * 86400, "2mo"), (150 * 86400, "5mo"),
            (400 * 86400, "1y"),
        )
        for age, want in cases:
            with self.subTest(age=age):
                self.assertEqual(web.short_age(now - age, now), want)
                self.assertIn(f'<span class="ab-st-age">{web._html(want)}</span>',
                              web.status_dot("held", now - age, now, label=False))
        self.assertEqual(web.short_age(0, now), "")

    def test_waiting_cell_for_six_and_forty_days(self):
        import time

        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                state.save(UnitState(path="A/6", status="held", title="Six"))
                state.save(UnitState(path="A/40", status="held", title="Forty"))
                with state._lock:  # save() stamps "now"; age the rows by hand
                    state.connection.execute("UPDATE units SET updated_at = ? WHERE path = 'A/6'", (now - 6 * 86400,))
                    state.connection.execute("UPDATE units SET updated_at = ? WHERE path = 'A/40'", (now - 40 * 86400,))
                page = web.render(state, "all")
            finally:
                state.close()
        # 0.21.0-dev4: the age is the row's tooltip (and the detail panel's line), never a date
        self.assertIn('title="Held · 6d"', page)
        self.assertIn('title="Held · 5w"', page)
        self.assertIn('<div class="ab-meterhint ab-panel-age">Held · 6d</div>', page)
        self.assertNotRegex(body(page), r'title="Held · (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ')


class TestStatusAgeAndRailPath(unittest.TestCase):
    def test_the_age_is_never_the_ellipsized_part(self):
        age = web.STYLES.split(".ab-st-age{")[1].split("}")[0]
        self.assertIn("flex:none", age)
        self.assertNotIn("ellipsis", age)
        self.assertNotIn("margin-left", age)
        label = web.STYLES.split(".ab-st-label{")[1].split("}")[0]
        self.assertIn("text-overflow:ellipsis", label)
        self.assertIn("min-width:0", label)
        self.assertIn("gap:5px", web.STYLES.split(".ab-st{")[1].split("}")[0])
        self.assertIn('<span class="ab-st-label">Review</span><span class="ab-st-age">', web.status_dot("review", 1, 100))

    def test_breakable_path(self):
        self.assertEqual(
            web._breakable_path("/mnt/user/data/media/audiobooks"),
            "/&#8203;mnt/&#8203;user/&#8203;data/&#8203;media/&#8203;audiobooks",
        )
        self.assertEqual(web._breakable_path("C:\\books<x>"), "C:\\&#8203;books&lt;x&gt;")

    def test_rail_foot_breaks_at_slashes_but_titles_stay_plain(self):
        from audiobind.config import CONFIG

        saved = (CONFIG.host_library, CONFIG.host_output)
        CONFIG.host_library, CONFIG.host_output = "/mnt/user/data/media/audiobooks", "/mnt/user/out"
        try:
            page = render_with([DONE])
        finally:
            CONFIG.host_library, CONFIG.host_output = saved
        foot = page.split('<div class="ab-rail-foot">')[1].split("</div>")[0]
        self.assertIn("/&#8203;mnt/&#8203;user/&#8203;data/&#8203;media/&#8203;audiobooks", foot)
        self.assertIn('title="/mnt/user/data/media/audiobooks"', foot)
        for title in re.findall(r'title="([^"]*)"', foot):
            self.assertNotIn("&#8203;", title)


def heading(page):
    return re.search(r"<h1><a [^>]*>(.*?)</a></h1>", page).group(1)


def page_title(page):
    return re.search(r"<title>(.*?)</title>", page).group(1)


def header_cells(page):
    return re.findall(r"<th[ >].*?</th>", page.split("<thead>")[1].split("</thead>")[0], re.S)


def col_count(page):
    return len(re.findall(r"<col[ >]", page.split("<colgroup>")[1].split("</colgroup>")[0]))


class TestViewNamesItself(unittest.TestCase):
    """0.21.0-dev4: the heading names the view; the table is Book, Why, Match in every view."""

    UNITS = [HELD, REVIEW, DONE]

    def test_heading_and_title_name_the_view(self):
        cases = (
            ("", "Needs your review &middot; 2"),
            ("attention", "Needs your review &middot; 2"),
            ("all", "Library"),
            ("held", "Held"),
            ("review", "Review"),
            ("converted", "Converted"),
        )
        for status, want in cases:
            with self.subTest(status=status):
                page = render_with(self.UNITS, status)
                self.assertEqual(heading(page), want)
                self.assertEqual(page_title(page), f"Audiobind &middot; {want}")
        self.assertEqual(heading(render_with([UnitState(path="A/S", status="interrupted", title="S")], "interrupted")),
                         "Stopped")
        # nothing needs you: the default view is All, named Library
        self.assertEqual(heading(render_with([DONE])), "Library")

    def render_searched(self, query):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                for u in self.UNITS:
                    state.save(u)
                return web.render(state, "", query)
            finally:
                state.close()

    def every_view(self):
        for status in ("", "attention", "all", "held", "review", "converted"):
            yield f"status={status!r}", render_with(self.UNITS, status)
        yield "search", self.render_searched("persuader")

    def test_the_table_is_book_why_match_in_every_view(self):
        for name, page in self.every_view():
            with self.subTest(view=name):
                heads = header_cells(page)
                labels = [re.sub(r"<[^>]+>", "", h) for h in heads]
                self.assertEqual(labels, ["", "Book", "Why", "Match", ""])  # cover + actions unlabelled
                self.assertEqual(len(heads), col_count(page))
                self.assertEqual(len(heads), web.TOTAL_COLUMNS)
                table = page.split('<table class="ab-tbl">')[1].split("</table>")[0]
                self.assertNotIn("Length", table)
                self.assertNotIn("Waiting", table)
                for row in re.findall(r'<tr class="urow".*?</tr>', table, re.S):
                    self.assertEqual(len(re.findall(r"<td[ >]", row)), web.TOTAL_COLUMNS)

    def test_every_colspan_spans_the_table(self):
        self.assertEqual(web.TOTAL_COLUMNS, len(web.COLUMNS) + 2)
        long_group = [UnitState(path=f"H/{i:02d}", status="held", title=f"B{i:02d}") for i in range(13)]
        pages = [p for _n, p in self.every_view()] + [render_with(long_group), render_with([], "held")]
        for page in pages:
            spans = re.findall(r'colspan="(\d+)"', page)
            self.assertTrue(spans)
            self.assertEqual(set(spans), {str(web.TOTAL_COLUMNS)})
        long_page = render_with(long_group)
        self.assertIn(f'<tr class="ab-more-row" data-group="held"><td colspan="{web.TOTAL_COLUMNS}">', long_page)
        self.assertIn(f'<tr class="ab-group" data-group="held"><td colspan="{web.TOTAL_COLUMNS}">', long_page)

    def test_a_held_row_carries_no_runtime_or_age_cell_and_its_title_has_both(self):
        for name, page in self.every_view():
            if 'data-path="Zuckoff/13 Hours"' not in page:
                continue
            with self.subTest(view=name):
                row = page.split('<tr class="urow" onclick="toggleRow(this, event)" data-path="Zuckoff/13 Hours"')[1]
                tag, cells = row.split(">", 1)
                cells = cells.split("</tr>")[0]
                self.assertIn('title="Held · &lt;1m"', tag)
                # the Why cell keeps its runtimes ("1.38 h vs 7.72 h"); no other cell prints one
                outside_why = re.sub(r'<td class="ab-whycell">.*?</td>', "", cells, flags=re.S)
                self.assertNotRegex(outside_why, r"\d+\.\d+\s*h\b")
                self.assertNotIn("&lt;1m", cells)
                self.assertNotIn("ab-st-age", cells)

    def test_the_meter_and_the_kebab_stay(self):
        page = render_with(self.UNITS)
        row = page.split('data-path="Child/Persuader"')[1].split("</tr>")[0]
        self.assertIn('<span class="ab-mmeter" aria-hidden="true">', row)
        self.assertIn('<span class="ab-match-word ab-conf-medium">Medium</span>', row)
        self.assertIn('<td class="ab-actcol"><div class="ab-kebabwrap"', row)
        self.assertIn('class="ab-kebab"', row)
        self.assertIn('<col class="ab-actcol" style="width:48px">', page)

    def test_the_detail_panel_states_the_age(self):
        page = render_with(self.UNITS)
        panel = page.split('<tr class="drow" data-path="Zuckoff/13 Hours"')[1].split("</tr>")[0]
        self.assertIn('<div class="ab-meterhint ab-panel-age">Held · &lt;1m</div>', panel)


class TestNeedsYourReviewLabel(unittest.TestCase):
    """0.21.0-dev4: the view is labelled "Needs your review"; the internals keep their names."""

    def test_the_label_is_needs_your_review_in_rail_chip_and_heading(self):
        page = render_with([HELD, REVIEW, DONE])
        rail = page.split('<aside class="ab-rail">')[1].split("</aside>")[0]
        self.assertIn('href="/?status=attention" aria-current="page">\n      <span class="ab-glyph">&#9888;</span> Needs your review\n', rail)
        self.assertIn('<a class="chip on ab-alert" href="/?status=attention">Needs your review <span class="n">2</span></a>',
                      chips(page))
        self.assertIn("Needs your review &middot; 2</a></h1>", page)
        self.assertIn("<title>Audiobind &middot; Needs your review &middot; 2</title>", page)
        for template in (web.PAGE, web.SETTINGS_PAGE, web.SERIES_PAGE, web.DISCOVER_PAGE):
            self.assertIn("</span> Needs your review\n", template)
        # the old label never reaches the page, on any view or page
        for other in (page, render_with([HELD], "all"), render_with([DONE]), render_with([HELD], "needs-you")):
            self.assertNotIn("Needs you ", other)
        for template in (web.PAGE, web.SETTINGS_PAGE, web.SERIES_PAGE, web.DISCOVER_PAGE):
            self.assertNotIn("Needs you\n", template)

    def test_internals_keep_their_names(self):
        page = render_with([HELD])
        self.assertIn('href="/?status=attention"', page)
        self.assertIn('class="ab-group-t">Held</span>', page)
        self.assertEqual(web.ATTENTION_ALIASES, ("attention", "needs-you"))

    def test_the_book_cell_uses_the_room_it_has(self):
        rule = web.STYLES.split("td.ab-bookcell{")[1].split("}")[0]
        self.assertIn("max-width:min(720px,100%)", rule)
        self.assertNotIn("max-width:520px", rule)


if __name__ == "__main__":
    unittest.main()

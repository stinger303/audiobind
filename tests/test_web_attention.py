"""0.21.0-dev3: the "Needs you" default view - one triage list, grouped by reason, no shelf."""

import pathlib
import re
import tempfile
import unittest

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
        self.assertIn('<a class="chip on ab-alert" href="/?status=attention">Needs you <span class="n">2</span></a>', page)
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
        self.assertIn('<a class="chip ab-alert" href="/?status=attention">Needs you', page)

    def test_a_single_status_filter_is_flat(self):
        page = render_with([HELD, REVIEW, DONE], "held")
        self.assertNotIn("ab-group", body(page))
        self.assertIn('<a class="chip on" href="/?status=held">Held', page)

    def test_all_converted_library_shows_all_and_the_all_clear(self):
        page = render_with([DONE, UnitState(path="A/2", status="converted", title="Two", hours=1)])
        self.assertNotIn("ab-group", body(page))
        self.assertNotIn("Needs you", chips(page))
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
        self.assertIn("more than your 25 % tolerance", tbody)
        # the more row is the group's last row
        self.assertGreater(tbody.index('class="ab-more-row"'), tbody.rindex('<tr class="drow"'))

    def test_a_short_group_has_no_button(self):
        self.assertNotIn("ab-more", body(render_with([HELD, REVIEW])))

    def test_columns_and_waiting_cell(self):
        page = render_with([HELD, DONE], "all")
        self.assertEqual([c[1] for c in web.COLUMNS], ["Book", "Why", "Match", "Length", "Waiting"])
        row = page.split('data-path="Child/61 Hours"')[1].split("</tr>")[0]
        self.assertIn('<td class="ab-whycell"></td>', row)  # converted: the WHY cell stays empty
        self.assertNotIn('class="ab-st-label"', row)  # Waiting: the age only, no word
        self.assertIn('<span class="ab-st-age">&lt;1m</span>', row)
        self.assertIn('title="Converted · just now"', row)

    def test_rail_needs_you_item_above_library(self):
        page = render_with([HELD, REVIEW])
        rail = page.split('<aside class="ab-rail">')[1].split("</aside>")[0]
        self.assertIn('class="ab-rail-item ab-alert" href="/?status=attention"', rail)
        self.assertLess(rail.index("> Library"), rail.index("Needs you"))
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
    #: the page's vertical scrollbar; calibrated so 1440 gives the measured Match of 197 px
    SCROLLBAR = 9

    @classmethod
    def match_budget(cls, viewport: int) -> float:
        """Pixels left for Match at a viewport width, the way table-layout:fixed hands them out."""
        def px(width: str) -> float:
            found = re.fullmatch(r"min\((\d+)px,(\d+)vw\)", width)
            if found:
                return min(float(found.group(1)), viewport * int(found.group(2)) / 100)
            return float(width[:-2])
        widths = {k: w for k, _l, w, _c in web.COLUMNS}
        if viewport < 1360:  # the stylesheet's narrow-screen Why width
            widths["why"] = re.search(r"col\.ab-col-why\{width:([^!;}]+)", web.STYLES).group(1)
        table = viewport - 212 - 1 - 48 - cls.SCROLLBAR  # rail, its border, the wrap's padding
        fixed = px(web.COVER_WIDTH) + 48 + sum(px(w) for k, w in widths.items() if k != "tier")
        return table - fixed

    def test_column_budget_leaves_match_room_for_its_word(self):
        widths = {k: w for k, _l, w, _c in web.COLUMNS}
        self.assertEqual(widths["scan"], "88px")
        self.assertEqual(widths["title"], "min(520px,28vw)")
        self.assertEqual(widths["why"], "min(300px,22vw)")
        self.assertEqual(widths["tier"], "")  # Match absorbs what is left
        self.assertAlmostEqual(self.match_budget(1440), 197, delta=1)  # the measured 1440 render
        for viewport in (1280, 1440):
            with self.subTest(viewport=viewport):
                self.assertGreaterEqual(self.match_budget(viewport), self.MATCH_NEED)

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
        # no browser here: the served cell holds the whole word in its non-shrinking span, and the
        # 1280 column budget (Why at 20vw) leaves Match more than the measured need of the full meter
        page = render_with([HELD])
        cell = re.search(r'<td class="ab-hidemd ab-match"[^>]*>(.*?)</td>', page).group(1)
        self.assertIn('<span class="ab-match-word ab-conf-medium">Medium</span>', cell)
        self.assertGreaterEqual(self.match_budget(1280), self.MATCH_NEED + 20)

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
        self.assertEqual(labels, ["Needs you", "Review", "Held", "Converted", "All"])
        self.assertEqual(web.STATUS_CHIP_LABELS["interrupted"], "Stopped")
        self.assertEqual(web.STATUS_CHIP_LABELS["failed"], "Failed")

    def test_rail_has_one_triage_item_below_library(self):
        for page in (render_with([HELD, REVIEW]),):
            rail = page.split('<aside class="ab-rail">')[1].split("</aside>")[0]
            self.assertNotIn("Attention", rail)
            self.assertNotIn("ATTENTION", rail)
            for name in ("review", "held", "failed"):
                self.assertNotIn(f'href="/?status={name}"', rail)
            self.assertLess(rail.index("> Library"), rail.index("> Needs you"))
            self.assertLess(rail.index("> Needs you"), rail.index("> Settings"))
        for template in (web.PAGE, web.SETTINGS_PAGE, web.SERIES_PAGE, web.DISCOVER_PAGE):
            self.assertNotIn('aria-label="Attention"', template)
            self.assertNotIn("rail_review_count", template)
            self.assertLess(template.index("</span> Library"), template.index("</span> Needs you"))

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
        self.assertIn('<span class="ab-st-age">6d</span>', page)
        self.assertIn('<span class="ab-st-age">5w</span>', page)
        self.assertNotRegex(body(page), r'ab-st-age">(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ')


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


if __name__ == "__main__":
    unittest.main()

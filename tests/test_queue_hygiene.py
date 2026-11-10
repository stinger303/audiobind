"""0.15.0-dev3 queue hygiene: auto-lock, status dots, kebab menus, table semantics, vocabulary."""

import json
import os
import pathlib
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from audiobind import matching, pipeline, web
from audiobind.catalog import Product
from audiobind.control import CONTROL
from audiobind.state import LOCKABLE_FIELDS, State, UnitState
from audiobind.units import BookUnit, Part

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase

CATALOG_DETAIL = json.dumps(
    {"match": {"asin": "B0CAT00001", "title": "Catalog Title", "authors": ["Catalog Author"]}}
)


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name)
        self.state = State(self.path)
        self.state.save(
            UnitState(
                path="A/B", status="review", title="Catalog Title", author="Catalog Author", detail=CATALOG_DETAIL
            )
        )

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def edit(self, **fields):
        """What the metadata editor's Save does: store the correction, lock what was typed."""
        self.state.set_overrides("A/B", fields)
        self.state.lock_fields("A/B", set(fields))


# --------------------------------------------------------------------------- auto-lock
class TestAutoLockPersistence(StateCase):
    def test_lock_is_stored_and_survives_a_restart(self):
        self.edit(title="Mine")
        self.assertEqual(self.state.locked_fields("A/B"), {"title"})
        self.state.close()
        self.state = State(self.path)  # a container restart reopens the same file
        self.assertEqual(self.state.locked_fields("A/B"), {"title"})
        self.assertEqual(self.state.get("A/B").title, "Mine")

    def test_locking_shows_the_human_title_and_author_at_once(self):
        self.edit(title="Mine", authors=["Me Myself"])
        row = self.state.get("A/B")
        self.assertEqual((row.title, row.author), ("Mine", "Me Myself"))

    def test_rescan_does_not_overwrite_a_locked_field(self):
        self.edit(title="Mine", authors=["Me Myself"], narrator="Reader")
        # a rescan / re-check / re-rip records whatever the catalog says now
        self.state.save(UnitState(path="A/B", status="converted", title="Catalog Again", author="Someone Else"))
        row = self.state.get("A/B")
        self.assertEqual(row.title, "Mine")
        self.assertEqual(row.author, "Me Myself")
        self.assertEqual(row.status, "converted")  # everything not locked still updates
        self.assertEqual(self.state.get_overrides("A/B"), {"title": "Mine", "authors": ["Me Myself"], "narrator": "Reader"})
        self.assertEqual(self.state.locked_fields("A/B"), {"title", "authors", "narrator"})

    def test_an_unlocked_field_still_follows_the_catalog(self):
        self.edit(title="Mine")
        self.state.save(UnitState(path="A/B", status="review", title="Catalog Again", author="New Author"))
        self.assertEqual(self.state.get("A/B").author, "New Author")

    def test_pipeline_record_cannot_overwrite_a_locked_title_even_from_a_stale_audit(self):
        """Defense in depth: the guard lives in State.save, not only in record()."""
        self.edit(title="Mine")
        u = BookUnit(path="/library/A/B", name="B", parent="/library/A", parts=[Part("B", [])], rel_path="A/B")
        audit = pipeline.Audit(unit=u)
        audit.match = matching.Match(product=Product(asin="B0CAT00002", title="Catalog Again", authors=["X"]))
        audit.confidence = "HIGH"
        audit.overrides = {}  # read before the edit landed
        pipeline.record(self.state, audit, "review")
        self.assertEqual(self.state.get("A/B").title, "Mine")
        self.assertEqual(self.state.get("A/B").asin, "B0CAT00002")

    def test_asin_lock_survives_a_rescan(self):
        self.state.set_asin_override("A/B", "B0MINE0001")
        self.state.lock_fields("A/B", {"asin"})
        self.state.save(UnitState(path="A/B", status="review", asin="B0OTHER001"))
        self.assertEqual(self.state.get_asin_override("A/B"), "B0MINE0001")
        self.assertIn("asin", self.state.locked_fields("A/B"))

    def test_unknown_fields_are_never_locked(self):
        self.state.lock_fields("A/B", {"title", "path", "status"})
        self.assertEqual(self.state.locked_fields("A/B"), {"title"})
        self.assertIn("asin", LOCKABLE_FIELDS)

    def test_delete_forgets_the_locks(self):
        self.edit(title="Mine")
        self.state.delete("A/B")
        self.assertEqual(self.state.locked_fields("A/B"), set())


class TestUnlock(StateCase):
    def test_one_unlock_hands_the_field_back_to_the_catalog(self):
        self.edit(title="Mine", narrator="Reader")
        self.assertTrue(self.state.unlock_field("A/B", "title"))
        self.assertEqual(self.state.locked_fields("A/B"), {"narrator"})
        self.assertEqual(self.state.get_overrides("A/B"), {"narrator": "Reader"})
        self.assertEqual(self.state.get("A/B").title, "Catalog Title")  # last catalog match
        # and the next rescan may now write the catalog's value
        self.state.save(UnitState(path="A/B", status="review", title="Catalog Again"))
        self.assertEqual(self.state.get("A/B").title, "Catalog Again")

    def test_unlocking_authors_restores_the_catalog_author(self):
        self.edit(authors=["Me Myself"])
        self.state.unlock_field("A/B", "authors")
        self.assertEqual(self.state.get("A/B").author, "Catalog Author")
        self.assertEqual(self.state.get_overrides("A/B"), {})

    def test_unlocking_the_asin_clears_the_hand_set_asin(self):
        self.state.set_asin_override("A/B", "B0MINE0001")
        self.state.lock_fields("A/B", {"asin"})
        self.assertTrue(self.state.unlock_field("A/B", "asin"))
        self.assertEqual(self.state.get_asin_override("A/B"), "")

    def test_unlocking_what_is_not_locked_is_a_no(self):
        self.assertFalse(self.state.unlock_field("A/B", "title"))
        self.assertFalse(self.state.unlock_field("A/B", "nonsense"))

    def test_unlock_survives_a_restart(self):
        self.edit(title="Mine")
        self.state.unlock_field("A/B", "title")
        self.state.close()
        self.state = State(self.path)
        self.assertEqual(self.state.locked_fields("A/B"), set())


class TestLockMigration(unittest.TestCase):
    def test_corrections_typed_before_locks_existed_start_out_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            state.save(UnitState(path="A/B", status="review"))
            state.set_overrides("A/B", {"title": "Mine", "series": "S"})
            state.set_asin_override("A/B", "B0MINE0001")
            # pretend this database predates field locks
            with state._lock:
                state.connection.execute("DELETE FROM field_locks")
                state.connection.execute("DELETE FROM meta WHERE key = 'field_locks_v1'")
            state.close()
            state = State(pathlib.Path(tmp))
            try:
                self.assertEqual(state.locked_fields("A/B"), {"title", "series", "asin"})
                # one-time: an unlock afterwards is not undone by the next start
                state.unlock_field("A/B", "series")
                state.close()
                state = State(pathlib.Path(tmp))
                self.assertEqual(state.locked_fields("A/B"), {"title", "asin"})
            finally:
                state.close()


# --------------------------------------------------------------------------- endpoints
class TestLockEndpoints(LiveServerCase):
    def setUp(self):
        super().setUp()
        CONTROL._queue.clear()
        self.state.save(UnitState(path="A/B", status="review", title="Catalog Title", detail=CATALOG_DETAIL))

    def tearDown(self):
        CONTROL._queue.clear()
        CONTROL.end_unit()
        super().tearDown()

    def test_saving_metadata_locks_the_edited_fields(self):
        status, body = self.post({"action": "set_meta", "path": "A/B", "fields": {"title": "Mine", "narrator": ""}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["locked"], ["title"])
        self.assertEqual(self.state.locked_fields("A/B"), {"title"})

    def test_emptying_a_box_unlocks_that_field(self):
        self.post({"action": "set_meta", "path": "A/B", "fields": {"title": "Mine", "narrator": "R"}})
        status, body = self.post({"action": "set_meta", "path": "A/B", "fields": {"title": "Mine", "narrator": ""}})
        self.assertEqual(body["locked"], ["title"])

    def test_unlock_endpoint(self):
        self.post({"action": "set_meta", "path": "A/B", "fields": {"title": "Mine"}})
        status, body = self.post({"action": "unlock", "path": "A/B", "field": "title"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["locked"], [])
        self.assertEqual(self.state.get("A/B").title, "Catalog Title")
        self.assertEqual(self.post({"action": "unlock", "path": "A/B", "field": "title"})[0], 409)
        self.assertEqual(self.post({"action": "unlock", "path": "A/B", "field": "status"})[0], 400)
        self.assertEqual(self.post({"action": "unlock", "path": "no/such", "field": "title"})[0], 404)

    def test_setting_an_asin_locks_it(self):
        os.makedirs(self.lib / "A" / "B")
        (self.lib / "A" / "B" / "01.mp3").write_bytes(b"audio")
        done = threading.Event()
        with mock.patch.object(web, "process_unit", side_effect=lambda **kw: done.set()):
            status, body = self.post({"action": "set_asin", "path": "A/B", "asin": "B0MINE0001"})
            self.assertEqual(status, 200, body)
            self.assertTrue(done.wait(5))
        self.assertIn("asin", self.state.locked_fields("A/B"))


class TestRecheckEndpoint(LiveServerCase):
    def setUp(self):
        super().setUp()
        CONTROL._queue.clear()
        CONTROL.end_unit()
        os.makedirs(self.lib / "A" / "B")
        (self.lib / "A" / "B" / "01.mp3").write_bytes(b"audio")
        self.state.save(UnitState(path="A/B", status="converted", title="Book"))

    def tearDown(self):
        CONTROL._queue.clear()
        CONTROL.end_unit()
        super().tearDown()

    def wait_dequeued(self):
        for _ in range(100):
            if not CONTROL.queue_snapshot()["count"]:
                return
            threading.Event().wait(0.05)

    def test_recheck_runs_the_manual_re_audit_for_one_book(self):
        calls = []
        done = threading.Event()

        def fake(**kw):
            calls.append(kw)
            done.set()

        with mock.patch.object(web, "process_unit", side_effect=fake):
            status, body = self.post({"action": "recheck", "path": "A/B"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["action"], "recheck")
            self.assertTrue(done.wait(5))
        self.wait_dequeued()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["unit"].rel_path, "A/B")
        self.assertFalse(calls[0]["force"])  # auto-convert rules still apply
        self.assertTrue(calls[0]["manual"])
        self.assertEqual(CONTROL.queue_snapshot()["count"], 0)

    def test_recheck_honours_the_asin_you_set(self):
        """Pins are never silently fallen back from: the audit is asked for that exact ASIN."""
        self.state.set_asin_override("A/B", "B0MINE0001")
        seen = []
        done = threading.Event()

        def fake_audit(unit, asin_override=""):
            seen.append(asin_override)
            done.set()
            raise RuntimeError("stop after the audit call")

        with (
            mock.patch.object(pipeline, "audit_unit", side_effect=fake_audit),
            mock.patch.object(web.logging.getLogger("audiobind"), "exception"),
        ):
            status, _body = self.post({"action": "recheck", "path": "A/B"})
            self.assertEqual(status, 200)
            self.assertTrue(done.wait(5))
            self.wait_dequeued()
        self.assertEqual(seen, ["B0MINE0001"])

    def test_recheck_refuses_a_book_that_is_encoding_or_queued(self):
        CONTROL.current_unit = "A/B"
        self.assertEqual(self.post({"action": "recheck", "path": "A/B"})[0], 409)
        CONTROL.end_unit()
        CONTROL.enqueue("A/B")
        self.assertEqual(self.post({"action": "recheck", "path": "A/B"})[0], 409)

    def test_recheck_of_an_unknown_book(self):
        self.assertEqual(self.post({"action": "recheck", "path": "No/Such"})[0], 404)


# --------------------------------------------------------------------------- semantics kit
NOW = 1_800_000_000.0  # a fixed "now" so the tests never race the clock


class TestRelativeAge(unittest.TestCase):
    def test_under_the_seven_day_cutoff_ages_are_relative(self):
        self.assertEqual(web.rel_age(NOW - 20, NOW), "just now")
        self.assertEqual(web.rel_age(NOW - 5 * 60, NOW), "5m ago")
        self.assertEqual(web.rel_age(NOW - 5 * 3600 - 59, NOW), "5h ago")
        self.assertEqual(web.rel_age(NOW - 2 * 86400, NOW), "2d ago")
        self.assertEqual(web.rel_age(NOW - 7 * 86400, NOW), "7d ago")  # the cutoff day itself

    def test_waiting_states_read_waiting(self):
        self.assertEqual(web.rel_age(NOW - 3 * 3600, NOW, waiting=True), "waiting 3h")
        self.assertEqual(web.rel_age(NOW - 10, NOW, waiting=True), "waiting <1m")

    def test_over_the_cutoff_shows_the_absolute_date(self):
        then = NOW - 8 * 86400
        local = time.localtime(then)
        expected = f"{web._MONTHS[local.tm_mon - 1]} {local.tm_mday}"
        self.assertEqual(web.rel_age(then, NOW), expected)
        self.assertEqual(web.rel_age(then, NOW, waiting=True), "waiting since " + expected)
        self.assertNotIn("ago", web.rel_age(then, NOW))

    def test_another_year_carries_the_year(self):
        then = NOW - 400 * 86400
        self.assertRegex(web.rel_age(then, NOW), r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")

    def test_unknown_time_is_empty_for_the_caller_to_dash(self):
        self.assertEqual(web.rel_age(0, NOW), "")
        self.assertEqual(web.rel_age(None, NOW), "")


class TestEmDash(unittest.TestCase):
    def test_unknown_numbers_render_the_dash(self):
        self.assertEqual(web.fmt_hours(0), web.DASH)
        self.assertEqual(web.fmt_hours(None), web.DASH)
        self.assertEqual(web.fmt_mb(0), web.DASH)
        self.assertIn("&mdash;", web.DASH)

    def test_known_numbers_keep_their_unit_and_grouping(self):
        self.assertEqual(web.fmt_hours(13.18), "13.18 h")
        self.assertEqual(web.fmt_mb(1856), "1,856 MB")

    def test_no_match_is_the_dash(self):
        html, title = web.match_cell(UnitState(path="x", status="review", confidence="NONE"), {}, {})
        self.assertEqual((html, title), (web.DASH, ""))
        html, _ = web.match_cell(UnitState(path="x", status="review", confidence=""), {}, {})
        self.assertEqual(html, web.DASH)

    def test_a_row_with_nothing_known_says_so_with_dashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            try:
                state.save(UnitState(path="Mystery/Folder", status="review"))
                with state._lock:
                    state.connection.execute("UPDATE units SET updated_at = 0")
                page = web.render(state)
            finally:
                state.close()
        row = page.split('<tr class="urow"')[1].split("</tr>")[0]
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        # author line, match, length and last scan are all unknown
        self.assertIn('<span class="ab-bauthor"><span class="ab-dash">&mdash;</span></span>', row)
        self.assertGreaterEqual(row.count("&mdash;"), 4, cells)
        for junk in ("N/A", "null", "None", "undefined", ">-<"):
            self.assertNotIn(junk, row)


def render_with(units, pins=None, **extra):
    with tempfile.TemporaryDirectory() as tmp:
        state = State(pathlib.Path(tmp))
        try:
            for u in units:
                state.save(u)
            for path, asin in (pins or {}).items():
                state.set_asin_override(path, asin)
            for path, fields in extra.get("locks", {}).items():
                state.set_overrides(path, {f: "x" for f in fields})
                state.lock_fields(path, fields)
            return web.render(state)
        finally:
            state.close()


def table(page):
    return page.split('<tbody id="ab-body">')[1].split("</tbody>")[0]


def rows(page):
    return [seg.split("</tr>")[0] for seg in table(page).split('<tr class="urow"')[1:]]


class TestStatusDot(unittest.TestCase):
    def test_one_labeled_dot_per_row_replaces_the_pill(self):
        page = render_with(
            [
                UnitState(path="A/1", status="converted", title="61 Hours", hours=13.18),
                UnitState(path="A/2", status="held", title="World War Z", hours=12.21),
                UnitState(path="A/3", status="review", title="The Power of Habit", hours=10.49),
            ]
        )
        self.assertNotIn('class="pill', table(page))
        for row in rows(page):
            self.assertEqual(row.count('class="ab-st-dot"'), 1)
        body = table(page)
        self.assertIn('ab-st ab-st-ok"', body)
        self.assertIn('aria-hidden="true"></span>Converted<span class="ab-st-age">just now</span>', body)
        self.assertIn("></span>Held<", body)
        self.assertIn('<span class="ab-st-age">waiting &lt;1m</span>', web.status_dot("review", NOW - 10, NOW))

    def test_only_a_moving_state_pulses(self):
        self.assertIn("ab-st-moving", web.status_dot("review", NOW, NOW))
        for settled in ("converted", "held", "failed", "ready", "interrupted"):
            self.assertNotIn("ab-st-moving", web.status_dot(settled, NOW, NOW), settled)
        self.assertIn("ab-st-moving .ab-st-dot{animation", web.STYLES)

    def test_the_word_always_sits_next_to_the_dot(self):
        for status, word in (("converted", "Converted"), ("held", "Held"), ("review", "Review"), ("failed", "Failed")):
            html = web.status_dot(status, NOW - 3600, NOW)
            self.assertIn(f'</span>{word}<span class="ab-st-age">', html)

    def test_status_age_uses_the_cutoff(self):
        self.assertIn(">2d ago<", web.status_dot("converted", NOW - 2 * 86400, NOW))
        self.assertIn(">waiting 3h<", web.status_dot("review", NOW - 3 * 3600, NOW))
        self.assertNotIn("ago", web.status_dot("converted", NOW - 30 * 86400, NOW))


class TestTableSemantics(unittest.TestCase):
    def test_numeric_cells_use_tabular_numerals_and_never_wrap(self):
        page = render_with([UnitState(path="A/1", status="converted", title="61 Hours", hours=13.18)])
        self.assertIn('<td class="ab-num">13.18 h</td>', page)
        num_rule = web.STYLES.split(".ab-num{")[1].split("}")[0]
        self.assertIn("font-variant-numeric:tabular-nums", num_rule)
        self.assertIn("white-space:nowrap", num_rule)

    def test_match_values_never_wrap(self):
        rule = web.STYLES.split(".ab-match{")[1].split("}")[0]
        self.assertIn("white-space:nowrap", rule)
        self.assertIn("text-overflow:ellipsis", rule)

    def test_sortable_headers_are_real_buttons_with_the_arrow_on_the_active_sort(self):
        page = render_with([UnitState(path="A/1", status="converted", title="T")])
        heads = re.findall(r'<th class="ab-sort[^"]*" data-column="(\w+)" aria-sort="(\w+)">(.*?)</th>', page)
        self.assertEqual([h[0] for h in heads], list(web.SORT_COLUMNS))
        for column, aria, inner in heads:
            self.assertTrue(inner.startswith('<button type="button" class="ab-sortbtn">'), inner)
            if column == "scan":  # the default sort
                self.assertEqual(aria, "descending")
                self.assertIn('<span class="ab-arrow">&#9660;</span>', inner)
            else:
                self.assertEqual(aria, "none")
                self.assertIn('<span class="ab-arrow"></span>', inner)

    def test_no_new_columns(self):
        self.assertEqual(web.SORT_COLUMNS, ("title", "status", "tier", "runtime", "scan"))
        self.assertEqual([c[1] for c in web.COLUMNS], ["Book", "Status", "Match", "Length", "Last scan"])


class TestMatchWording(unittest.TestCase):
    def test_a_hand_set_asin_reads_current_asin_then_confidence(self):
        page = render_with(
            [UnitState(path="A/3", status="review", title="The Power of Habit", confidence="LOW", asin="B0FD4CV4BF")],
            pins={"A/3": "B0FD4CV4BF"},
        )
        self.assertIn(
            '<span class="ab-match-cur">Current ASIN</span> &middot; <span class="ab-conf-low">Low Confidence</span>',
            page,
        )

    def test_an_automatic_match_reads_series_then_confidence(self):
        detail = json.dumps({"match": {"asin": "B003G8RX9O", "series": "Jack Reacher", "sequence": "14"}})
        page = render_with(
            [UnitState(path="A/1", status="converted", title="61 Hours", confidence="HIGH", detail=detail)]
        )
        self.assertIn('Jack Reacher #14 &middot; <span class="ab-conf-high">High Confidence</span>', page)
        self.assertIn('title="Jack Reacher #14 · High Confidence"', page)

    def test_your_series_wins_over_the_catalogs(self):
        detail = {"match": {"series": "Catalog Series", "sequence": "2"}}
        self.assertEqual(web.series_label(detail, {"series": "Mine", "series_index": "3"}), "Mine #3")
        self.assertEqual(web.series_label(detail, {}), "Catalog Series #2")

    def test_confidence_is_always_spelled_out_with_a_capital_c(self):
        page = render_with(
            [
                UnitState(path="A/1", status="converted", confidence="HIGH"),
                UnitState(path="A/2", status="review", confidence="MEDIUM"),
                UnitState(path="A/3", status="review", confidence="LOW"),
            ]
        )
        body = table(page)
        for word in ("High Confidence", "Medium Confidence", "Low Confidence"):
            self.assertIn(word, body)
        cells = re.findall(r'<td class="ab-hidemd ab-match"[^>]*>(.*?)</td>', body)
        self.assertEqual(len(cells), 3)
        for cell in cells:
            self.assertNotRegex(cell, r"\b(LOW|MEDIUM|HIGH)\b")
            self.assertNotIn("confidence", cell)  # never a lower-case c


# --------------------------------------------------------------------------- kebab menu
class TestKebabMenu(unittest.TestCase):
    def setUp(self):
        self.page = render_with(
            [
                UnitState(path="A/1", status="converted", title="61 Hours"),
                UnitState(path="A/2", status="review", title='A "Quoted" Book'),
            ]
        )

    def test_every_row_has_a_visible_kebab_with_the_four_actions(self):
        for row in rows(self.page):
            self.assertEqual(row.count('class="ab-kebab"'), 1)
            self.assertIn('aria-haspopup="menu" aria-expanded="false"', row)
            menu = row.split('<div class="ab-menu" role="menu" hidden>')[1].split("</div>")[0]
            labels = re.findall(r'role="menuitem"[^>]*>([^<]+)</button>', menu)
            self.assertEqual(labels, ["Open details", "Edit ASIN", "Re-check against Audible", "Delete&hellip;"])
            self.assertIn("&#8942;</button>", row)

    def test_the_kebab_carries_what_delete_needs_safely_escaped(self):
        self.assertIn('data-path="A/2" data-title="A &quot;Quoted&quot; Book"', self.page)
        self.assertIn('aria-label="Actions for A &quot;Quoted&quot; Book"', self.page)

    def test_menu_clicks_do_not_toggle_the_row(self):
        self.assertIn('<div class="ab-kebabwrap" onclick="event.stopPropagation()">', self.page)

    def test_menu_script_closes_on_outside_click_and_escape_and_is_keyboard_driven(self):
        script = web.SCRIPT
        for name in ("function abMenuToggle(", "function abMenuClose(", "function abMenuPick(", "function abMenuOpen("):
            self.assertIn(name, script)
        menu_js = script.split("function abMenuOpen(")[1]
        self.assertIn("document.addEventListener('click'", menu_js)
        self.assertIn("e.key === 'Escape'", menu_js)
        self.assertIn("e.key === 'ArrowDown'", menu_js)
        self.assertIn("e.key === 'ArrowUp'", menu_js)
        self.assertIn("abOpenDeleteModal(kebab.dataset.path", menu_js)  # Delete... keeps the modal
        self.assertIn("abEditAsin(row)", menu_js)  # Edit ASIN keeps its behavior
        self.assertIn("abUnitAction(item, 'recheck')", menu_js)

    def test_an_open_menu_holds_off_the_auto_reload(self):
        busy = web.SCRIPT.split("function abBusy(")[1].split("\n}")[0]
        self.assertIn("abMenuOpen()", busy)


# --------------------------------------------------------------------------- vocabulary
def visible_text(page):
    page = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    page = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
    attrs = " ".join(re.findall(r'(?:title|aria-label|placeholder)="([^"]*)"', page))
    return re.sub(r"<[^>]+>", " ", page) + " " + attrs


class TestVocabulary(unittest.TestCase):
    def test_toasts_say_setting_asin(self):
        self.assertIn("note = 'setting ASIN ' + body.asin + ' - re-checking…';", web.SCRIPT)
        self.assertIn("abToast('setting ASIN ' + asin + ' - re-checking\\u2026');", web.SCRIPT)
        self.assertNotIn("pinning", web.SCRIPT)

    def test_no_user_facing_pin_words_on_the_library_page(self):
        evidence = json.dumps(
            {
                "match": {
                    "asin": "B0FD4CV4BF", "title": "T", "title_sim": 1.0, "author_sim": 1.0, "score": 1.0,
                    "pinned": True, "asin_pin_failed": "", "runtime_delta_pct": 0, "hours": 1.0,
                },
                "source": {"hours": 1.0},
            }
        )
        alternates = json.dumps([{"asin": "B0OTHER001", "title": "Other", "authors": ["A"]}])
        page = render_with(
            [
                UnitState(path="A/1", status="review", confidence="HIGH", asin="B0FD4CV4BF", detail=evidence,
                          alternates=alternates, title="T"),
                UnitState(path="A/2", status="review", confidence="COLLECTION", title="Box"),
                UnitState(path="A/3", status="review", confidence="NONE", title="Dead",
                          detail=json.dumps({"match": {"asin_pin_failed": "B0DEAD0001"}})),
            ],
            pins={"A/1": "B0FD4CV4BF", "A/3": "B0DEAD0001"},
        )
        text = visible_text(page)
        self.assertNotRegex(text, r"(?i)\bpin(s|ned|ning)?\b")
        self.assertIn("The ASIN you set (B0DEAD0001) is not in Audible's catalog", text)

    def test_locked_is_reserved_for_metadata_locks(self):
        """Every lock word on the page is about a field you locked - nothing else borrows it."""
        page = render_with([UnitState(path="A/1", status="review", title="T")], locks={"A/1": {"title"}})
        badge = 'title="Some of this book\'s metadata is locked by you">locked</span>'
        self.assertEqual(page.count(badge), 1)  # the row's lock badge, shown
        text = re.sub(r"\s+", " ", visible_text(page.replace(badge, "")))
        allowed = (
            "\U0001f512 Locked by you",
            "\U0001f513 Unlocked",
            "Click to unlock and hand it back to the catalog",
            "Click to unlock: the next re-check matches this book on its own",
            "Edit and save it to lock it",
            "Use this ASIN to lock one exact entry",
            "Editing a field locks it automatically",
            "One click unlocks",
            "The lock lives with the book",
            "Some of this book's metadata is locked by you",
            "Setting it locks the match",
            "for the book (locks it)",
            "Want it locked in?",
        )
        for phrase in allowed:
            text = text.replace(phrase, "")
        self.assertNotRegex(text, r"(?i)\b(un)?lock(ed|s|ing)?\b")
        self.assertIn("\U0001f512 Locked by you", page)
        self.assertIn("Editing a field locks it automatically", page)


if __name__ == "__main__":
    unittest.main()

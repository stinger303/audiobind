"""0.15.0-dev2: the series shelf - grouping, gap detection, "I have this", degraded state.

Nothing here touches the network: the catalog is faked at ``catalog.series_products`` or
``series.FETCH``, and the background refresher is stubbed wherever a page is rendered.
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from audiobind import catalog, covers, series, web
from audiobind.config import CONFIG
from audiobind.state import State, UnitState

try:
    from live import LiveServerCase
except ImportError:
    from tests.live import LiveServerCase


def book(path, title, author="Lee Child", series_name="Jack Reacher", seq="", asin="", status="converted",
         output="", overrides=None):
    detail = {
        "match": {
            "asin": asin,
            "title": title,
            "authors": [author] if author else [],
            "series": series_name or "",
            "sequence": seq,
        }
    }
    return UnitState(path=path, status=status, asin=asin, title=title, author=author,
                     detail=json.dumps(detail), output=output)


def entry(seq, asin, title, hours=10.0):
    return {"asin": asin, "title": title, "sequence": seq, "hours": hours,
            "image": f"https://m.media-amazon.com/images/I/{asin}.jpg"}


def store(state, name, author, entries, title=None):
    key = series.group_key(name, author)
    payload = {"series_asin": "B0SERIES01", "title": title or name, "entries": entries, "fetched_at": time.time()}
    state.series_cache_put(key, True, payload, [(e["asin"], e["image"]) for e in entries])
    return key


FIVE = [entry(str(n), f"B0000000J{n}", f"Book {n}") for n in range(1, 6)]


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        # rendering must never reach the network; the refresher is checked on its own below
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.scheduled = self.schedule.start()

    def tearDown(self):
        self.schedule.stop()
        self.state.close()
        self.dir.cleanup()

    def owned(self, *numbers, name="Jack Reacher", author="Lee Child"):
        for n in numbers:
            self.state.save(book(f"{author}/{name}/{n}", f"Book {n}", author, name, str(n)))


class TestGapComputation(StateCase):
    def test_owned_one_to_three_of_five_misses_four_and_five(self):
        self.owned(1, 2, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        view = series.aggregate(self.state)["series"][0]
        self.assertTrue(view["known"])
        self.assertEqual([m["n"] for m in view["missing"]], [4, 5])
        self.assertEqual((view["have"], view["total"]), (3, 5))
        self.assertEqual(view["next_gap"], {"n": 4, "title": "Book 4"})
        self.assertEqual([t["kind"] for t in view["tiles"]], ["owned"] * 3 + ["gap"] * 2)

    def test_unnumbered_and_between_entries_are_listed_but_never_missing(self):
        self.owned(1, 2, 3)
        extras = [
            entry("", "B0NOVELLA1", "High Heat"),
            entry("", "B0NOVELLA2", "Small Wars"),
            entry("23.6", "B0BETWEEN1", "No Middle Name"),
            entry("1-3", "B0BOXSET01", "Books 1-3"),
        ]
        store(self.state, "Jack Reacher", "Lee Child", FIVE + extras)
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual([m["n"] for m in view["missing"]], [4, 5])
        self.assertEqual(view["total"], 5)
        self.assertEqual(view["unnumbered"], 2)
        self.assertEqual(view["between"], ["23.6"])
        self.assertEqual(view["other"], 1)
        self.assertEqual({e["asin"] for e in view["extras"]}, {e["asin"] for e in extras})
        self.assertNotIn("B0BETWEEN1", {m["asin"] for m in view["missing"]})

    def test_i_have_this_takes_an_asin_off_the_missing_list(self):
        self.owned(1, 2, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        self.state.set_owned_mark("B0000000J4")
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual([m["n"] for m in view["missing"]], [5])
        self.assertEqual([m["n"] for m in view["marked"]], [4])
        self.assertEqual(view["tiles"][3]["kind"], "marked")  # no longer a dashed gap
        self.assertEqual(view["have"], 4)
        self.state.set_owned_mark("B0000000J4", False)
        self.assertEqual([m["n"] for m in series.aggregate(self.state)["series"][0]["missing"]], [4, 5])

    def test_a_book_matched_to_a_series_asin_owns_that_number_without_a_sequence(self):
        self.owned(1, 2)
        self.state.save(book("Lee Child/x/third", "Some Other Title", seq="", asin="B0000000J3"))
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        view = series.aggregate(self.state)["series"][0]
        self.assertEqual([m["n"] for m in view["missing"]], [4, 5])

    def test_sequence_shapes(self):
        self.assertEqual(series.seq_number("19"), 19)
        self.assertEqual(series.seq_number("07"), 7)
        self.assertEqual(series.seq_number("19.0"), 19)
        for raw in ("", None, "23.6", "1-3", "one"):
            self.assertIsNone(series.seq_number(raw), raw)


class TestGrouping(StateCase):
    def test_groups_by_series_and_author_and_drops_one_book_series(self):
        self.owned(1, 2, 3)
        self.owned(1, 2, name="Riyria Revelations", author="Michael J. Sullivan")
        self.owned(4, name="Harry Hole", author="Jo Nesbo")  # a single book: no shelf card
        # same series name, different author: a separate series
        self.owned(1, 2, name="Jack Reacher", author="Someone Else")
        shelf = series.aggregate(self.state)
        names = sorted((v["name"], v["author"], v["owned"]) for v in shelf["series"])
        self.assertEqual(
            names,
            [("Jack Reacher", "Lee Child", 3), ("Jack Reacher", "Someone Else", 2),
             ("Riyria Revelations", "Michael J. Sullivan", 2)],
        )
        slugs = {v["slug"] for v in shelf["series"]}
        self.assertEqual(len(slugs), 3)
        self.assertIn("jack-reacher", slugs)

    def test_series_title_variants_share_one_group(self):
        self.state.save(book("a/1", "The Lincoln Lawyer", "Michael Connelly", "A Lincoln Lawyer Novel", "1"))
        self.state.save(book("a/2", "The Brass Verdict", "Michael Connelly", "Lincoln Lawyer", "2"))
        self.assertEqual(len(series.aggregate(self.state)["series"]), 1)

    def test_operator_override_beats_the_catalog_series(self):
        row = {"path": "p", "title": "T", "author": "A", "output": "",
               "detail": json.dumps({"match": {"series": "Catalog Series", "sequence": "3"}}),
               "overrides": json.dumps({"series": "My Series", "series_index": "9"})}
        self.assertEqual(series.unit_series(row), ("My Series", "9"))

    def test_persisted_match_beats_the_folder(self):
        row = {"path": "Lee Child - Wrong Series/14 - 61 Hours", "title": "61 Hours", "author": "Lee Child",
               "output": "", "overrides": "",
               "detail": json.dumps({"match": {"series": "Jack Reacher", "sequence": "14"}})}
        self.assertEqual(series.unit_series(row), ("Jack Reacher", "14"))

    def test_output_path_carries_series_for_rows_recorded_before_this_version(self):
        saved = CONFIG.output_root
        CONFIG.output_root = pathlib.Path("/output")
        try:
            row = {"path": "x", "title": "61 Hours", "author": "Lee Child", "detail": "{}", "overrides": "",
                   "output": "/output/Lee Child/Jack Reacher/14 - 61 Hours/61 Hours.m4b"}
            self.assertEqual(series.unit_series(row), ("Jack Reacher", "14"))
            row["output"] = "/output/Lee Child/61 Hours/61 Hours.m4b"  # not in a series
            self.assertEqual(series.unit_series(row)[0], "")
        finally:
            CONFIG.output_root = saved

    def test_folder_name_is_the_last_resort(self):
        row = {"path": "Lee Child - Jack Reacher/14 - 61 Hours", "title": "61 Hours", "author": "Lee Child",
               "detail": "{}", "overrides": "", "output": ""}
        self.assertEqual(series.unit_series(row), ("Jack Reacher", "14"))
        plain = dict(row, path="Lee Child/61 Hours")
        self.assertEqual(series.unit_series(plain), ("", ""))


class TestBadgeAndDegraded(StateCase):
    def test_rail_badge_is_the_sum_of_known_gaps(self):
        self.owned(1, 2, 3)  # 2 missing of 5
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        self.owned(1, 2, name="Riyria Revelations", author="Michael J. Sullivan")
        store(self.state, "Riyria Revelations", "Michael J. Sullivan",
              [entry(str(n), f"B0000000R{n}", f"R{n}") for n in range(1, 6)])  # 3 missing
        self.owned(1, 2, 3, 4, 5, name="Lincoln Lawyer", author="Michael Connelly")  # size unknown
        self.assertEqual(series.missing_total(self.state), 5)
        page = web.render(self.state)
        self.assertRegex(page, r'href="/series">\s*<span class="ab-glyph">&#9878;</span> Series\s*'
                               r'<span class="ab-count"[^>]*>5</span>')
        self.assertIn('class="ab-rail-item ab-alert" href="/series"', page)
        self.assertIn('<span class="ab-count" title="books missing from your series">5</span>',
                      web.render_settings(self.state))

    def test_series_tab_sits_directly_under_library(self):
        page = web.render(self.state)
        lib, ser, sett = page.index("</span> Library"), page.index("</span> Series"), page.index("</span> Settings")
        self.assertLess(lib, ser)
        self.assertLess(ser, sett)

    def test_unfetchable_series_invents_no_total(self):
        self.owned(1, 2, 3, 4, 5, name="Lincoln Lawyer", author="Michael Connelly")
        key = series.group_key("Lincoln Lawyer", "Michael Connelly")
        self.state.series_cache_put(key, False, {"reason": "unreachable"}, [])
        view = series.aggregate(self.state)["series"][0]
        self.assertFalse(view["known"])
        self.assertIsNone(view["total"])
        self.assertEqual(view["missing"], [])
        self.assertEqual(series.missing_total(self.state), 0)
        html = web.render_series(self.state)
        card = re.search(r'<article class="ab-series-card".*?</article>', html, re.S).group(0)
        self.assertIn("<b>5</b><span class=\"ab-series-of\">owned</span>", card)
        self.assertIn("fetching series size&hellip;", card)
        self.assertNotIn(" of ", card)
        self.assertNotIn("MISSING", card)

    def test_a_failed_refresh_keeps_the_last_good_listing(self):
        self.owned(1, 2, 3)
        key = store(self.state, "Jack Reacher", "Lee Child", FIVE)
        previous = self.state.series_cache_all()[key]
        with mock.patch.object(series, "FETCH", return_value=None):
            series.refresh_one(self.state, {"key": key, "name": "Jack Reacher", "author": "Lee Child",
                                            "owned_asins": [], "previous": previous})
        record = self.state.series_cache_all()[key]
        self.assertTrue(record["ok"])
        self.assertEqual(record["payload"]["last_error"], "unreachable")
        self.assertTrue(series.aggregate(self.state)["series"][0]["known"])
        self.assertTrue(series.is_stale(record, now=time.time() + series.TTL_FAIL_SECONDS + 1))

    def test_not_found_is_cached_as_unknown(self):
        self.owned(1, 2)
        key = series.group_key("Jack Reacher", "Lee Child")
        with mock.patch.object(series, "FETCH", return_value={}):
            series.refresh_one(self.state, {"key": key, "name": "Jack Reacher", "author": "Lee Child"})
        record = self.state.series_cache_all()[key]
        self.assertFalse(record["ok"])
        self.assertEqual(record["payload"]["reason"], "not_found")
        self.assertFalse(series.aggregate(self.state)["series"][0]["known"])


class TestRefreshStaysOffTheRenderPath(StateCase):
    def test_render_queues_stale_series_and_never_fetches_inline(self):
        # v0.15.1: rendering (a GET) never queues outbound lookups; the daemon loop does
        from audiobind import daemon

        self.owned(1, 2, 3)
        with mock.patch.object(series, "FETCH") as fetch:
            web.render(self.state)
            web.render_series(self.state)
            self.assertFalse(any(call.args[1] for call in self.scheduled.call_args_list))
            daemon.refresh_series(self.state)
        fetch.assert_not_called()
        jobs = self.scheduled.call_args[0][1]
        self.assertEqual([j["name"] for j in jobs], ["Jack Reacher"])

    def test_a_fresh_listing_is_not_refetched(self):
        self.owned(1, 2, 3)
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        series.aggregate(self.state)
        self.assertEqual(self.scheduled.call_args[0][1], [])

    def test_refresher_runs_one_at_a_time_and_stores_the_listing(self):
        self.owned(1, 2, 3)
        refresher = series.Refresher()
        gate = __import__("threading").Event()

        def slow(name, author, owned):
            gate.wait(5)
            return {"series_asin": "B0SERIES01", "title": name, "entries": FIVE}

        with mock.patch.object(series, "FETCH", side_effect=slow):
            job = {"key": series.group_key("Jack Reacher", "Lee Child"), "name": "Jack Reacher",
                   "author": "Lee Child"}
            self.assertTrue(refresher.schedule(self.state, [job]))
            self.assertFalse(refresher.schedule(self.state, [job]))  # one in flight
            gate.set()
            refresher.join(5)
        self.assertEqual(series.missing_total(self.state), 2)

    def test_listing_and_marks_move_the_page_signature(self):
        before = web.snapshot(self.state)["rev"]
        store(self.state, "Jack Reacher", "Lee Child", FIVE)
        after_listing = web.snapshot(self.state)["rev"]
        self.assertNotEqual(before, after_listing)
        self.state.set_owned_mark("B0000000J4")
        self.assertNotEqual(after_listing, web.snapshot(self.state)["rev"])


class TestCatalogListing(unittest.TestCase):
    @staticmethod
    def product(asin, title, seq, series_asin="B0SERIES01", series_title="Jack Reacher", lang="english", mins=600):
        return {"asin": asin, "title": title, "language": lang, "runtime_length_min": mins,
                "product_images": {"500": f"https://m.media-amazon.com/images/I/{asin}.jpg"},
                "series": [{"asin": series_asin, "title": series_title, "sequence": seq}]}

    def test_pick_series_votes_dedupes_and_skips_other_languages(self):
        products = [
            self.product("B0000000J1", "Killing Floor", "1"),
            self.product("B0000000X1", "Killing Floor (dramatized)", "1", mins=0),
            self.product("B0000000J2", "Die Trying", "2"),
            self.product("B0000000G2", "Ausgeliefert", "2", lang="german"),
            self.product("B0000000N1", "High Heat", ""),
            self.product("B0000000O1", "Someone Else", "1", series_asin="B0OTHER001", series_title="Jack Reacher"),
        ]
        picked = series.pick_series(products, "Jack Reacher", owned_asins=set())
        self.assertEqual(picked["series_asin"], "B0SERIES01")
        self.assertEqual([(e["sequence"], e["asin"]) for e in picked["entries"]],
                         [("1", "B0000000J1"), ("2", "B0000000J2"), ("", "B0000000N1")])
        self.assertEqual(picked["entries"][0]["hours"], 10.0)

    def test_an_owned_edition_wins_its_number(self):
        products = [self.product("B0000000J1", "Killing Floor", "1"),
                    self.product("B0000000K1", "Killing Floor", "1")]
        picked = series.pick_series(products, "Jack Reacher", owned_asins={"B0000000K1"})
        self.assertEqual(picked["entries"][0]["asin"], "B0000000K1")

    def test_fetch_walks_pages_until_a_short_one(self):
        full = [self.product(f"B00000{n:04d}", f"T{n}", str(n)) for n in range(1, 51)]
        tail = [self.product("B000000051", "T51", "51")]
        with mock.patch.object(catalog, "series_products", side_effect=[full, tail]) as call:
            result = series.fetch_series("Jack Reacher", "Lee Child", set())
        self.assertEqual(call.call_count, 2)
        self.assertEqual(len(result["entries"]), 51)

    def test_fetch_failure_is_none_and_not_found_is_empty(self):
        with mock.patch.object(catalog, "series_products", return_value=None):
            self.assertIsNone(series.fetch_series("Jack Reacher", "Lee Child", set()))
        with mock.patch.object(catalog, "series_products", return_value=[]):
            self.assertEqual(series.fetch_series("Jack Reacher", "Lee Child", set()), {})

    def test_series_search_goes_through_the_throttled_memoised_helper(self):
        with mock.patch.object(catalog, "_get_json", return_value={"products": [{"asin": "B0000000J1"}]}) as get:
            self.assertEqual(catalog.series_products("Jack Reacher", "Lee Child", 1), [{"asin": "B0000000J1"}])
        url = get.call_args[0][0]
        self.assertIn("keywords=Jack+Reacher", url)
        self.assertIn("author=Lee+Child", url)
        self.assertIn("page=1", url)


class TestPages(StateCase):
    def setUp(self):
        super().setUp()
        self.owned(*range(1, 19))
        reacher = [entry(str(n), f"B000000{n:03d}", f"Book {n}", 12.0) for n in range(1, 32)]
        reacher[18]["title"] = "Personal"
        reacher += [entry("", "B0NOVELLA1", "High Heat"), entry("", "B0NOVELLA2", "Small Wars"),
                    entry("23.6", "B0BETWEEN1", "No Middle Name")]
        store(self.state, "Jack Reacher", "Lee Child", reacher)
        self.owned(1, 2, 3, name="Riyria Revelations", author="Michael J. Sullivan")
        store(self.state, "Riyria Revelations", "Michael J. Sullivan",
              [entry(str(n), f"B0000000R{n}", f"R{n}") for n in range(1, 4)])

    def test_series_page_copy_matches_the_mockup(self):
        html = web.render_series(self.state)
        self.assertIn("<b>18</b><span class=\"ab-series-of\">of 31 owned</span>", html)
        self.assertIn('<span class="ab-series-missing">13 MISSING</span>', html)
        self.assertIn('<span class="ab-series-complete">COMPLETE</span>', html)
        self.assertIn("nothing to chase", html)
        self.assertIn("Missing from Jack Reacher &mdash; 13 books", html)
        self.assertIn("Show the 13 missing &darr;", html)
        self.assertIn("next gap: #19 Personal", html)
        self.assertIn("Lee Child &middot; mainline numbering", html)
        visible = html.split('<details class="ab-series-extra">')[0]
        self.assertEqual(visible.count(">Not in your library<"), 13)
        self.assertEqual(html.count('abSeriesMark(this,true)">I have this<'), 13)  # extras only copy a link
        self.assertGreaterEqual(html.count(">Copy Audible link<"), 13)
        self.assertEqual(html.count("ab-series-tile ab-series-gap"), 13)
        self.assertIn("2</b><span>novellas, unnumbered &middot; hidden", html)
        self.assertIn("between-the-numbers entry (#23.6) &middot; hidden", html)
        self.assertIn('<details class="ab-series-extra">', html)
        self.assertIn('data-link="https://www.audible.com/pd?asin=B000000019"', html)
        self.assertIn('id="ab-series-jack-reacher"', html)
        self.assertIn('class="ab-rail-item ab-active ab-alert" href="/series" aria-current="page"', html)
        self.assertIn("12.0 hrs &middot; ASIN B000000019 &middot; Audible.com", html)

    def test_detail_panel_hook_for_a_book_in_a_shelf_series(self):
        page = web.render(self.state)
        self.assertIn("Jack Reacher &mdash; you're 18 of 31", page)
        self.assertIn("13 books missing from this series, starting with #19 Personal", page)
        self.assertIn('href="/series#jack-reacher">See what you\'re missing</a>', page)
        hooks = re.findall(r'<div class="ab-series-hook">.*?</a></div>', page, re.S)
        hook = next(h for h in hooks if "Jack Reacher" in h)
        self.assertEqual(hook.count('class="ab-series-tile"'), 8)
        self.assertEqual(hook.count("ab-series-tile ab-series-gap"), 5)
        self.assertIn("Riyria Revelations &mdash; you're 3 of 3", page)

    def test_no_hook_for_a_book_outside_any_shelf_series(self):
        self.state.save(UnitState(path="Solo/Book", status="converted", title="Standalone", author="Nobody"))
        page = web.render(self.state, query="Standalone")
        panel = re.search(r'<tr class="drow" data-path="Solo/Book".*?</tr>', page, re.S).group(0)
        self.assertNotIn("ab-series-hook", panel)

    def test_brace_in_a_series_title_renders(self):
        self.owned(1, 2, name="Dune {Saga}", author="Frank {Herbert}")
        self.assertIn("Dune {Saga}", web.render_series(self.state))
        web.render(self.state)


class TestAssets(unittest.TestCase):
    def test_series_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            js = pathlib.Path(tmp) / "series.js"
            js.write_text(web.SERIES_SCRIPT, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_external_assets_in_the_series_templates(self):
        for marker in ("http://", "https://", "//cdn"):
            self.assertNotIn(marker, web.SERIES_PAGE + web.SERIES_SCRIPT)


class TestRecordPersistsSeries(unittest.TestCase):
    def test_match_detail_carries_series_and_sequence(self):
        from audiobind import matching, pipeline
        from audiobind.units import BookUnit

        with tempfile.TemporaryDirectory() as tmp:
            state = State(pathlib.Path(tmp))
            unit = BookUnit(path="/l/x", name="x", parent="/l", parts=[], rel_path="x")
            audit = pipeline.Audit(unit=unit)
            audit.match = matching.Match(
                product=catalog.Product(asin="B003G8RX9O", title="61 Hours", authors=["Lee Child"],
                                        series="Jack Reacher", sequence="14")
            )
            pipeline.record(state, audit, "review")
            match = json.loads(state.get("x").detail)["match"]
            state.close()
        self.assertEqual((match["series"], match["sequence"]), ("Jack Reacher", "14"))


class TestSeriesEndpoints(LiveServerCase):
    def setUp(self):
        super().setUp()
        self.schedule = mock.patch.object(series.REFRESHER, "schedule", return_value=False)
        self.schedule.start()
        saved_state_dir = CONFIG.state_dir
        CONFIG.state_dir = self.root / "state"
        self.addCleanup(setattr, CONFIG, "state_dir", saved_state_dir)
        for n in (1, 2, 3):
            self.state.save(book(f"Lee Child/Jack Reacher/{n}", f"Book {n}", seq=str(n)))
        store(self.state, "Jack Reacher", "Lee Child", FIVE)

    def tearDown(self):
        self.schedule.stop()
        super().tearDown()

    def test_mark_and_undo(self):
        status, body = self.post({"action": "mark_owned", "asin": "B0000000J4"}, "/api/series")
        self.assertEqual((status, body["ok"]), (200, True))
        status, shelf = self.request("/api/series")
        self.assertEqual(shelf["missing_total"], 1)
        self.assertEqual([m["n"] for m in shelf["series"][0]["marked"]], [4])
        status, _ = self.post({"action": "unmark_owned", "asin": "B0000000J4"}, "/api/series")
        self.assertEqual(self.request("/api/series")[1]["missing_total"], 2)

    def test_mark_refuses_an_asin_from_no_series_listing(self):
        status, _ = self.post({"action": "mark_owned", "asin": "B0NOTLISTD"}, "/api/series")
        self.assertEqual(status, 404)
        status, _ = self.post({"action": "mark_owned", "asin": "../etc"}, "/api/series")
        self.assertEqual(status, 400)
        self.assertEqual(self.state.owned_marks(), set())

    def test_series_post_has_the_cross_site_guard(self):
        status, _ = self.request("/api/series", json.dumps({"action": "mark_owned", "asin": "B0000000J4"}).encode(),
                                 {"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.state.owned_marks(), set())

    def test_series_page_is_served(self):
        status, html = self.request("/series", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Missing from Jack Reacher &mdash; 2 books", html)

    def test_cover_proxy_allows_listed_series_asins_only(self):
        with mock.patch.object(catalog, "download", return_value=False) as download:
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0000000J5"))
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args[0][0], "https://m.media-amazon.com/images/I/B0000000J5.jpg")
            # the .miss marker still stops a second outbound try
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0000000J5"))
            self.assertEqual(download.call_count, 1)
            # an ASIN in no listing and no book: no request at all
            self.assertIsNone(covers.fetch_by_asin(self.state, "B0NOTLISTD"))
            self.assertEqual(download.call_count, 1)


if __name__ == "__main__":
    unittest.main()

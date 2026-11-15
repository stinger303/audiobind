"""0.18.0-dev1: the tracker client - search parsing, candidate selection, politeness.

The network is a fake transport: no test here can reach a tracker.
"""

import json
import pathlib
import unittest
import urllib.parse

from audiobind import tracker
from tests.tracker_cfg import SEARCH_URL, endpoints, make_client

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "search_answer.json"
TORRENT = b"d8:announce30:https://tr.tracker.example/ann4:infod6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text())


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Records every call; answers from a queue of (status, headers, body)."""

    def __init__(self, *answers, clock=None):
        self.answers = list(answers)
        self.calls = []
        self.clock = clock

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers),
                           "at": self.clock() if self.clock else None})
        answer = self.answers.pop(0) if self.answers else (200, {}, b'{"data": []}')
        if isinstance(answer, Exception):
            raise answer
        return answer


def client(*answers, session="cookie-value", **kwargs):
    clock = FakeClock()
    transport = FakeTransport(*answers, clock=clock)
    limiter = tracker.RateLimiter(clock=clock, sleep=clock.sleep)
    backoff = tracker.Backoff(clock=clock)
    return make_client(session, transport=transport, limiter=limiter, backoff=backoff, **kwargs), transport, clock


class TestParseSearch(unittest.TestCase):
    def setUp(self):
        self.rows = tracker.parse_search(fixture())

    def test_entries_without_a_numeric_id_or_not_objects_are_dropped(self):
        self.assertEqual([t.tid for t in self.rows], ["1001", "1002", "1003", "1004", "1005"])

    def test_author_info_is_a_json_string(self):
        phm = self.rows[0]
        self.assertEqual(phm.authors, ["Andy Weir"])
        self.assertEqual(phm.narrators, ["Ray Porter"])
        self.assertEqual(self.rows[3].narrators, ["Michael Kramer", "Kate Reading"])

    def test_series_info(self):
        self.assertEqual(self.rows[3].series, [("The Stormlight Archive", "1")])
        self.assertEqual(self.rows[0].series, [])
        self.assertEqual(self.rows[4].series, [("bare", "")])

    def test_malformed_fields_fall_back_to_empty(self):
        broken = self.rows[4]
        self.assertEqual(broken.authors, [])
        self.assertEqual(broken.narrators, [])
        self.assertEqual(broken.numfiles, 0)
        self.assertEqual(broken.name, "Mistborn & The Final Empire", "HTML entities are unescaped")
        self.assertFalse(broken.is_audio, "an epub is never an audiobook")

    def test_flags(self):
        self.assertEqual(self.rows[1].numfiles, 0)
        self.assertFalse(self.rows[0].snatched)
        self.assertFalse(self.rows[1].snatched, '"0" is not snatched')
        self.assertTrue(self.rows[2].snatched)
        self.assertTrue(self.rows[0].is_audio)
        self.assertEqual(self.rows[0].category, "Audiobooks - Science Fiction")

    def test_freeleech_decision_reads_the_raw_entry(self):
        self.assertTrue(tracker.freeleech_decision(self.rows[0].raw)[0])
        self.assertFalse(tracker.freeleech_decision(self.rows[1].raw)[0], 'free "1"')
        self.assertFalse(tracker.freeleech_decision(self.rows[2].raw)[0], "fl_vip")
        self.assertFalse(tracker.freeleech_decision(self.rows[3].raw)[0], "personal freeleech")

    def test_garbage_payloads(self):
        for payload in (None, [], "x", {}, {"data": "nope"}, {"data": None}):
            self.assertEqual(tracker.parse_search(payload), [])


class TestSelectCandidate(unittest.TestCase):
    def setUp(self):
        self.rows = tracker.parse_search(fixture())

    def test_exact_match_wins_and_files_beat_an_empty_torrent(self):
        pick, why = tracker.select_candidate({"title": "Project Hail Mary", "authors": ["Andy Weir"]}, self.rows)
        self.assertEqual(pick.tid, "1001", "the free one has numfiles 0, so the one with files wins")
        self.assertIn("#1001", why)

    def test_near_match_passes(self):
        pick, _ = tracker.select_candidate({"title": "The Way of Kings", "authors": ["Brandon Sanderson"],
                                        "series": "The Stormlight Archive", "sequence": "1"}, self.rows)
        self.assertEqual(pick.tid, "1004")
        pick, why = tracker.select_candidate({"title": "Project Hail Mary: A Novel", "authors": ["Andy Weir"]},
                                         self.rows)
        self.assertEqual(pick.tid, "1001")

    def test_below_threshold_is_not_found_never_forced(self):
        pick, why = tracker.select_candidate({"title": "Project Hail", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        self.assertIn("closest was", why)
        pick, why = tracker.select_candidate({"title": "Artemis", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        pick, why = tracker.select_candidate({"title": "Anything"}, [])
        self.assertEqual((pick, why), (None, "no results"))

    def test_author_must_match_when_the_row_has_one(self):
        pick, _ = tracker.select_candidate({"title": "Project Hail Mary", "authors": ["Someone Else"]}, self.rows)
        self.assertIsNone(pick)
        pick, _ = tracker.select_candidate({"title": "Project Hail Mary", "authors": []}, self.rows)
        self.assertEqual(pick.tid, "1001", "no author on the row: title alone decides")

    def test_already_snatched_is_skipped(self):
        pick, why = tracker.select_candidate({"title": "The Martian", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        self.assertIn("snatched", why)

    def test_not_audio_is_skipped(self):
        pick, _ = tracker.select_candidate({"title": "Mistborn & The Final Empire"}, self.rows)
        self.assertIsNone(pick)

    def test_series_sequence_match_is_preferred(self):
        a = tracker.Torrent(tid="1", name="Dune", authors=["Frank Herbert"], numfiles=2, filetype="mp3",
                        series=[("Dune", "2")], raw={})
        b = tracker.Torrent(tid="2", name="Dune", authors=["Frank Herbert"], numfiles=2, filetype="mp3",
                        series=[("Dune", "1")], raw={})
        pick, why = tracker.select_candidate({"title": "Dune", "authors": ["Frank Herbert"], "series": "Dune",
                                          "sequence": "1"}, [a, b])
        self.assertEqual(pick.tid, "2")
        self.assertIn("series match", why)

    def test_dune_never_takes_dune_messiah(self):
        t = tracker.Torrent(tid="9", name="Dune Messiah", authors=["Frank Herbert"], numfiles=1, filetype="m4b")
        self.assertIsNone(tracker.select_candidate({"title": "Dune", "authors": ["Frank Herbert"]}, [t])[0])

    def test_subtitle_stripping_never_shortens_both_sides(self):
        t = tracker.Torrent(tid="9", name="Star Wars: Heir to the Empire", authors=["Timothy Zahn"], numfiles=1,
                        filetype="m4b")
        self.assertIsNone(tracker.select_candidate({"title": "Star Wars: Thrawn", "authors": ["Timothy Zahn"]}, [t])[0])


class TestRateLimiter(unittest.TestCase):
    def test_three_seconds_between_requests(self):
        mc, transport, clock = client((200, {}, b'{"data": []}'), (200, {}, b'{"data": []}'),
                                      (200, {}, b'{"data": []}'))
        mc.search("a")
        clock.now += 1.0  # the caller did something else for a second
        mc.search("b")
        mc.search("c")
        starts = [c["at"] for c in transport.calls]
        self.assertEqual(len(starts), 3)
        for earlier, later in zip(starts, starts[1:]):
            self.assertGreaterEqual(later - earlier, tracker.MIN_SPACING)
        self.assertEqual(clock.sleeps, [2.0, 3.0])

    def test_no_wait_before_the_first_request(self):
        mc, _, clock = client()
        mc.search("a")
        self.assertEqual(clock.sleeps, [])

    def test_spacing_is_at_least_three_seconds(self):
        self.assertGreaterEqual(tracker.MIN_SPACING, 3.0)


class TestBackoff(unittest.TestCase):
    def test_429_starts_a_backoff_that_blocks_without_a_request(self):
        mc, transport, clock = client((429, {}, b"slow down"))
        with self.assertRaises(tracker.TrackerError) as ctx:
            mc.search("a")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(mc.backoff.remaining(), 60)
        with self.assertRaises(tracker.TrackerBackoff):
            mc.search("b")
        with self.assertRaises(tracker.TrackerBackoff):
            mc.download(1, False)
        self.assertEqual(len(transport.calls), 1, "nothing is sent while backing off")

    def test_doubles_to_a_thirty_minute_cap_and_resets_on_success(self):
        b = tracker.Backoff(clock=FakeClock())
        self.assertEqual([b.fail() for _ in range(8)], [60, 120, 240, 480, 960, 1800, 1800, 1800])
        b.ok()
        self.assertEqual((b.remaining(), b.fail()), (0.0, 60))

    def test_any_non_200_backs_off(self):
        for status in (403, 500, 503, 302):
            mc, _, _ = client((status, {}, b""))
            with self.assertRaises(tracker.TrackerError):
                mc.search("x")
            self.assertGreater(mc.backoff.remaining(), 0, status)

    def test_retry_after_longer_than_the_backoff_is_honoured(self):
        mc, _, _ = client((429, {"Retry-After": "900"}, b""))
        with self.assertRaises(tracker.TrackerError):
            mc.search("x")
        self.assertEqual(mc.backoff.remaining(), 900)

    def test_unreachable_backs_off(self):
        mc, _, _ = client(TimeoutError("timed out"))
        with self.assertRaises(tracker.TrackerError):
            mc.search("x")
        self.assertEqual(mc.backoff.remaining(), 60)

    def test_html_instead_of_json_backs_off_and_is_never_parsed(self):
        mc, _, _ = client((200, {}, b"<html><body>Please log in</body></html>"))
        with self.assertRaises(tracker.TrackerError) as ctx:
            mc.search("x")
        self.assertIn("not answer JSON", str(ctx.exception))
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_nothing_returned_is_an_empty_result_not_an_error(self):
        mc, _, _ = client((200, {}, b'{"error": "Nothing returned, out of 0"}'))
        self.assertEqual(mc.search("x"), [])
        self.assertEqual(mc.backoff.remaining(), 0)

    def test_other_json_errors_back_off(self):
        mc, _, _ = client((200, {}, b'{"error": "You are not logged in"}'))
        with self.assertRaises(tracker.TrackerError):
            mc.search("x")
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_restore_carries_a_backoff_across_restarts(self):
        clock = FakeClock()
        b = tracker.Backoff(clock=clock)
        b.restore(clock.now + 300, failures=3)
        self.assertEqual((b.remaining(), b.failures), (300, 3))
        b2 = tracker.Backoff(clock=clock)
        b2.restore(clock.now - 5)
        self.assertEqual(b2.remaining(), 0)


class TestRequests(unittest.TestCase):
    def test_search_request_shape(self):
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()))
        rows = mc.search("Project Hail Mary", "Andy Weir")
        self.assertEqual(len(rows), 5)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://tracker.example/search.json"))
        self.assertEqual(call["headers"]["Cookie"], "session_cookie=cookie-value")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["data"]), {"q": "Project Hail Mary Andy Weir", "category": "13"})

    def test_download_with_and_without_the_credit(self):
        mc, transport, _ = client((200, {}, TORRENT), (200, {}, TORRENT))
        self.assertEqual(mc.download(1001, True), TORRENT)
        self.assertEqual(mc.download("1002", False), TORRENT)
        urls = [c["url"] for c in transport.calls]
        self.assertEqual(urls, ["https://tracker.example/download.php?tid=1001&fl",
                                "https://tracker.example/download.php?tid=1002"])
        self.assertTrue(all(c["method"] == "GET" for c in transport.calls))

    def test_download_that_is_not_a_torrent_is_an_error(self):
        mc, _, _ = client((200, {}, b'{"error": "not enough credits"}'))
        with self.assertRaises(tracker.TrackerError):
            mc.download(1, True)
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_only_the_two_authorized_endpoints(self):
        eps = endpoints()
        self.assertTrue(tracker.authorized_url(SEARCH_URL, eps))
        self.assertTrue(tracker.authorized_url(tracker.download_url(eps, 5, True), eps))
        for url in ("https://tracker.example/", "https://tracker.example/bookmarks.php",
                    "https://tracker.example/search.json?x=1",
                    "https://tracker.example/download.php/abc?tid=1",
                    "https://evil.example/tor/download.php?tid=1"):
            self.assertFalse(tracker.authorized_url(url, eps), url)
            with self.assertRaises(tracker.TrackerError):
                tracker.urllib_transport("GET", url, None, {}, 1, endpoints=eps)

    def test_no_session_no_request(self):
        mc, transport, _ = client(session="")
        with self.assertRaises(tracker.TrackerError):
            mc.search("x")
        self.assertEqual(transport.calls, [])

    def test_repr_never_shows_the_cookie(self):
        mc, _, _ = client(session="SECRET-COOKIE")
        self.assertNotIn("SECRET-COOKIE", repr(mc))

    def test_redirects_are_never_followed(self):
        handler = tracker._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://tracker.example/login.php"))


ALT_ROW = {
    "torrent_id": 77, "title": "Project Hail Mary", "authors": ["Andy Weir"], "narrators": {"1": "Ray Porter"},
    "series": [["Hail Mary", "1"]], "size_bytes": 1234, "added_at": "2021-05-04", "files": 3, "format": "mp3",
    "category": "Audiobooks - Science Fiction", "snatched": 0, "freeleech": 1, "personal_fl": "2030-01-01 00:00:00",
}


class TestAlternativeSpellings(unittest.TestCase):
    def test_alternative_spellings_parse_to_the_same_torrent(self):
        canonical = {
            "id": 77, "name": "Project Hail Mary", "author_info": json.dumps({"1": "Andy Weir"}),
            "narrator_info": json.dumps({"1": "Ray Porter"}), "series_info": json.dumps({"9": ["Hail Mary", "1"]}),
            "size": "1234", "added": "2021-05-04", "numfiles": 3, "filetype": "mp3",
            "catname": "Audiobooks - Science Fiction", "my_snatched": 0, "free": 1,
            "personal_freeleech": "2030-01-01 00:00:00",
        }
        a = tracker.parse_search({"data": [canonical]})[0]
        b = tracker.parse_search({"results": [ALT_ROW]})[0]
        for attr in ("tid", "name", "authors", "narrators", "series", "size", "added", "numfiles", "filetype",
                     "category", "snatched"):
            self.assertEqual(getattr(a, attr), getattr(b, attr), attr)
        self.assertEqual(tracker.freeleech_decision(a.raw)[0], tracker.freeleech_decision(b.raw)[0])
        self.assertFalse(tracker.freeleech_decision(b.raw)[0])

    def test_personal_freeleech_boolean_and_datetime_forms(self):
        self.assertFalse(tracker.freeleech_decision({"personal_freeleech": "2030-01-01 00:00:00"})[0])
        self.assertFalse(tracker.freeleech_decision({"personal_fl": True})[0])
        self.assertTrue(tracker.freeleech_decision({"personal_freeleech": "0000-00-00 00:00:00"})[0])

    def test_a_payload_missing_everything_yields_no_candidates(self):
        for payload in ({}, {"data": [{}]}, {"data": [{"unknown": 1}, 5, None, []]}, {"results": "x"},
                        {"torrents": [{"id": "abc"}]}):
            self.assertEqual(tracker.parse_search(payload), [], payload)

    def test_unrecognised_fields_are_simply_absent(self):
        row = tracker.parse_search({"data": [{"id": 5, "zzz": object.__name__}]})[0]
        self.assertEqual((row.name, row.authors, row.series, row.numfiles, row.filetype), ("", [], [], 0, ""))


class TestSearchBody(unittest.TestCase):
    TEMPLATE = '{"q": "{{title}} {{author}}", "who": "{{author}}", "cat": ["{{category}}"], "fixed": 25}'

    def build(self, title="Dune", author="Frank Herbert", category="7"):
        return tracker.build_search_document(self.TEMPLATE, title, author, category)

    def test_both_placeholders_and_a_category_produce_the_exact_document(self):
        self.assertEqual(self.build(), {"q": "Dune Frank Herbert", "who": "Frank Herbert", "cat": ["7"], "fixed": 25})

    def test_a_missing_author_never_breaks_the_document(self):
        doc = self.build(author="")
        self.assertEqual(doc, {"q": "Dune", "cat": ["7"], "fixed": 25})
        json.loads(json.dumps(doc))

    def test_an_unset_category_drops_its_list_item(self):
        self.assertEqual(self.build(category="")["cat"], [])

    def test_quotes_and_backslashes_in_a_title_stay_inside_the_string(self):
        doc = self.build(title='He said "go" \\ {{author}}', author="")
        self.assertEqual(doc["q"], 'He said "go" \\ {{author}}')
        self.assertEqual(json.loads(json.dumps(doc)), doc)

    def test_an_unset_body_is_an_error_and_sends_nothing(self):
        mc, transport, _ = client(endpoints=endpoints(search_body=""))
        with self.assertRaises(tracker.TrackerError) as ctx:
            mc.search("Dune")
        self.assertIn("TRACKER_SEARCH_BODY", str(ctx.exception))
        self.assertEqual(transport.calls, [])

    def test_get_encodes_the_same_document(self):
        doc = self.build()
        self.assertEqual(tracker.document_params(doc),
                         [("q", "Dune Frank Herbert"), ("who", "Frank Herbert"), ("cat", "7"), ("fixed", "25")])
        self.assertEqual(tracker.document_params({"a": {"b": True, "c": None}, "d": ["x", "y"]}),
                         [("a[b]", "true"), ("d", "x"), ("d", "y")])

    def test_no_site_dialect_is_left_in_the_client(self):
        source = pathlib.Path(tracker.__file__).read_text()
        for word in ("tor[", "searchIn", "browse_lang", "sortType", "perpage", "PER_PAGE"):
            self.assertNotIn(word, source)


class TestFormEncoding(unittest.TestCase):
    FORM = ('{"tor[text]": "{{title}}", "tor[author]": "{{author}}", "tor[searchIn]": "torrents", '
            '"tor[browse_lang][]": ["english"], "perpage": 25}')
    OLD_CLIENT_BODY = ("tor%5Btext%5D=Dune&tor%5Bauthor%5D=Frank+Herbert&tor%5BsearchIn%5D=torrents"
                       "&tor%5Bbrowse_lang%5D%5B%5D=english&perpage=25")

    def post(self, title="Dune", author="Frank Herbert", template=None, **kw):
        eps = endpoints(search_body=template or self.FORM, search_encoding="form", search_category="", **kw)
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()), endpoints=eps)
        mc.search(title, author)
        return transport.calls[0]

    def test_a_form_template_sends_exactly_what_the_old_client_sent(self):
        call = self.post()
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(call["data"].decode(), self.OLD_CLIENT_BODY)

    def test_nested_list_bool_number_and_null(self):
        body = '{"tor": {"text": "{{title}}", "n": 3, "x": null}, "l": ["a", "b"], "t": true, "f": false, "p": 1.5}'
        self.assertEqual(self.post(template=body)["data"].decode(),
                         "tor%5Btext%5D=Dune&tor%5Bn%5D=3&l=a&l=b&t=true&f=false&p=1.5")

    def test_an_empty_author_omits_its_key(self):
        data = self.post(author="")["data"].decode()
        self.assertNotIn("author", data)
        self.assertEqual(data, "tor%5Btext%5D=Dune&tor%5BsearchIn%5D=torrents"
                               "&tor%5Bbrowse_lang%5D%5B%5D=english&perpage=25")
        self.assertNotIn("=&", data)

    def test_an_empty_title_omits_its_key(self):
        self.assertNotIn("tor%5Btext%5D", self.post(title="")["data"].decode())

    def test_json_is_still_the_default_and_unchanged(self):
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()), endpoints=endpoints())
        mc.search("Dune", "Frank Herbert")
        call = transport.calls[0]
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["data"]), {"q": "Dune Frank Herbert", "category": "13"})


class TestConfiguredEndpoints(unittest.TestCase):
    def test_get_search_puts_the_form_in_the_query_and_only_that_url_is_authorized(self):
        eps = endpoints(search_method="GET")
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()), endpoints=eps)
        mc.search("Dune", "Frank Herbert")
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].startswith(SEARCH_URL + "?"))
        self.assertIsNone(call["data"])
        self.assertEqual(urllib.parse.parse_qsl(call["url"].split("?", 1)[1]),
                         [("q", "Dune Frank Herbert"), ("category", "13")])
        self.assertTrue(tracker.authorized_url(call["url"], eps))
        self.assertFalse(tracker.authorized_url(call["url"], endpoints()), "a POST install never sends a query search")

    def test_no_category_configured_leaves_its_key_out(self):
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()), endpoints=endpoints(search_category=""))
        mc.search("Dune")
        self.assertEqual(json.loads(transport.calls[0]["data"]), {"q": "Dune"})

    def test_the_freeleech_parameter_is_never_sent_unless_configured(self):
        eps = endpoints(freeleech_param="")
        mc, transport, _ = client((200, {}, TORRENT), endpoints=eps)
        self.assertFalse(mc.can_spend)
        mc.download(9, True)
        self.assertEqual(transport.calls[0]["url"], "https://tracker.example/download.php?tid=9")
        spend, why = tracker.freeleech_decision({}, can_spend=False)
        self.assertFalse(spend)
        self.assertIn("no freeleech parameter", why)

    def test_a_custom_download_parameter_and_cookie_name(self):
        eps = endpoints(download_param="id", freeleech_param="free=1")
        mc, transport, _ = client((200, {}, TORRENT), endpoints=eps, cookie_name="sid")
        mc.download(9, True)
        self.assertEqual(transport.calls[0]["url"], "https://tracker.example/download.php?id=9&free=1")
        self.assertEqual(transport.calls[0]["headers"]["Cookie"], "sid=cookie-value")

    def test_an_unconfigured_client_authorizes_nothing(self):
        self.assertFalse(tracker.authorized_url("https://tracker.example/search.json", tracker.Endpoints()))


if __name__ == "__main__":
    unittest.main()


def _t(tid, name, filetype, free=False, author="Andy Weir"):
    return tracker.Torrent(tid=tid, name=name, authors=[author], numfiles=3, filetype=filetype,
                           raw={"free": free})


class TestFormatPreference(unittest.TestCase):
    ROW = {"title": "Project Hail Mary", "authors": ["Andy Weir"]}

    def pick(self, *torrents):
        return tracker.select_candidate(self.ROW, list(torrents))[0]

    def test_m4b_beats_mp3_when_both_qualify(self):
        mp3, m4b = _t("1", "Project Hail Mary", "mp3"), _t("2", "Project Hail Mary", "m4b")
        self.assertEqual(self.pick(mp3, m4b).tid, "2")
        self.assertEqual(self.pick(m4b, mp3).tid, "2")
        self.assertEqual(self.pick(mp3, _t("3", "Project Hail Mary", "m4a")).tid, "3")

    def test_only_mp3_is_still_chosen(self):
        self.assertEqual(self.pick(_t("1", "Project Hail Mary", "mp3")).tid, "1")

    def test_freeleech_still_wins_over_format(self):
        free_mp3 = _t("1", "Project Hail Mary", "mp3", free=True)
        paid_m4b = _t("2", "Project Hail Mary", "m4b")
        self.assertEqual(self.pick(paid_m4b, free_mp3).tid, "1")

    def test_a_knockoff_m4b_never_beats_a_real_mp3(self):
        knockoff = _t("1", "Summary of Project Hail Mary Study Guide", "m4b")
        self.assertEqual(self.pick(knockoff, _t("2", "Project Hail Mary", "mp3")).tid, "2")
        self.assertIsNone(self.pick(knockoff))

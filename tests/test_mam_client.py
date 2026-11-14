"""0.18.0-dev1: the MAM client - search parsing, candidate selection, politeness.

The network is a fake transport: no test here can reach MAM.
"""

import json
import pathlib
import unittest
import urllib.parse

from audiobind import mam

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "mam_search.json"
TORRENT = b"d8:announce30:https://t.myanonamouse.net/ann4:infod6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"


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


def client(*answers, mam_id="cookie-value"):
    clock = FakeClock()
    transport = FakeTransport(*answers, clock=clock)
    limiter = mam.RateLimiter(clock=clock, sleep=clock.sleep)
    backoff = mam.Backoff(clock=clock)
    return mam.MamClient(mam_id, transport=transport, limiter=limiter, backoff=backoff), transport, clock


class TestParseSearch(unittest.TestCase):
    def setUp(self):
        self.rows = mam.parse_search(fixture())

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

    def test_wedge_decision_reads_the_raw_entry(self):
        self.assertTrue(mam.wedge_decision(self.rows[0].raw)[0])
        self.assertFalse(mam.wedge_decision(self.rows[1].raw)[0], 'free "1"')
        self.assertFalse(mam.wedge_decision(self.rows[2].raw)[0], "fl_vip")
        self.assertFalse(mam.wedge_decision(self.rows[3].raw)[0], "personal freeleech")

    def test_garbage_payloads(self):
        for payload in (None, [], "x", {}, {"data": "nope"}, {"data": None}):
            self.assertEqual(mam.parse_search(payload), [])


class TestSelectCandidate(unittest.TestCase):
    def setUp(self):
        self.rows = mam.parse_search(fixture())

    def test_exact_match_wins_and_files_beat_an_empty_torrent(self):
        pick, why = mam.select_candidate({"title": "Project Hail Mary", "authors": ["Andy Weir"]}, self.rows)
        self.assertEqual(pick.tid, "1001", "the free one has numfiles 0, so the one with files wins")
        self.assertIn("MAM #1001", why)

    def test_near_match_passes(self):
        pick, _ = mam.select_candidate({"title": "The Way of Kings", "authors": ["Brandon Sanderson"],
                                        "series": "The Stormlight Archive", "sequence": "1"}, self.rows)
        self.assertEqual(pick.tid, "1004")
        pick, why = mam.select_candidate({"title": "Project Hail Mary: A Novel", "authors": ["Andy Weir"]},
                                         self.rows)
        self.assertEqual(pick.tid, "1001")

    def test_below_threshold_is_not_found_never_forced(self):
        pick, why = mam.select_candidate({"title": "Project Hail", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        self.assertIn("closest was", why)
        pick, why = mam.select_candidate({"title": "Artemis", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        pick, why = mam.select_candidate({"title": "Anything"}, [])
        self.assertEqual((pick, why), (None, "no results"))

    def test_author_must_match_when_the_row_has_one(self):
        pick, _ = mam.select_candidate({"title": "Project Hail Mary", "authors": ["Someone Else"]}, self.rows)
        self.assertIsNone(pick)
        pick, _ = mam.select_candidate({"title": "Project Hail Mary", "authors": []}, self.rows)
        self.assertEqual(pick.tid, "1001", "no author on the row: title alone decides")

    def test_already_snatched_is_skipped(self):
        pick, why = mam.select_candidate({"title": "The Martian", "authors": ["Andy Weir"]}, self.rows)
        self.assertIsNone(pick)
        self.assertIn("snatched", why)

    def test_not_audio_is_skipped(self):
        pick, _ = mam.select_candidate({"title": "Mistborn & The Final Empire"}, self.rows)
        self.assertIsNone(pick)

    def test_series_sequence_match_is_preferred(self):
        a = mam.Torrent(tid="1", name="Dune", authors=["Frank Herbert"], numfiles=2, filetype="mp3",
                        series=[("Dune", "2")], raw={})
        b = mam.Torrent(tid="2", name="Dune", authors=["Frank Herbert"], numfiles=2, filetype="mp3",
                        series=[("Dune", "1")], raw={})
        pick, why = mam.select_candidate({"title": "Dune", "authors": ["Frank Herbert"], "series": "Dune",
                                          "sequence": "1"}, [a, b])
        self.assertEqual(pick.tid, "2")
        self.assertIn("series match", why)

    def test_dune_never_takes_dune_messiah(self):
        t = mam.Torrent(tid="9", name="Dune Messiah", authors=["Frank Herbert"], numfiles=1, filetype="m4b")
        self.assertIsNone(mam.select_candidate({"title": "Dune", "authors": ["Frank Herbert"]}, [t])[0])

    def test_subtitle_stripping_never_shortens_both_sides(self):
        t = mam.Torrent(tid="9", name="Star Wars: Heir to the Empire", authors=["Timothy Zahn"], numfiles=1,
                        filetype="m4b")
        self.assertIsNone(mam.select_candidate({"title": "Star Wars: Thrawn", "authors": ["Timothy Zahn"]}, [t])[0])


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
            self.assertGreaterEqual(later - earlier, mam.MIN_SPACING)
        self.assertEqual(clock.sleeps, [2.0, 3.0])

    def test_no_wait_before_the_first_request(self):
        mc, _, clock = client()
        mc.search("a")
        self.assertEqual(clock.sleeps, [])

    def test_spacing_is_at_least_three_seconds(self):
        self.assertGreaterEqual(mam.MIN_SPACING, 3.0)


class TestBackoff(unittest.TestCase):
    def test_429_starts_a_backoff_that_blocks_without_a_request(self):
        mc, transport, clock = client((429, {}, b"slow down"))
        with self.assertRaises(mam.MamError) as ctx:
            mc.search("a")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(mc.backoff.remaining(), 60)
        with self.assertRaises(mam.MamBackoff):
            mc.search("b")
        with self.assertRaises(mam.MamBackoff):
            mc.download(1, False)
        self.assertEqual(len(transport.calls), 1, "nothing is sent while backing off")

    def test_doubles_to_a_thirty_minute_cap_and_resets_on_success(self):
        b = mam.Backoff(clock=FakeClock())
        self.assertEqual([b.fail() for _ in range(8)], [60, 120, 240, 480, 960, 1800, 1800, 1800])
        b.ok()
        self.assertEqual((b.remaining(), b.fail()), (0.0, 60))

    def test_any_non_200_backs_off(self):
        for status in (403, 500, 503, 302):
            mc, _, _ = client((status, {}, b""))
            with self.assertRaises(mam.MamError):
                mc.search("x")
            self.assertGreater(mc.backoff.remaining(), 0, status)

    def test_retry_after_longer_than_the_backoff_is_honoured(self):
        mc, _, _ = client((429, {"Retry-After": "900"}, b""))
        with self.assertRaises(mam.MamError):
            mc.search("x")
        self.assertEqual(mc.backoff.remaining(), 900)

    def test_unreachable_backs_off(self):
        mc, _, _ = client(TimeoutError("timed out"))
        with self.assertRaises(mam.MamError):
            mc.search("x")
        self.assertEqual(mc.backoff.remaining(), 60)

    def test_html_instead_of_json_backs_off_and_is_never_parsed(self):
        mc, _, _ = client((200, {}, b"<html><body>Please log in</body></html>"))
        with self.assertRaises(mam.MamError) as ctx:
            mc.search("x")
        self.assertIn("not answer JSON", str(ctx.exception))
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_nothing_returned_is_an_empty_result_not_an_error(self):
        mc, _, _ = client((200, {}, b'{"error": "Nothing returned, out of 0"}'))
        self.assertEqual(mc.search("x"), [])
        self.assertEqual(mc.backoff.remaining(), 0)

    def test_other_json_errors_back_off(self):
        mc, _, _ = client((200, {}, b'{"error": "You are not logged in"}'))
        with self.assertRaises(mam.MamError):
            mc.search("x")
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_restore_carries_a_backoff_across_restarts(self):
        clock = FakeClock()
        b = mam.Backoff(clock=clock)
        b.restore(clock.now + 300, failures=3)
        self.assertEqual((b.remaining(), b.failures), (300, 3))
        b2 = mam.Backoff(clock=clock)
        b2.restore(clock.now - 5)
        self.assertEqual(b2.remaining(), 0)


class TestRequests(unittest.TestCase):
    def test_search_request_shape(self):
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()))
        rows = mc.search("Project Hail Mary", "Andy Weir")
        self.assertEqual(len(rows), 5)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"))
        self.assertEqual(call["headers"]["Cookie"], "mam_id=cookie-value")
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        form = urllib.parse.parse_qs(call["data"].decode())
        self.assertEqual(form["tor[text]"], ["Project Hail Mary"])
        self.assertEqual(form["tor[author]"], ["Andy Weir"])
        self.assertEqual(form["tor[searchIn]"], ["torrents"])
        self.assertEqual(form["tor[browse_lang][]"], ["1"])
        self.assertEqual(form["tor[main_cat][]"], ["13"])
        self.assertNotIn("tor[cat][]", form)
        self.assertEqual(form["tor[sortType]"], ["relevance"])
        self.assertIn("perpage", form)

    def test_search_form_uses_main_category_13_and_no_leaf_category(self):
        mc, transport, _ = client((200, {}, FIXTURE.read_bytes()))
        mc.search("Dungeon Crawler Carl")
        pairs = urllib.parse.parse_qsl(transport.calls[0]["data"].decode())
        self.assertIn(("tor[main_cat][]", "13"), pairs)
        self.assertFalse([k for k, _ in pairs if k == "tor[cat][]"])
        self.assertEqual(len(transport.calls), 1)

    def test_download_with_and_without_the_wedge(self):
        mc, transport, _ = client((200, {}, TORRENT), (200, {}, TORRENT))
        self.assertEqual(mc.download(1001, True), TORRENT)
        self.assertEqual(mc.download("1002", False), TORRENT)
        urls = [c["url"] for c in transport.calls]
        self.assertEqual(urls, ["https://www.myanonamouse.net/tor/download.php?tid=1001&fl",
                                "https://www.myanonamouse.net/tor/download.php?tid=1002"])
        self.assertTrue(all(c["method"] == "GET" for c in transport.calls))

    def test_download_that_is_not_a_torrent_is_an_error(self):
        mc, _, _ = client((200, {}, b'{"error": "not enough wedges"}'))
        with self.assertRaises(mam.MamError):
            mc.download(1, True)
        self.assertGreater(mc.backoff.remaining(), 0)

    def test_only_the_two_authorized_endpoints(self):
        self.assertTrue(mam.authorized_url(mam.SEARCH_URL))
        self.assertTrue(mam.authorized_url(mam.download_url(5, True)))
        for url in ("https://www.myanonamouse.net/", "https://www.myanonamouse.net/tor/bookmarks.php",
                    "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php?x=1",
                    "https://www.myanonamouse.net/tor/download.php/abc?tid=1",
                    "https://evil.example/tor/download.php?tid=1"):
            self.assertFalse(mam.authorized_url(url), url)
            with self.assertRaises(mam.MamError):
                mam.urllib_transport("GET", url, None, {}, 1)

    def test_no_mam_id_no_request(self):
        mc, transport, _ = client(mam_id="")
        with self.assertRaises(mam.MamError):
            mc.search("x")
        self.assertEqual(transport.calls, [])

    def test_repr_never_shows_the_cookie(self):
        mc, _, _ = client(mam_id="SECRET-COOKIE")
        self.assertNotIn("SECRET-COOKIE", repr(mc))

    def test_redirects_are_never_followed(self):
        handler = mam._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://www.myanonamouse.net/login.php"))


if __name__ == "__main__":
    unittest.main()

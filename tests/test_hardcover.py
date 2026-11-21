"""Hardcover as an evidence-only metadata source (parity feature 4).

Retrieval against a mocked transport (found / not found / bad token / unreachable / rate limited),
the on-disk cache and its TTL, the no-token-is-off path, the comparison, the detail-panel line, the
settings plumbing (the token is a secret) and the Test connection route. No network anywhere: every
test replaces ``hardcover._transport``, and the class-level guard fails any test that reaches it
unreplaced.
"""

import inspect
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from audiobind import config, hardcover, matching, pipeline, web
from audiobind.catalog import Product
from audiobind.config import CONFIG

try:
    from test_watcher_gui_settings import WatcherSettingsCase
except ImportError:
    from tests.test_watcher_gui_settings import WatcherSettingsCase

TOKEN = "HC-SECRET-TOKEN-1234567890"

BOOK = {
    "id": 4242,
    "title": "Leviathan Wakes",
    "slug": "leviathan-wakes",
    "release_year": 2011,
    "contributions": [{"author": {"name": "James S. A. Corey"}}],
    "book_series": [{"position": 1.0, "series": {"name": "The Expanse"}}],
}
EDITION = {"id": 9, "asin": "B0055A9OHO", "release_date": "2011-06-15", "audio_seconds": 72000, "book": BOOK}


def gql(data: dict, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps({"data": data}).encode()


def errors(message: str, code: str = "", status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps({"errors": [{"message": message, "extensions": {"code": code}}]}).encode()


class FakeTransport:
    """Queued answers in order; each call's decoded body and token are recorded."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        #: set when a request arrived with nothing scripted (the client turns the raise into
        #: Unreachable, so the test case checks this flag too)
        self.unexpected = False

    def __call__(self, body: bytes, token: str, timeout: float):
        self.calls.append({"body": json.loads(body), "token": token})
        if not self.answers:
            self.unexpected = True
            raise AssertionError("unexpected Hardcover request")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def product(**overrides) -> Product:
    values = dict(asin="B0055A9OHO", title="Leviathan Wakes", authors=["James S. A. Corey"],
                  series="The Expanse", sequence="1", year="2011", hours=20.0, language="english")
    values.update(overrides)
    return Product(**values)


class HardcoverCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = pathlib.Path(tmp.name) / hardcover.CACHE_FILE_NAME
        for target, value in ((hardcover, "cache_path"), (hardcover, "MIN_GAP_SECONDS")):
            patcher = mock.patch.object(target, value, (lambda: self.cache) if value == "cache_path" else 0)
            patcher.start()
            self.addCleanup(patcher.stop)
        # any request a test did not script is a failure, never a real connection
        self.transport = FakeTransport()
        patcher = mock.patch.object(hardcover, "_transport", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertFalse(self.transport.unexpected, "an unscripted Hardcover request"))
        saved = dict(config._EFFECTIVE)
        self.addCleanup(lambda: (config._EFFECTIVE.clear(), config._EFFECTIVE.update(saved)))
        config._EFFECTIVE.pop("hardcover_evidence", None)
        config._EFFECTIVE.pop("hardcover_token", None)
        for key, value in (("hardcover_token", ""), ("hardcover_evidence", "off")):
            patcher = mock.patch.object(CONFIG, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer(self, *answers):
        self.transport.answers.extend(answers)

    def switch_on(self, token=TOKEN):
        config._EFFECTIVE["hardcover_evidence"] = "on"
        config._EFFECTIVE["hardcover_token"] = token


class TestRetrieval(HardcoverCase):
    def test_found_by_asin(self):
        self.answer(gql({"editions": [EDITION]}))
        record = hardcover.lookup("b0055a9oho", "Leviathan Wakes", "James S. A. Corey", TOKEN)
        self.assertEqual(record["found"], True)
        self.assertEqual(record["via"], "asin")
        self.assertEqual(record["title"], "Leviathan Wakes")
        self.assertEqual(record["authors"], ["James S. A. Corey"])
        self.assertEqual((record["series"], record["position"]), ("The Expanse", "1"))
        self.assertEqual((record["edition_year"], record["first_year"]), ("2011", "2011"))
        self.assertEqual(record["url"], "https://hardcover.app/books/leviathan-wakes")
        (call,) = self.transport.calls
        self.assertEqual(call["token"], TOKEN)
        self.assertEqual(call["body"]["variables"], {"asin": "B0055A9OHO"})
        self.assertIn("editions(where: {asin: {_eq: $asin}}", call["body"]["query"])

    def test_bearer_prefix_is_dropped(self):
        self.answer(gql({"editions": [EDITION]}))
        hardcover.lookup("B0055A9OHO", "", "", f"Bearer {TOKEN}")
        self.assertEqual(self.transport.calls[0]["token"], TOKEN)

    def test_asin_miss_falls_back_to_a_checked_title_search(self):
        hits = {"hits": [
            {"document": {"id": 1, "title": "Leviathan Wakes: A Study Guide", "author_names": ["Someone Else"]}},
            {"document": {"id": 4242, "title": "Leviathan Wakes", "author_names": ["James S. A. Corey"]}},
        ]}
        self.answer(gql({"editions": []}), gql({"search": {"results": hits}}), gql({"books": [BOOK]}))
        record = hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "James S. A. Corey", TOKEN)
        self.assertEqual(record["via"], "title")
        self.assertEqual(record["id"], 4242)
        self.assertEqual(self.transport.calls[2]["body"]["variables"], {"id": 4242})

    def test_not_found(self):
        hits = {"hits": [{"document": {"id": 1, "title": "Another Book", "author_names": ["James S. A. Corey"]}}]}
        self.answer(gql({"editions": []}), gql({"search": {"results": json.dumps(hits)}}))
        self.assertEqual(hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "James S. A. Corey", TOKEN),
                         {"found": False})
        self.assertEqual(len(self.transport.calls), 2, "a non-matching hit costs no book request")

    def test_bad_token_http_401(self):
        self.answer((401, b'{"error":"Unable to verify token"}'))
        with self.assertRaises(hardcover.AuthError):
            hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "", TOKEN)

    def test_bad_token_graphql_error(self):
        self.answer(errors("Could not verify JWT: JWTExpired", "invalid-jwt"))
        with self.assertRaises(hardcover.AuthError):
            hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "", TOKEN)

    def test_unreachable(self):
        for failure in (urllib.error.URLError("refused"), TimeoutError(), OSError("dns")):
            with self.subTest(failure=type(failure).__name__):
                self.answer(failure)
                with self.assertRaises(hardcover.Unreachable):
                    hardcover.lookup("B0055A9OHO", "", "", TOKEN)

    def test_server_error_is_unreachable(self):
        self.answer((503, b"down"))
        with self.assertRaises(hardcover.Unreachable):
            hardcover.lookup("B0055A9OHO", "", "", TOKEN)

    def test_rate_limited(self):
        self.answer((429, b'{"error":"Throttled"}'))
        with self.assertRaises(hardcover.RateLimited):
            hardcover.lookup("B0055A9OHO", "", "", TOKEN)

    def test_junk_answers_are_bad_answers(self):
        for answer in ((200, b"<html>"), (200, b"[1]"), (200, b'{"data": null}'), errors("field 'x' not found"),
                       gql({"editions": "nope"})):
            with self.subTest(answer=answer):
                self.answer(answer)
                with self.assertRaises(hardcover.BadAnswer):
                    hardcover.lookup("B0055A9OHO", "", "", TOKEN)

    def test_every_error_is_a_hardcover_error_and_never_cached(self):
        for cls in (hardcover.AuthError, hardcover.RateLimited, hardcover.Unreachable, hardcover.BadAnswer):
            self.assertTrue(issubclass(cls, hardcover.HardcoverError))
        self.answer((429, b""))
        with self.assertRaises(hardcover.RateLimited):
            hardcover.lookup("B0055A9OHO", "", "", TOKEN)
        self.assertFalse(self.cache.exists())
        self.answer(gql({"editions": [EDITION]}))
        self.assertTrue(hardcover.lookup("B0055A9OHO", "", "", TOKEN)["found"], "a failure is retried")

    def test_an_unsafe_slug_is_never_a_link(self):
        bad = dict(EDITION, book=dict(BOOK, slug='x" onmouseover="alert(1)'))
        self.answer(gql({"editions": [bad]}))
        self.assertEqual(hardcover.lookup("B0055A9OHO", "", "", TOKEN)["url"], "")


class TestCache(HardcoverCase):
    def test_a_found_answer_is_served_from_disk_until_its_ttl(self):
        self.answer(gql({"editions": [EDITION]}))
        first = hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "Corey", TOKEN, now=1000.0)
        self.assertTrue(self.cache.exists())
        again = hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "Corey", TOKEN,
                                 now=1000.0 + hardcover.FOUND_TTL_SECONDS)
        self.assertEqual(first, again)
        self.assertEqual(len(self.transport.calls), 1)
        self.answer(gql({"editions": [EDITION]}))
        hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "Corey", TOKEN,
                         now=1001.0 + hardcover.FOUND_TTL_SECONDS)
        self.assertEqual(len(self.transport.calls), 2, "expired: asked again")

    def test_a_miss_expires_sooner(self):
        self.answer(gql({"editions": []}))
        hardcover.lookup("B0000000XX", "", "", TOKEN, now=0.0)
        hardcover.lookup("B0000000XX", "", "", TOKEN, now=float(hardcover.MISS_TTL_SECONDS))
        self.assertEqual(len(self.transport.calls), 1)
        self.answer(gql({"editions": []}))
        hardcover.lookup("B0000000XX", "", "", TOKEN, now=hardcover.MISS_TTL_SECONDS + 1.0)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertLess(hardcover.MISS_TTL_SECONDS, hardcover.FOUND_TTL_SECONDS)

    def test_a_corrupt_cache_is_ignored_and_rewritten(self):
        self.cache.write_text("{not json", encoding="utf-8")
        self.answer(gql({"editions": [EDITION]}))
        self.assertTrue(hardcover.lookup("B0055A9OHO", "", "", TOKEN)["found"])
        self.assertIn("asin:B0055A9OHO", self.cache.read_text(encoding="utf-8"))

    def test_the_token_is_never_written_to_the_cache(self):
        self.answer(gql({"editions": [EDITION]}))
        hardcover.lookup("B0055A9OHO", "", "", TOKEN)
        self.assertNotIn(TOKEN, self.cache.read_text(encoding="utf-8"))

    def test_the_cache_is_bounded(self):
        with mock.patch.object(hardcover, "CACHE_MAX_ENTRIES", 3):
            for i in range(5):
                self.answer(gql({"editions": []}))
                hardcover.lookup(f"B00000000{i}", "", "", TOKEN, now=float(i))
        kept = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(sorted(kept), [f"asin:B00000000{i}||" for i in (2, 3, 4)])


class TestNoTokenIsOff(HardcoverCase):
    def test_default_is_off(self):
        self.assertFalse(hardcover.enabled())
        self.assertEqual(config.setting_env("hardcover_evidence"), "off")

    def test_the_toggle_without_a_token_is_off(self):
        config._EFFECTIVE["hardcover_evidence"] = "on"
        self.assertFalse(hardcover.enabled())

    def test_a_token_without_the_toggle_is_off(self):
        config._EFFECTIVE["hardcover_token"] = TOKEN
        self.assertFalse(hardcover.enabled())
        self.switch_on()
        self.assertTrue(hardcover.enabled())

    def test_off_makes_no_request_and_no_cache_file(self):
        for setup in (lambda: None, lambda: config._EFFECTIVE.update(hardcover_evidence="on"),
                      lambda: config._EFFECTIVE.update(hardcover_token=TOKEN, hardcover_evidence="off")):
            config._EFFECTIVE.pop("hardcover_evidence", None)
            config._EFFECTIVE.pop("hardcover_token", None)
            setup()
            self.assertIsNone(hardcover.evidence_for(product()))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.cache.exists())

    def test_lookup_with_no_token_refuses_before_any_request(self):
        for token in ("", "   ", "Bearer "):
            with self.assertRaises(hardcover.AuthError):
                hardcover.lookup("B0055A9OHO", "Leviathan Wakes", "", token)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.cache.exists())

    def test_env_values(self):
        self.assertEqual(config._hardcover_evidence(""), "off")
        self.assertEqual(config._hardcover_evidence("TRUE"), "on")
        self.assertEqual(config._hardcover_evidence("0"), "off")
        with self.assertRaises(RuntimeError) as caught:
            config._hardcover_evidence("maybe")
        self.assertIn("HARDCOVER_EVIDENCE", str(caught.exception))
        self.assertEqual(config._hardcover_token(f"  Bearer {TOKEN} "), TOKEN)
        self.assertEqual(config._hardcover_token("Bearer "), "")
        with self.assertRaises(RuntimeError) as caught:
            config._hardcover_token("two words")
        self.assertNotIn("two words", str(caught.exception), "never echoes the value")


class TestEvidenceAndCompare(HardcoverCase):
    def test_found_and_agreeing(self):
        self.switch_on()
        self.answer(gql({"editions": [EDITION]}))
        ev = hardcover.evidence_for(product())
        self.assertEqual(ev["status"], "found")
        self.assertEqual(ev["verdict"], "agrees")
        self.assertEqual([p["field"] for p in ev["points"]], ["series", "position", "year"])

    def test_position_disagreement(self):
        result = hardcover.compare(hardcover._record_from_book(BOOK, "title"), "Leviathan Wakes",
                                   ["James S. A. Corey"], "Expanse", "2", "2011")
        self.assertEqual(result["verdict"], "disagrees")
        self.assertEqual([p for p in result["points"] if not p["agrees"]],
                         [{"field": "position", "hardcover": "1", "catalogue": "2", "agrees": False}])

    def test_a_different_book_behind_the_asin_disagrees(self):
        other = dict(BOOK, title="Caliban's War")
        result = hardcover.compare(hardcover._record_from_book(other, "asin", EDITION), "Leviathan Wakes",
                                   ["James S. A. Corey"], "The Expanse", "1", "2011")
        self.assertEqual(result["verdict"], "disagrees")
        self.assertEqual(result["points"][0]["field"], "book")

    def test_an_earlier_first_publication_is_not_a_disagreement(self):
        old = dict(BOOK, release_year=1965, book_series=[])
        rec = hardcover._record_from_book(old, "title")
        self.assertEqual(hardcover.compare(rec, "Leviathan Wakes", ["James S. A. Corey"], "", "", "2019")["verdict"],
                         "agrees")
        late = hardcover._record_from_book(dict(old, release_year=2030), "title")
        self.assertEqual(hardcover.compare(late, "Leviathan Wakes", ["James S. A. Corey"], "", "", "2019")["verdict"],
                         "disagrees")

    def test_a_field_only_one_side_lists_is_not_compared(self):
        rec = hardcover._record_from_book(dict(BOOK, book_series=[], release_year=None), "title")
        self.assertEqual(hardcover.compare(rec, "Leviathan Wakes", ["James S. A. Corey"], "The Expanse", "1", "")
                         , {"verdict": "nothing to compare", "points": []})

    def test_errors_become_a_dict_and_never_escape(self):
        self.switch_on()
        for answer, kind in (((401, b""), "bad_token"), ((429, b""), "rate_limited"),
                             (urllib.error.URLError("x"), "unreachable"), ((200, b"junk"), "bad_answer")):
            with self.subTest(kind=kind):
                self.answer(answer)
                ev = hardcover.evidence_for(product())
                self.assertEqual((ev["status"], ev["kind"]), ("error", kind))
        with mock.patch.object(hardcover, "lookup", side_effect=KeyError("boom")):
            self.assertEqual(hardcover.evidence_for(product())["status"], "error")

    def test_not_found(self):
        self.switch_on()
        self.answer(gql({"editions": []}), gql({"search": {"results": {"hits": []}}}))
        self.assertEqual(hardcover.evidence_for(product()), {"status": "not_found"})

    def test_no_product_is_never_looked_up(self):
        self.switch_on()
        self.assertIsNone(hardcover.evidence_for(None))
        self.assertIsNone(hardcover.evidence_for(product(asin="")))
        self.assertEqual(self.transport.calls, [])


class TestEvidenceOnly(HardcoverCase):
    """The matcher never sees Hardcover: it is asked after grading, and grade/scoring cannot read it."""

    def test_matching_does_not_import_or_mention_hardcover(self):
        self.assertNotIn("hardcover", inspect.getsource(matching).lower())

    def test_it_is_asked_after_the_action_is_chosen(self):
        source = inspect.getsource(pipeline.audit_unit)
        self.assertGreater(source.index("hardcover.evidence_for"), source.index("audit.action = choose_action"))
        self.assertEqual(source.count("audit.hardcover"), 1, "written once, read nowhere in audit_unit")

    def test_record_is_byte_identical_while_off(self):
        saved = []

        class FakeState:
            def save(self, unit):
                saved.append(unit)

            def get(self, path):  # Q-B12: record() reads the previous row's series; there is none
                return None

        unit = mock.Mock(parts=[1], n_files=1, state_key="Author/Book", title_guess="Book", author_guess="A")
        audit = pipeline.Audit(unit=unit, match=matching.Match(product=product(), confidence="HIGH"))
        pipeline.record(FakeState(), audit, "review")
        self.assertNotIn("hardcover", json.loads(saved[0].detail))
        audit.hardcover = {"status": "not_found"}
        pipeline.record(FakeState(), audit, "review")
        with_hc = json.loads(saved[1].detail)
        self.assertEqual(with_hc.pop("hardcover"), {"status": "not_found"})
        self.assertEqual(json.dumps(with_hc), saved[0].detail)


class TestPanelLine(HardcoverCase):
    FOUND = {"status": "found", "record": hardcover._record_from_book(BOOK, "asin", EDITION),
             "verdict": "agrees", "points": []}

    def test_off_renders_nothing_even_for_a_stored_answer(self):
        self.assertEqual(web.hardcover_line({"hardcover": self.FOUND}), "")
        config._EFFECTIVE["hardcover_evidence"] = "on"  # no token: still off
        self.assertEqual(web.hardcover_line({"hardcover": self.FOUND}), "")

    def test_on_without_an_answer_renders_nothing(self):
        self.switch_on()
        self.assertEqual(web.hardcover_line({}), "")
        self.assertEqual(web.hardcover_line({"hardcover": "junk"}), "")

    def test_agrees(self):
        self.switch_on()
        line = web.hardcover_line({"hardcover": self.FOUND})
        self.assertIn('data-hardcover="found"', line)
        self.assertIn('href="https://hardcover.app/books/leviathan-wakes"', line)
        self.assertIn("book 1 of The Expanse, 2011 edition", line)
        self.assertIn("agrees with the catalogue", line)
        self.assertEqual(line.count("<p"), 1, "one line")

    def test_disagrees_names_the_field(self):
        self.switch_on()
        ev = dict(self.FOUND, verdict="disagrees",
                  points=[{"field": "position", "hardcover": "1", "catalogue": "2", "agrees": False}])
        line = web.hardcover_line({"hardcover": ev})
        self.assertIn("<b>disagrees with the catalogue</b>: series position 1, catalogue says 2", line)

    def test_not_found_and_error(self):
        self.switch_on()
        self.assertIn("no record of this book", web.hardcover_line({"hardcover": {"status": "not_found"}}))
        line = web.hardcover_line({"hardcover": {"status": "error", "kind": "bad_token", "message": "<refused>"}})
        self.assertIn("could not be asked: &lt;refused&gt;", line)

    def test_stored_text_is_escaped(self):
        self.switch_on()
        rec = dict(self.FOUND["record"], title="<script>x</script>", series="<i>s</i>",
                   url="javascript:alert(1)")
        line = web.hardcover_line({"hardcover": dict(self.FOUND, record=rec)})
        self.assertNotIn("<script>", line)
        self.assertNotIn("<i>s</i>", line)
        self.assertNotIn("javascript:", line)


class TestSettings(WatcherSettingsCase):
    def setUp(self):
        super().setUp()
        for key, value in (("hardcover_token", ""), ("hardcover_evidence", "off")):
            patcher = mock.patch.object(CONFIG, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transport = FakeTransport()
        for target, value in ((hardcover, "_transport"), (hardcover, "MIN_GAP_SECONDS")):
            patcher = mock.patch.object(target, value, self.transport if value == "_transport" else 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plumbing(self):
        self.assertIn("hardcover_token", config.SECRET_SETTING_KEYS)
        for key in ("hardcover_token", "hardcover_evidence"):
            self.assertIn(key, config.SETTING_KEYS)
            self.assertNotIn(key, config.GENERAL_SETTING_KEYS, "never wiped by Reset to defaults")
        self.assertEqual(config.SETTING_CHOICES["hardcover_evidence"], ("off", "on"))

    def test_settings_library_has_the_block(self):
        page = self.page()
        library = page[page.index('id="ab-tab-library"'):page.index('id="ab-tab-watcher"')]
        self.assertIn('<div class="ab-acct-title">Hardcover evidence</div>', library)
        self.assertIn('<option value="off" selected>Off</option>', self.field("hardcover_evidence", page))
        self.assertIn('placeholder="not set"', self.field("hardcover_token", page))
        self.assertIn('id="ab-hc-test"', library)

    def test_the_token_never_reaches_a_browser(self):
        status, body, raw = self.save({"settings": {"hardcover_token": f"Bearer {TOKEN}", "hardcover_evidence": "on"}})
        self.assertEqual(status, 200, body)
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(config.setting_effective("hardcover_token"), TOKEN)
        self.assertTrue(hardcover.enabled())
        page = self.page()
        self.assertNotIn(TOKEN, page)
        self.assertIn('placeholder="set - leave blank to keep it"', self.field("hardcover_token", page))
        self.assertNotIn(TOKEN, json.dumps(web.public_settings(dict(config._EFFECTIVE))))

    def test_a_bad_token_is_refused_without_echo_and_clear_works(self):
        status, body, _ = self.save({"settings": {"hardcover_token": "has a space"}})
        self.assertEqual(status, 400)
        self.assertIn("Hardcover token", body["error"])
        self.assertNotIn("has a space", body["error"])
        self.assertEqual(self.save({"settings": {"hardcover_token": TOKEN}})[0], 200)
        self.assertEqual(self.save({"clear": ["hardcover_token"]})[0], 200)
        self.assertEqual(config.setting_effective("hardcover_token"), "")

    def test_a_bad_toggle_value_is_refused(self):
        status, body, _ = self.save({"settings": {"hardcover_evidence": "maybe"}})
        self.assertEqual(status, 400)
        self.assertIn("hardcover_evidence: must be one of off, on", body["error"])

    def test_test_connection(self):
        status, _h, body = self.call("/api/hardcover/test", {})
        self.assertEqual(status, 502)
        self.assertIn("No Hardcover token", json.loads(body)["message"])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.save({"settings": {"hardcover_token": TOKEN}})[0], 200)
        self.transport.answers.append(gql({"me": [{"username": "reader"}]}))
        status, _h, body = self.call("/api/hardcover/test", {})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "message": "Connected to Hardcover as reader"}))
        self.transport.answers.append((401, b""))
        status, _h, body = self.call("/api/hardcover/test", {})
        self.assertEqual(status, 502)
        self.assertNotIn(TOKEN, body)

    def test_test_connection_is_post_only(self):
        status, headers, _ = self.call("/api/hardcover/test", method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")


if __name__ == "__main__":
    unittest.main()

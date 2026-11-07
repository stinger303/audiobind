"""Catalog lookahead: one unit, thread-safe against State, and harmless when it fails."""
import pathlib
import tempfile
import threading
import time
import unittest

from audiobind import catalog, matching, prefetch
from audiobind.catalog import Product
from audiobind.prefetch import Prefetcher, next_eligible
from audiobind.state import State, UnitState
from audiobind.units import BookUnit


def unit(rel="Author/Book"):
    return BookUnit(path=f"/library/{rel}", name=rel.split("/")[-1],
                    parent=rel.split("/")[0], parts=[], rel_path=rel)


class PrefetchBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = State(pathlib.Path(self.dir.name))
        self._match_unit = matching.match_unit
        self._chapters = catalog.chapters
        self._book = catalog.book
        self.calls = []

    def tearDown(self):
        matching.match_unit = self._match_unit
        catalog.chapters = self._chapters
        catalog.book = self._book
        self.state.close()
        self.dir.cleanup()

    def stub_catalog(self, confidence="HIGH", explode=False, delay=0.0):
        def _match(u, asin_override=""):
            self.calls.append(("match", u.rel_path, asin_override))
            if delay:
                time.sleep(delay)
            if explode:
                raise RuntimeError("audible is down")
            match = matching.Match(product=Product(asin="B00TEST0001", title="T"),
                                   confidence=confidence)
            return match
        matching.match_unit = _match
        catalog.chapters = lambda asin: self.calls.append(("chapters", asin)) or []
        catalog.book = lambda asin: self.calls.append(("book", asin)) or {}


class TestNextEligible(PrefetchBase):
    def test_picks_the_following_unit(self):
        units = [unit("a"), unit("b"), unit("c")]
        self.assertEqual(next_eligible(self.state, units, 0).rel_path, "b")

    def test_skips_units_already_decided(self):
        units = [unit("a"), unit("b"), unit("c")]
        self.state.save(UnitState(path="b", status="converted"))
        self.assertEqual(next_eligible(self.state, units, 0).rel_path, "c")

    def test_none_at_the_end_of_the_queue(self):
        self.assertIsNone(next_eligible(self.state, [unit("a")], 0))

    def test_force_takes_the_next_one_regardless(self):
        units = [unit("a"), unit("b")]
        self.state.save(UnitState(path="b", status="converted"))
        self.assertEqual(next_eligible(self.state, units, 0, force=True).rel_path, "b")

    def test_a_broken_state_read_is_not_fatal(self):
        class Broken:
            def get(self, path):
                raise RuntimeError("db gone")
        self.assertIsNone(next_eligible(Broken(), [unit("a"), unit("b")], 0))


class TestPrefetcher(PrefetchBase):
    def test_it_fetches_the_match_and_the_chapters(self):
        self.stub_catalog()
        fetcher = Prefetcher()
        self.assertTrue(fetcher.schedule(self.state, unit("a")))
        fetcher.join(5)
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds, ["match", "chapters", "book"])
        self.assertEqual(fetcher.completed, 1)
        self.assertEqual(fetcher.failed, 0)

    def test_a_weak_match_does_not_fetch_chapters(self):
        self.stub_catalog(confidence="LOW")
        fetcher = Prefetcher()
        fetcher.schedule(self.state, unit("a"))
        fetcher.join(5)
        self.assertEqual([c[0] for c in self.calls], ["match"])

    def test_nothing_to_prefetch_is_not_an_error(self):
        fetcher = Prefetcher()
        self.assertFalse(fetcher.schedule(self.state, None))
        self.assertEqual(fetcher.started, 0)

    def test_only_one_lookahead_runs_at_a_time(self):
        self.stub_catalog(delay=0.4)
        fetcher = Prefetcher()
        self.assertTrue(fetcher.schedule(self.state, unit("a")))
        self.assertFalse(fetcher.schedule(self.state, unit("b")))
        self.assertFalse(fetcher.schedule(self.state, unit("c")))
        self.assertEqual(fetcher.skipped, 2)
        fetcher.join(5)
        self.assertEqual([c[1] for c in self.calls if c[0] == "match"], ["a"])

    def test_a_failure_is_swallowed_and_counted(self):
        self.stub_catalog(explode=True)
        fetcher = Prefetcher()
        fetcher.schedule(self.state, unit("a"))
        fetcher.join(5)
        self.assertEqual(fetcher.failed, 1)
        self.assertEqual(fetcher.completed, 0)
        self.assertFalse(fetcher.busy)
        # and the next one still runs
        self.stub_catalog()
        fetcher.schedule(self.state, unit("b"))
        fetcher.join(5)
        self.assertEqual(fetcher.completed, 1)

    def test_it_reads_the_asin_override_through_state(self):
        self.stub_catalog()
        self.state.set_asin_override("a", "B00OVERRIDE")
        fetcher = Prefetcher()
        fetcher.schedule(self.state, unit("a"))
        fetcher.join(5)
        self.assertEqual(self.calls[0], ("match", "a", "B00OVERRIDE"))

    def test_the_prefetch_never_writes_to_state(self):
        self.stub_catalog()
        self.state.save(UnitState(path="a", status="ready", title="original"))
        before = self.state.get("a")
        fetcher = Prefetcher()
        fetcher.schedule(self.state, unit("a"))
        fetcher.join(5)
        after = self.state.get("a")
        self.assertEqual((before.status, before.title, before.updated_at),
                         (after.status, after.title, after.updated_at))


class TestPrefetchThreadSafety(PrefetchBase):
    """A lookahead runs while the daemon and the web handlers are hammering the same State."""

    def test_state_survives_a_prefetch_plus_many_writers(self):
        errors = []

        def prefetch_worker():
            fetcher = Prefetcher()
            try:
                for i in range(15):
                    fetcher.schedule(self.state, unit(f"look/{i}"))
                    fetcher.join(5)
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        def writer(n):
            try:
                for i in range(40):
                    self.state.save(UnitState(path=f"w{n}/book{i}", status="ready",
                                              title=f"book{i}", hours=1.5))
                    self.state.counts()
                    self.state.set_meta("paused", False)
                    self.state.get_overrides(f"w{n}/book{i}")
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(40):
                    self.state.all()
                    self.state.last_run()
                    self.state.heartbeat_age()
                    self.state.get_asin_override("w0/book0")
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        # the prefetch reads State from its own thread the whole time
        self.stub_catalog(delay=0.002)
        threads = ([threading.Thread(target=writer, args=(n,)) for n in range(4)]
                   + [threading.Thread(target=reader) for _ in range(2)]
                   + [threading.Thread(target=prefetch_worker)])
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(errors, [], f"concurrent access failed: {errors}")
        self.assertEqual(self.state.counts().get("ready"), 160)

    def test_scheduling_from_many_threads_still_runs_one_at_a_time(self):
        """The invariant is "never two lookaheads at once", whatever the thread timing does."""
        inside = []
        peak = []
        guard = threading.Lock()

        def _match(u, asin_override=""):
            with guard:
                inside.append(u.rel_path)
                peak.append(len(inside))
            time.sleep(0.05)
            with guard:
                inside.remove(u.rel_path)
            return matching.Match(product=Product(asin="B00TEST0001", title="T"),
                                  confidence="LOW")
        matching.match_unit = _match

        fetcher = Prefetcher()

        def racer(n):
            fetcher.schedule(self.state, unit(f"race/{n}"))
            fetcher.join(10)

        threads = [threading.Thread(target=racer, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        fetcher.join(10)
        self.assertTrue(peak, "no lookahead ran at all")
        self.assertEqual(max(peak), 1, f"{max(peak)} lookaheads ran at once")
        self.assertEqual(fetcher.started, len(peak))


class TestCatalogCache(unittest.TestCase):
    def setUp(self):
        catalog.cache_clear()

    def tearDown(self):
        catalog.cache_clear()

    def test_a_cached_payload_is_returned_without_a_request(self):
        catalog.cache_put("http://x/1", {"asin": "B1"})
        self.assertEqual(catalog.cache_get("http://x/1"), {"asin": "B1"})

    def test_an_unknown_url_is_a_miss(self):
        self.assertIsNone(catalog.cache_get("http://x/never"))

    def test_entries_expire(self):
        catalog.cache_put("http://x/2", {"a": 1})
        original = catalog.CACHE_TTL_SECONDS
        try:
            catalog.CACHE_TTL_SECONDS = -1
            self.assertIsNone(catalog.cache_get("http://x/2"))
        finally:
            catalog.CACHE_TTL_SECONDS = original

    def test_the_cache_is_bounded(self):
        for i in range(catalog.CACHE_MAX_ENTRIES + 20):
            catalog.cache_put(f"http://x/{i}", {"i": i})
        self.assertLessEqual(len(catalog._cache), catalog.CACHE_MAX_ENTRIES)

    def test_concurrent_cache_access_is_safe(self):
        errors = []

        def worker(n):
            try:
                for i in range(100):
                    catalog.cache_put(f"http://x/{n}/{i}", {"i": i})
                    catalog.cache_get(f"http://x/{n}/{i}")
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])

    def test_the_prefetched_payload_is_what_the_pipeline_then_reads(self):
        """The whole point: the main loop must not wait on the network a second time."""
        requests = []
        original = catalog._get_json

        def counting_get(url, **kw):
            requests.append(url)
            payload = {"asin": "B00TEST0001", "title": "T"}
            catalog.cache_put(url, payload)
            return payload
        try:
            catalog._get_json = counting_get
            first = catalog.book("B00TEST0001")          # the prefetch thread
            catalog._get_json = original                 # the network is now "gone"
            second = catalog.book("B00TEST0001")         # the main loop
            self.assertEqual(first, second)
            self.assertEqual(len(requests), 1)
        finally:
            catalog._get_json = original


class TestDaemonSchedulesALookahead(PrefetchBase):
    def test_scan_once_schedules_the_next_unit(self):
        from audiobind import daemon

        self.stub_catalog()
        processed = []
        original_process = daemon.process_unit
        original_discover = daemon.discover_units
        units = [unit("one"), unit("two")]
        try:
            daemon.discover_units = lambda root, globs: units
            def fake_process(state, unit, log=print, force=False):
                processed.append(unit.rel_path)
                daemon.PREFETCH.join(5)                  # the lookahead runs during "the encode"
                state.save(UnitState(path=unit.rel_path, status="converted"))
            daemon.process_unit = fake_process
            daemon.scan_once(self.state)
        finally:
            daemon.process_unit = original_process
            daemon.discover_units = original_discover

        self.assertEqual(processed, ["one", "two"])
        prefetched = [c[1] for c in self.calls if c[0] == "match"]
        self.assertIn("two", prefetched, "the next book was never looked ahead")


if __name__ == "__main__":
    unittest.main()

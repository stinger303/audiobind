"""Look ahead one book: do the next unit's catalog work while the current one encodes.

An encode is minutes of CPU; the catalog search and the chapter fetch that follow it are seconds
of waiting on someone else's API. Doing the next book's lookups in a background thread while
ffmpeg is busy removes that wait from the main loop entirely.

Three rules this module exists to keep:

* **One lookahead unit.** Never a queue of threads - exactly one in flight, and a second request
  while it runs is dropped.
* **It cannot hurt the encode.** Everything the thread does is wrapped: a failure is logged and
  forgotten, the normal path simply fetches again.
* **It cannot corrupt the state.** The thread only *reads* State, through the same RLock as
  everyone else, and writes nothing. The prefetched payload lands in the catalog's URL cache,
  which is where the pipeline already looks, so the main loop picks it up by itself.
"""
from __future__ import annotations

import logging
import threading

from . import catalog, matching
from .units import BookUnit

log = logging.getLogger("audiobind")

#: statuses that mean "already decided" - the daemon skips these, so prefetching them is waste
DECIDED = ("converted", "held", "failed", "review")


def next_eligible(state, units: list[BookUnit], after_index: int, force: bool = False):
    """The next unit the daemon would actually work on, or None."""
    for unit in units[after_index + 1:]:
        if force:
            return unit
        try:
            previous = state.get(unit.rel_path)
        except Exception:                               # noqa: BLE001 - never break the caller
            return None
        if previous and previous.status in DECIDED:
            continue
        return unit
    return None


class Prefetcher:
    """Runs at most one catalog lookahead at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: str = ""
        #: counters, for the tests and for anyone reading the logs
        self.started = 0
        self.completed = 0
        self.failed = 0
        self.skipped = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    def schedule(self, state, unit: BookUnit | None) -> bool:
        """Start a lookahead for ``unit``. False when one is already running or there is nothing."""
        if unit is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self.skipped += 1
                return False
            self._current = unit.rel_path
            self.started += 1
            thread = threading.Thread(target=self._run, args=(state, unit),
                                      name="prefetch", daemon=True)
            self._thread = thread
        thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        """Wait for the in-flight lookahead. Only the tests and a clean shutdown need this."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, state, unit: BookUnit) -> None:
        try:
            self._prefetch(state, unit)
            self.completed += 1
            log.debug("prefetched catalog data for %s", unit.rel_path)
        except Exception as exc:                        # noqa: BLE001 - must never reach the encode
            self.failed += 1
            log.debug("prefetch for %s failed, the normal path will retry: %s",
                      unit.rel_path, exc)
        finally:
            with self._lock:
                self._current = ""

    @staticmethod
    def _prefetch(state, unit: BookUnit) -> None:
        """The actual lookups. Read-only against State; results land in the catalog cache."""
        asin_override = state.get_asin_override(unit.rel_path)
        match = matching.match_unit(unit, asin_override)
        if not match.product:
            return
        # the audit only fetches chapters for a tier it would convert, so mirror that
        if match.confidence in ("HIGH", "MEDIUM"):
            catalog.chapters(match.asin)
            catalog.book(match.asin)


#: the daemon's single lookahead
PREFETCH = Prefetcher()

"""The long-running mode: scan on a timer, convert what we are sure about."""

from __future__ import annotations

import logging
import os
import time

from . import ingest, web
from . import series as series_mod
from . import watcher as watcher_mod
from .config import CONFIG, effective_val, set_effective
from .control import CONTROL
from .pipeline import process_unit
from .prefetch import PREFETCH, already_decided, next_eligible
from .state import SOURCE_INGEST, State
from .units import BookUnit, discover_units, is_single_file

log = logging.getLogger("audiobind")


def _ingest_source_gone(unit: BookUnit) -> bool:
    """A torrent removed (or being removed) since discovery: nothing to read any more.

    The unit's path is the torrent folder, or the file itself for a single-file torrent.
    """
    try:
        if is_single_file(unit):
            return not os.path.isfile(unit.path)
        return not os.path.isdir(unit.path) or not all(os.path.isfile(f) for f in unit.files)
    except OSError:
        return True


def scan_once(state: State, force: bool = False) -> dict:
    """One pass over the library (and the ingest folder, when INGEST_DIR is set).

    Already-decided units are skipped unless forced."""
    set_effective(state)
    run_id = state.start_run()
    state.heartbeat()  # "last scan" should read fresh the moment a pass begins
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    log.info("scan: %d unit(s) on disk", len(units))
    # ingest units (empty when INGEST_DIR is unset) go through the very same loop below
    ingest_units = ingest.discover()
    if ingest_units:
        log.info("ingest: %d unit(s) in %s", len(ingest_units), ingest.ingest_root())
        units = units + ingest_units
    CONTROL.begin_scan(len(units))
    try:
        seen = converted = held = failed = 0
        for index, unit in enumerate(units):
            if CONTROL.paused:
                log.info("paused - stopping before the next book")
                break
            previous = state.get(unit.state_key)
            if previous and not force and already_decided(previous):
                CONTROL.scan_tick(index + 1)
                continue
            if unit.source == SOURCE_INGEST and _ingest_source_gone(unit):
                # the torrent was removed after this scan listed it: its row (if any) is kept
                # as it was, and nothing is recorded for files that are no longer there
                log.info("[%s] gone from the ingest folder - skipped", unit.state_key)
                CONTROL.scan_tick(index + 1)
                continue
            seen += 1
            log.info("[%s]", unit.state_key)
            before = previous.status if previous else ""
            # look the next book up while this one encodes; failure here changes nothing
            PREFETCH.schedule(state, next_eligible(state, units, index, force))
            if unit.source == SOURCE_INGEST:
                try:
                    process_unit(state, unit, log=log.info)
                except OSError as exc:
                    # a seeding torrent can be removed while its files are being read; that
                    # is one skipped book, never a failed scan
                    log.warning("[%s] ingest source vanished while being checked: %s", unit.state_key, exc)
                    CONTROL.scan_tick(index + 1)
                    continue
            else:
                process_unit(state, unit, log=log.info)
            CONTROL.scan_tick(index + 1)
            current = state.get(unit.state_key)
            status = current.status if current else ""
            if status in ("converted", "ready") and before != status:
                converted += 1
            elif status == "held":
                held += 1
            elif status == "failed":
                failed += 1
    finally:
        CONTROL.end_scan()

    state.finish_run(run_id, seen, converted, held, failed)
    state.heartbeat()
    log.info("scan done: %d new, %d converted, %d held, %d failed", seen, converted, held, failed)
    resolve_wishlist(state)
    return {"seen": seen, "converted": converted, "held": held, "failed": failed}


def resolve_wishlist(state: State) -> None:
    """Wishlist rows whose book is now converted become "in_library" (local only, idempotent;
    runs whether or not the MAM watcher is on - a book can arrive by a manual bookmark too)."""
    try:
        watcher_mod.resolve_library(state)
    except Exception:  # noqa: BLE001 - the wishlist must never fail a scan
        log.exception("wishlist: library match failed")


def refresh_series(state: State) -> None:
    """Queue stale series listings for the background refresher. Only the daemon loop (and
    an explicit POST) does this - page and API GETs read the cache and never go outbound."""
    try:
        series_mod.aggregate(state, refresh=True)
    except Exception:  # noqa: BLE001 - the shelf must never stop the loop
        log.exception("series refresh could not be scheduled")


def run() -> None:
    """Loop forever, honouring SCAN_INTERVAL_MIN."""
    state = State(CONFIG.state_dir)
    CONFIG.ensure_dirs()
    if state.get_meta("paused", False):
        CONTROL.pause()  # a paused container stays paused across restarts
    if state.get_meta("dry_run") is not None:
        CONTROL.set_dry_run(bool(state.get_meta("dry_run")))
    log.info(
        "audiobind daemon: %s -> %s every %d min",
        CONFIG.library_root,
        CONFIG.output_root,
        CONFIG.scan_interval_min,
    )
    set_effective(state)
    # a set-but-missing INGEST_DIR is a warning, not a crash: the library keeps converting
    ingest.check_at_start()
    if CONFIG.web_enabled:
        web.serve(state)
        log.info("status page on http://0.0.0.0:%d", CONFIG.web_port)
    # the wishlist watcher: starts its own thread only when MAM_ID is set and qBittorrent
    # credentials exist; otherwise one log line and nothing else (no thread, no network)
    watcher_mod.WATCHER.start(state)

    set_effective(state)
    while True:
        if CONTROL.paused:
            CONTROL.clear_scan()  # a click while paused must not busy-loop
            refresh_series(state)  # a no-op unless a listing is a day old
            CONTROL.wait(60)
            set_effective(state)
            continue

        force = CONTROL.scan_force  # a Re-check all click re-audits decided books
        CONTROL.clear_scan()  # anything clicked during a scan triggers another
        try:
            scan_once(state, force=force)
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("scan failed")
        refresh_series(state)

        # Sleep in 15-second chunks so a lowered interval takes effect within ~15s.
        set_effective(state)
        interval_s = max(60, effective_val("scan_interval_min") * 60)
        deadline = time.time() + interval_s
        while time.time() < deadline and not CONTROL.paused and not CONTROL.scan_requested():
            chunk = min(15, deadline - time.time())
            if chunk <= 0:
                break
            if CONTROL.wait(chunk):
                log.info("scan requested from the status page")
                break

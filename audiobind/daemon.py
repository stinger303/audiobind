"""The long-running mode: scan on a timer, convert what we are sure about."""

from __future__ import annotations

import dataclasses
import logging
import os
import time

from . import ingest, web
from . import series as series_mod
from . import watcher as watcher_mod
from .config import CONFIG, deprecated_env_lines, effective_val, rejected_env_lines, set_effective
from .control import CONTROL
from .pipeline import process_unit, recover_publications
from .prefetch import PREFETCH, already_decided, next_eligible
from .state import SOURCE_INGEST, State, UnitState
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


def source_side_error(unit: BookUnit, exc: OSError) -> bool:
    """True only when ``exc`` names a path of this book's source: its folder (or loose file), or
    anything inside it.

    Decided from the paths the error carries, never from its message. An error with no path, or
    one naming the output tree, the work dir or a binary, is not about the source: the caller must
    not record it as a source failure, because that would rewrite the row's status on a guess.
    """
    roots = {os.path.normpath(unit.path), *(os.path.normpath(f) for f in unit.files)}
    for name in (exc.filename, exc.filename2):
        if name is None or isinstance(name, int):
            continue
        try:
            path = os.path.normpath(os.fsdecode(name))
        except (TypeError, ValueError):
            continue
        for root in roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                return True
    return False


def _record_unreadable(state: State, unit: BookUnit, exc: OSError) -> None:
    """Record a library book whose files could not be read as failed, with the reason.

    Whatever its row already knew (title, published output, match evidence) is kept: only the
    status and note change, so a converted book whose source went away still lists its output.
    """
    note = f"a source file could not be read: {exc}"[:300]
    previous = state.get(unit.state_key)
    if previous is not None:
        row = dataclasses.replace(previous, status="failed", note=note)
    else:
        row = UnitState(path=unit.state_key, status="failed", title=unit.title_guess,
                        author=unit.author_guess, files=unit.n_files, note=note)
    state.save(row)


def prune_dismissals(state: State) -> int:
    """Forget dismissals whose ingest folder (or single file) is gone; returns how many.

    Only while the ingest root is configured, enabled and reachable this run: an unmounted or
    disabled ingest folder would otherwise look like every torrent was removed, and one
    transient outage would bring every dismissed book back.
    """
    root = ingest.ingest_root()
    if root is None:
        return 0
    try:
        if not root.is_dir():
            return 0
        pruned = 0
        for key in sorted(state.dismissed_keys()):
            if not ingest.is_ingest(key):
                continue
            rel = ingest.rel_of(key)
            # "Torrent#CD1"-style keys live in "Torrent"; a folder may also really contain a "#"
            if rel and not any(os.path.lexists(root / p) for p in {rel, rel.split("#", 1)[0]} if p):
                state.undismiss(key)
                pruned += 1
                log.info("ingest: %s is gone from the ingest folder - dismissal forgotten", key)
        return pruned
    except OSError as exc:
        log.warning("ingest: could not check dismissed books in %s (%s) - kept them all", root, exc)
        return 0


def skip_dismissed(state: State, units: list[BookUnit]) -> list[BookUnit]:
    """Ingest units the operator removed from Audiobind stay out of the scan (the torrent seeds on)."""
    dismissed = state.dismissed_keys()
    if not dismissed:
        return units
    kept = []
    for unit in units:
        if unit.source == SOURCE_INGEST and unit.state_key in dismissed:
            log.debug("[%s] removed from Audiobind - skipped", unit.state_key)
            continue
        kept.append(unit)
    if len(kept) != len(units):
        log.info("ingest: %d unit(s) removed from Audiobind - skipped", len(units) - len(kept))
    return kept


def scan_once(state: State, force: bool = False) -> dict:
    """One pass over the library (and the ingest folder, when INGEST_DIR is set).

    Already-decided units are skipped unless forced."""
    recover_publications(state)
    set_effective(state)
    run_id = state.start_run()
    state.heartbeat()  # "last scan" should read fresh the moment a pass begins
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    log.info("scan: %d unit(s) on disk", len(units))
    # ingest units (empty when INGEST_DIR is unset) go through the very same loop below
    ingest_units = ingest.discover()
    prune_dismissals(state)
    # dismissed before anything reads them: never checked, prefetched or converted again
    ingest_units = skip_dismissed(state, ingest_units)
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
                try:
                    process_unit(state, unit, log=log.info)
                except OSError as exc:
                    if not source_side_error(unit, exc):
                        # C1: not about this book's source (the output mount, the work dir, an
                        # error with no path): end the pass as before B1 and rewrite no row
                        log.exception("[%s] scan stopped - an error not about this book's source",
                                      unit.state_key)
                        raise
                    # B1: a library file deleted, replaced or unreadable between the scan and the read
                    # (a mount that dropped, say) fails this one book; the rest of the pass goes on
                    log.warning("[%s] FAILED - a source file could not be read: %s", unit.state_key, exc)
                    if CONTROL.begin_audit(unit.state_key):
                        try:
                            if previous is None or state.get(unit.state_key) is not None:
                                _record_unreadable(state, unit, exc)
                        finally:
                            CONTROL.end_audit(unit.state_key)
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
    runs whether or not the watcher is on - a book can arrive by a manual bookmark too)."""
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


def restore_controls(state: State) -> None:
    """Restore pause and the two dry-run switches at startup. Each is independent: DRY_RUN
    (library pipeline) and WATCH_DRY_RUN (acquisition watcher) are floors for their own switch only:
    true in the environment starts it on whatever was saved (the UI can turn it off after)."""
    if state.get_meta("paused", False):
        CONTROL.pause()  # a paused container stays paused across restarts
    saved = state.get_meta("dry_run")
    if CONFIG.dry_run:
        CONTROL.set_dry_run(True)
        log.info("dry run: on (DRY_RUN=true in the environment)")
    elif saved is not None:
        CONTROL.set_dry_run(bool(saved))
        log.info("dry run: %s (saved switch)", "on" if saved else "off")
    else:
        log.info("dry run: off (saved switch)")
    saved = state.get_meta("watcher_dry_run")
    if CONFIG.watch_dry_run:
        CONTROL.set_watcher_dry_run(True)
        log.info("watcher dry run: on (WATCH_DRY_RUN=true in the environment)")
    elif saved is not None:
        CONTROL.set_watcher_dry_run(bool(saved))
        log.info("watcher dry run: %s (saved switch)", "on" if saved else "off")
    else:
        log.info("watcher dry run: off (saved switch)")


def paused_tick(state: State, wait_s: float = 60) -> None:
    """One idle turn of the loop while paused."""
    CONTROL.clear_scan()  # a click while paused must not busy-loop
    state.heartbeat()  # paused is alive, not stalled: the healthcheck must stay green
    refresh_series(state)  # a no-op unless a listing is a day old
    CONTROL.wait(wait_s)
    set_effective(state)


def run() -> None:
    """Loop forever, honouring SCAN_INTERVAL_MIN."""
    state = State(CONFIG.state_dir)
    CONFIG.ensure_dirs()
    # WEBAPI-02c: a Delete interrupted between its renames and the removal is undone first, so the
    # first scan finds every book where its row says it is
    with state.layout_writer(blocking=True):
        web.recover_deletes(web.delete_journal(state))
        recover_publications(state)
    restore_controls(state)
    log.info(
        "audiobind daemon: %s -> %s every %d min",
        CONFIG.library_root,
        CONFIG.output_root,
        CONFIG.scan_interval_min,
    )
    for line in deprecated_env_lines():  # one line per renamed variable still in use; never a value
        log.warning(line)
    for line in rejected_env_lines():  # SEC-05: a client URL variable that was not used; never its value
        log.warning(line)
    set_effective(state)
    # a set-but-missing INGEST_DIR is a warning, not a crash: the library keeps converting
    ingest.check_at_start()
    if CONFIG.web_enabled:
        web.serve(state)
        log.info("status page on http://0.0.0.0:%d", CONFIG.web_port)
    # the wishlist watcher: starts its own thread only when the tracker settings are complete and qBittorrent
    # credentials exist; otherwise one log line and nothing else (no thread, no network). From here on a
    # Settings page save that completes the configuration starts it too, with no restart.
    watcher_mod.WATCHER.autostart = True
    watcher_mod.WATCHER.start(state)

    set_effective(state)
    while True:
        if CONTROL.paused:
            paused_tick(state)
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

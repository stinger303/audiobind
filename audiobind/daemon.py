"""The long-running mode: scan on a timer, convert what we are sure about."""
from __future__ import annotations

import logging
import time

from . import web
from .config import CONFIG, set_effective, effective_val
from .control import CONTROL
from .pipeline import process_unit
from .prefetch import PREFETCH, next_eligible
from .state import State
from .units import discover_units

log = logging.getLogger("audiobind")


def scan_once(state: State, force: bool = False) -> dict:
    """One pass over the library. Already-decided units are skipped unless forced."""
    set_effective(state)
    run_id = state.start_run()
    state.heartbeat()          # "last scan" should read fresh the moment a pass begins
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    log.info("scan: %d unit(s) on disk", len(units))
    CONTROL.begin_scan(len(units))
    try:
        seen = converted = held = failed = 0
        for index, unit in enumerate(units):
            if CONTROL.paused:
                log.info("paused - stopping before the next book")
                break
            previous = state.get(unit.rel_path)
            if previous and previous.status in ("converted", "held", "failed", "review") and not force:
                CONTROL.scan_tick(index + 1)
                continue
            seen += 1
            log.info("[%s]", unit.rel_path)
            before = previous.status if previous else ""
            # look the next book up while this one encodes; failure here changes nothing
            PREFETCH.schedule(state, next_eligible(state, units, index, force))
            process_unit(state, unit, log=log.info)
            CONTROL.scan_tick(index + 1)
            current = state.get(unit.rel_path)
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
    return {"seen": seen, "converted": converted, "held": held, "failed": failed}


def run() -> None:
    """Loop forever, honouring SCAN_INTERVAL_MIN."""
    state = State(CONFIG.state_dir)
    CONFIG.ensure_dirs()
    if state.get_meta("paused", False):
        CONTROL.pause()                     # a paused container stays paused across restarts
    if state.get_meta("dry_run") is not None:
        CONTROL.set_dry_run(bool(state.get_meta("dry_run")))
    log.info("audiobind daemon: %s -> %s every %d min",
             CONFIG.library_root, CONFIG.output_root, CONFIG.scan_interval_min)
    if CONFIG.web_enabled:
        web.serve(state)
        log.info("status page on http://0.0.0.0:%d", CONFIG.web_port)

    set_effective(state)
    while True:
        if CONTROL.paused:
            CONTROL.clear_scan()            # a click while paused must not busy-loop
            CONTROL.wait(60)
            set_effective(state)
            continue

        force = CONTROL.scan_force          # a Re-check all click re-audits decided books
        CONTROL.clear_scan()                # anything clicked during a scan triggers another
        try:
            scan_once(state, force=force)
        except Exception:                   # noqa: BLE001 - never let the loop die
            log.exception("scan failed")

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

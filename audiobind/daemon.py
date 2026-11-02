"""The long-running mode: scan on a timer, convert what we are sure about."""
from __future__ import annotations

import logging
import time

from . import web
from .config import CONFIG
from .pipeline import process_unit
from .state import State
from .units import discover_units

log = logging.getLogger("audiobind")


def scan_once(state: State, force: bool = False) -> dict:
    """One pass over the library. Already-decided units are skipped unless forced."""
    run_id = state.start_run()
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    log.info("scan: %d unit(s) on disk", len(units))

    seen = converted = held = failed = 0
    for unit in units:
        previous = state.get(unit.rel_path)
        if previous and previous.status in ("converted", "held", "failed", "review") and not force:
            continue
        seen += 1
        log.info("[%s]", unit.rel_path)
        before = previous.status if previous else ""
        process_unit(state, unit, log=log.info)
        current = state.get(unit.rel_path)
        status = current.status if current else ""
        if status in ("converted", "ready") and before != status:
            converted += 1
        elif status == "held":
            held += 1
        elif status == "failed":
            failed += 1

    state.finish_run(run_id, seen, converted, held, failed)
    state.heartbeat()
    log.info("scan done: %d new, %d converted, %d held, %d failed", seen, converted, held, failed)
    return {"seen": seen, "converted": converted, "held": held, "failed": failed}


def run() -> None:
    """Loop forever, honouring SCAN_INTERVAL_MIN."""
    state = State(CONFIG.state_dir)
    CONFIG.ensure_dirs()
    log.info("audiobind daemon: %s -> %s every %d min",
             CONFIG.library_root, CONFIG.output_root, CONFIG.scan_interval_min)
    if CONFIG.web_enabled:
        web.serve(state)
        log.info("status page on http://0.0.0.0:%d", CONFIG.web_port)

    while True:
        try:
            scan_once(state)
        except Exception:                   # noqa: BLE001 - never let the loop die
            log.exception("scan failed")
        time.sleep(max(60, CONFIG.scan_interval_min * 60))

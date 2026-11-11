"""INGEST_DIR: finished torrent downloads, watched read-only, converted like library books.

Each direct child folder of the ingest root is a candidate book unit that goes through the very
same audit -> match -> convert -> publish path as a library unit. Only two things differ: the
unit's ``source`` is "ingest", and its state key is ``ingest:<path under INGEST_DIR>``.

The ingest root belongs to the torrent client - the files are still seeding. Nothing in
Audiobind writes, moves or deletes anything under it: discovery only lists and reads, the
encoder only reads its sources, and Delete refuses ingest rows outright.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import CONFIG, effective_val
from .state import INGEST_KEY_PREFIX, SOURCE_INGEST, key_source
from .units import BookUnit, discover_ingest_units, find_unit

log = logging.getLogger("audiobind")

#: why the daemon switched ingest off at start ("" = it did not). Set once by check_at_start;
#: a folder missing when the daemon starts stays off until a restart, as the warning says
_DISABLED_AT_START = ""

#: what Delete answers for an ingest row (also shown in the panel's tooltip)
DELETE_REFUSAL = (
    "delete is for library sources; remove the torrent in qBittorrent - this book's files are "
    "a torrent that is still seeding, so Audiobind never deletes them"
)


def configured_dir() -> str:
    """The INGEST_DIR setting through the effective-config getter; "" when the feature is off."""
    return str(effective_val("ingest_dir") or "").strip()


def ingest_root() -> Path | None:
    """The ingest root to scan, or None when the feature is off (unset, or disabled at start)."""
    configured = configured_dir()
    if not configured or _DISABLED_AT_START:
        return None
    return Path(configured)


def check_at_start() -> bool:
    """Daemon start: an INGEST_DIR that is set must exist. Warn and keep running without it if not.

    Also refuses a folder that overlaps OUTPUT_ROOT or LIBRARY_ROOT: published books would be
    ingested again, or library books would be converted twice under two names. Returns True
    when ingest is on for this run.
    """
    global _DISABLED_AT_START
    _DISABLED_AT_START = ""
    configured = configured_dir()
    if not configured:
        return False
    root = Path(configured)
    problem = ""
    if not root.is_dir():
        problem = f"INGEST_DIR {configured} does not exist or is not a directory"
    else:
        resolved = root.resolve()
        for name, other in (("OUTPUT_ROOT", CONFIG.output_root), ("LIBRARY_ROOT", CONFIG.library_root)):
            other_resolved = Path(other).resolve()
            if (
                resolved == other_resolved
                or other_resolved in resolved.parents
                or resolved in other_resolved.parents
            ):
                problem = f"INGEST_DIR {configured} overlaps {name} {other}"
                break
    if problem:
        _DISABLED_AT_START = problem
        log.warning("%s - ingest is DISABLED for this run (fix it and restart to enable)", problem)
        return False
    log.info("ingest: watching %s (read-only)", root)
    return True


def discover() -> list[BookUnit]:
    """This scan's ingest units; [] when the feature is off or the folder cannot be read."""
    root = ingest_root()
    if root is None:
        return []
    if not root.is_dir():
        # unmounted mid-run: existing rows are kept, nothing is touched, the next scan retries
        log.warning("ingest: %s is not reachable - skipping ingest this scan", root)
        return []
    try:
        return discover_ingest_units(root, CONFIG.ignore_globs)
    except OSError as exc:
        log.warning("ingest: could not read %s (%s) - skipping ingest this scan", root, exc)
        return []


def rel_of(key: str) -> str:
    """``ingest:Author - Title`` -> ``Author - Title`` (a library key is returned unchanged)."""
    return key[len(INGEST_KEY_PREFIX):] if key_source(key) == SOURCE_INGEST else key


def is_ingest(key: str) -> bool:
    return key_source(key) == SOURCE_INGEST


def find_ingest_unit(key: str) -> BookUnit | None:
    """The ingest unit on disk for this state key, or None (gone from the folder, or ingest off)."""
    root = ingest_root()
    if root is None or not is_ingest(key):
        return None
    rel = rel_of(key)
    return next((u for u in discover_ingest_units(root, CONFIG.ignore_globs) if u.rel_path == rel), None)


def find_any_unit(key: str) -> BookUnit | None:
    """The unit on disk behind a state key, from whichever root its source names."""
    if is_ingest(key):
        return find_ingest_unit(key)
    return find_unit(CONFIG.library_root, CONFIG.ignore_globs, key)


def source_dir(key: str) -> str:
    """The container path an ingest row's files live under (for the detail panel)."""
    rel = rel_of(key).split("#", 1)[0]
    configured = configured_dir()
    return str(Path(configured) / rel) if configured else rel

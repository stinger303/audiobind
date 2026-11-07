"""Cover art for the status page: fetch once, then serve from disk forever.

The page must render with the network unplugged, so nothing here is allowed to block a request
for long or to raise: every failure is a 404 and the page falls back to a neutral placeholder.

Two sources, in order:

* the published ``cover.jpg`` that the converter already wrote next to a finished m4b;
* the catalog image URL stored with the unit's match data (or, for rows matched before 0.10.0,
  the deterministic Amazon URL derived from the ASIN).

A fetch that fails leaves a ``.miss`` marker so a broken cover is not re-requested on every
two-second poll; the marker expires after six hours.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import catalog
from .config import CONFIG
from .state import State

CACHE_DIR_NAME = "cover-cache"
#: how long a failed fetch is remembered before it is tried again
MISS_TTL_SECONDS = 6 * 3600
ASIN_RE = re.compile(r"^[A-Z0-9]{8,14}$")


def cache_dir() -> Path:
    return Path(CONFIG.state_dir) / CACHE_DIR_NAME


def valid_asin(asin: str) -> bool:
    """Guard the cache filename: only a real ASIN shape ever reaches the filesystem."""
    return bool(ASIN_RE.match((asin or "").strip().upper()))


def cached_path(asin: str) -> Path:
    return cache_dir() / f"{asin.strip().upper()}.jpg"


def _miss_path(asin: str) -> Path:
    return cache_dir() / f"{asin.strip().upper()}.miss"


def _miss_is_fresh(asin: str) -> bool:
    marker = _miss_path(asin)
    try:
        return (time.time() - marker.stat().st_mtime) < MISS_TTL_SECONDS
    except OSError:
        return False


def _record_miss(asin: str) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        _miss_path(asin).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def stored_image_url(state: State, asin: str) -> str:
    """The catalog image URL persisted with the match, else the ASIN-derived fallback."""
    try:
        data = json.loads(state.detail_for_asin(asin) or "{}")
        url = ((data.get("match") or {}).get("image") or "").strip()
    except (ValueError, AttributeError):
        url = ""
    return url or catalog.cover_url({}, asin.strip().upper())


def published_cover(state: State, rel_path: str) -> Path | None:
    """``cover.jpg`` beside a converted book, resolved through the DB - never from user input."""
    unit = state.get(rel_path)
    if unit is None or not unit.output:
        return None
    candidate = Path(unit.output).parent / "cover.jpg"
    try:
        output_root = Path(CONFIG.output_root).resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    if output_root not in resolved.parents:          # stay inside the output tree
        return None
    return resolved if resolved.is_file() else None


def fetch_by_asin(state: State, asin: str) -> Path | None:
    """Return a cached cover for this ASIN, downloading it once if we have never tried."""
    if not valid_asin(asin):
        return None
    asin = asin.strip().upper()
    path = cached_path(asin)
    if path.is_file():
        return path                                   # served from disk forever after
    if _miss_is_fresh(asin):
        return None
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    temp = path.with_suffix(".part")
    if catalog.download(stored_image_url(state, asin), str(temp)):
        try:
            temp.replace(path)
            return path
        except OSError:
            pass
    try:
        temp.unlink()
    except OSError:
        pass
    _record_miss(asin)
    return None


def resolve(state: State, asin: str = "", rel_path: str = "") -> Path | None:
    """What /cover should send, or None for 'no cover - show the placeholder'."""
    if rel_path:
        local = published_cover(state, rel_path)
        if local is not None:
            return local
        unit = state.get(rel_path)
        if unit is not None and unit.asin:
            return fetch_by_asin(state, unit.asin)
        return None
    return fetch_by_asin(state, asin)

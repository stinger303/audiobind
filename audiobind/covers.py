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
import threading
import time
from pathlib import Path

from . import catalog
from .config import CONFIG
from .state import State

CACHE_DIR_NAME = "cover-cache"
#: how long a failed fetch is remembered before it is tried again
MISS_TTL_SECONDS = 6 * 3600
#: most cached covers kept; past this the oldest unprotected ones go (library covers never do)
MAX_CACHED_COVERS = 2000
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


#: a download prunes the cache only after this many new covers ...
PRUNE_EVERY_WRITES = 25
#: ... or once this long has passed since the last prune
PRUNE_EVERY_SECONDS = 600.0

_prune_lock = threading.Lock()
_writes_since_prune = 0
_last_prune = 0.0


def maybe_prune(state: State) -> bool:
    """Count one new cover and prune when PRUNE_EVERY_WRITES or PRUNE_EVERY_SECONDS is reached.

    A prune lists the cache and reads every kept ASIN; doing that on every download made a
    Discover page of fresh covers O(covers x books) under the State lock.
    """
    global _writes_since_prune, _last_prune
    with _prune_lock:
        _writes_since_prune += 1
        now = time.monotonic()
        due = _writes_since_prune >= PRUNE_EVERY_WRITES or not _last_prune or now - _last_prune >= PRUNE_EVERY_SECONDS
        if not due:
            return False
        _writes_since_prune = 0
        _last_prune = now
    prune(state)
    return True


def prune(state: State) -> int:
    """Shrink the cover cache; returns how many files were removed.

    Evicts a cover (and its .miss marker) whose ASIN is no longer in the library, a catalog-seen
    result, a series listing or the wishlist, then enforces MAX_CACHED_COVERS by dropping the
    oldest unprotected covers. A library book's cover is never removed.
    """
    try:
        files = [f for f in cache_dir().iterdir() if f.suffix in (".jpg", ".miss")]
    except OSError:
        return 0
    keep = state.cover_keep_asins()
    # built once: asking has_asin() per kept ASIN held the State lock once per cover
    library = state.library_asins()
    removed = 0

    def drop(f: Path) -> None:
        nonlocal removed
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass

    survivors = []
    for f in files:
        if f.stem.upper() in keep:
            if f.suffix == ".jpg":
                survivors.append(f)
        else:
            drop(f)
    excess = len(survivors) - MAX_CACHED_COVERS
    if excess > 0:
        def age(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        for f in sorted((f for f in survivors if f.stem.upper() not in library), key=age)[:excess]:
            drop(f)
    return removed


def stored_image_url(state: State, asin: str) -> str:
    """The best known image URL for an ASIN.

    Order: the URL stored with the unit's match data, then the Audible catalog's
    ``product_images`` for the ASIN (the ASIN-derived Amazon URL pattern only resolves
    for some ASINs - the rest return a 43-byte tracking pixel that is not a cover).
    """
    asin = asin.strip().upper()
    try:
        data = json.loads(state.detail_for_asin(asin) or "{}")
        url = ((data.get("match") or {}).get("image") or "").strip()
    except (ValueError, AttributeError):
        url = ""
    if url:
        return url
    # a book from a fetched series listing (not in the library) carries its catalog image
    listed = (state.series_image(asin) or "").strip()
    if listed:
        return listed
    # ... and so does a search result or recommendation a Discover page showed
    shown = (state.catalog_seen_image(asin) or "").strip()
    if shown:
        return shown
    images = catalog.product_images(asin)
    for size in ("500", "1000"):
        if images.get(size):
            return images[size]
    return catalog.cover_url({}, asin)


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
    if output_root not in resolved.parents:  # stay inside the output tree
        return None
    return resolved if resolved.is_file() else None


def fetch_by_asin(state: State, asin: str) -> Path | None:
    """Return a cached cover for this ASIN, downloading it once if we have never tried."""
    if not valid_asin(asin):
        return None
    asin = asin.strip().upper()
    path = cached_path(asin)
    if path.is_file():
        return path  # served from disk forever after
    if (
        not state.has_asin(asin)
        and state.series_image(asin) is None
        and state.catalog_seen_image(asin) is None
    ):
        # only books in the library, in a series listing the shelf fetched, or in catalog
        # results a Discover page showed get covers fetched: /cover?asin=<anything> must not
        # trigger outbound requests or leave a .miss file per made-up value
        return None
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
            maybe_prune(state)
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

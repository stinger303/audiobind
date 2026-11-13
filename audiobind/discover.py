"""Discovery: the wishlist, catalog search, recommendations from what you own.

Phase 2 of the acquisition plan (docs/backlog.md). The wishlist is the hand-off point to
Phase 3: a row lands there only when you click "Get this" / "Want" - nothing in this module
adds one on its own, downloads anything or talks to a tracker.
"""

from __future__ import annotations

import logging

from . import series as series_mod
from .covers import valid_asin
from .state import WISHLIST_WANTED

log = logging.getLogger("audiobind")

#: where a wishlist row came from (shown in the list; free text from API callers is refused)
ORIGINS = ("search", "for_you", "series", "api")
MAX_TITLE = 300
MAX_NAME = 200
MAX_AUTHORS = 10


# ------------------------------------------------------------------ the wishlist
def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _names(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    names = [_text(v, MAX_NAME) for v in value if isinstance(v, (str, int, float))]
    return [n for n in names if n][:MAX_AUTHORS]


def series_entry(state, asin: str) -> dict | None:
    """A book from a stored series listing, with its series name and author (cache only)."""
    try:
        shelf = series_mod.aggregate(state, refresh=False)
    except Exception:  # noqa: BLE001 - a series problem must not break an add
        log.exception("series shelf could not be read for a wishlist add")
        return None
    for view in shelf["series"]:
        for entry in list(view.get("missing") or []) + list(view.get("marked") or []) + list(view.get("extras") or []):
            if entry.get("asin") == asin:
                return {
                    "asin": asin,
                    "title": str(entry.get("title") or ""),
                    "authors": [view["author"]] if view.get("author") else [],
                    "series": str(view.get("name") or ""),
                    "sequence": str(entry.get("sequence") or entry.get("n") or ""),
                    "hours": entry.get("hours") or 0,
                }
    return None


def known_product(state, asin: str) -> dict | None:
    """What the server already knows about an ASIN: a series listing entry. Known metadata
    always wins over what a client posts."""
    return series_entry(state, asin)


def wishlist_item(state, payload: dict) -> tuple[dict | None, str]:
    """(row to store, '') or (None, why it was refused) for one POSTed add."""
    asin = str(payload.get("asin") or "").strip().upper()
    if not valid_asin(asin):
        return None, "that is not an ASIN - it is 8-14 letters and digits, like B005EHN61O"
    origin = str(payload.get("origin") or "api").strip().lower()
    if origin not in ORIGINS:
        origin = "api"
    known = known_product(state, asin)
    if known and known.get("title"):
        item = dict(known)
    else:
        title = _text(payload.get("title"), MAX_TITLE)
        if not title:
            return None, "Audiobind has not seen that ASIN in a search or a series - send a title with it"
        item = {
            "asin": asin,
            "title": title,
            "authors": _names(payload.get("authors")),
            "series": _text(payload.get("series"), MAX_TITLE),
            "sequence": _text(payload.get("sequence"), 20),
            "hours": payload.get("hours") or 0,
        }
    item["asin"] = asin
    item["title"] = _text(item.get("title"), MAX_TITLE)
    item["authors"] = _names(item.get("authors"))
    try:
        item["hours"] = max(0.0, round(float(item.get("hours") or 0), 2))
    except (TypeError, ValueError):
        item["hours"] = 0.0
    item["origin"] = origin
    return item, ""


def add(state, payload: dict) -> tuple[dict | None, bool, str]:
    """(stored row, newly added, error). Re-adding an ASIN is not an error: added=False."""
    item, error = wishlist_item(state, payload)
    if item is None:
        return None, False, error
    added = state.wishlist_add(item)
    if added:
        log.info("wishlist: added %s (%s) from %s", item["asin"], item["title"], item["origin"])
    return state.wishlist_get(item["asin"]), added, ""


def public_row(row: dict) -> dict:
    """A wishlist row as the API shows it."""
    return {k: row.get(k) for k in ("asin", "title", "authors", "added_at", "status", "origin",
                                    "series", "sequence", "hours")}


__all__ = ["ORIGINS", "WISHLIST_WANTED", "add", "known_product", "public_row", "series_entry", "wishlist_item"]

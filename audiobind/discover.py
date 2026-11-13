"""Discovery: the wishlist, catalog search, recommendations from what you own.

Phase 2 of the acquisition plan (docs/backlog.md). The wishlist is the hand-off point to
Phase 3: a row lands there only when you click "Get this" / "Want" - nothing in this module
adds one on its own, downloads anything or talks to a tracker.
"""

from __future__ import annotations

import logging

from . import catalog, matching
from . import series as series_mod
from .covers import valid_asin
from .state import WISHLIST_WANTED
from .util import norm

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
    """What the server already knows about an ASIN: a catalog result a Discover page showed,
    else a series listing entry. Known metadata always wins over what a client posts."""
    seen = state.catalog_seen(asin)
    if seen and seen.get("title"):
        return {"asin": asin, **{k: seen.get(k) for k in ("title", "authors", "series", "sequence", "hours")}}
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


# ------------------------------------------------------------------ already in your library
class LibraryIndex:
    """The already-in-library guard for catalog results.

    A catalog book is yours when a book in the library is matched or pinned to its ASIN, when
    you marked its ASIN "I have this" on the Series page, or when a library book has the same
    title and first author after normalisation (another edition of the same work). The same
    three rules the series shelf uses to count a book as owned.
    """

    def __init__(self, rows: list[dict], marks: set[str]):
        self.asins: dict[str, dict] = {}
        self.works: dict[tuple[str, str], dict] = {}
        for row in rows:
            for asin in (row.get("asin"), row.get("asin_override")):
                if asin:
                    self.asins.setdefault(str(asin), row)
            title = norm(row.get("title"))
            author = norm(series_mod.unit_author(row))
            if title and author:
                self.works.setdefault((title, author), row)
        self.marks = set(marks)

    @classmethod
    def from_state(cls, state) -> LibraryIndex:
        return cls(state.series_rows(), state.owned_marks())

    def owner(self, product: dict) -> dict | None:
        """The library row that makes this catalog book yours ({} for an "I have this" mark)."""
        asin = str(product.get("asin") or "")
        if asin and asin in self.asins:
            return self.asins[asin]
        if asin and asin in self.marks:
            return {}
        authors = product.get("authors") or []
        key = (norm(product.get("title")), norm(authors[0] if authors else ""))
        if key[0] and key[1] and key in self.works:
            return self.works[key]
        return None

    def owns(self, product: dict) -> bool:
        return self.owner(product) is not None


# ------------------------------------------------------------------ catalog results
def product_dict(product) -> dict:
    """A catalog Product as the Discover pages, the API and the catalog_seen table store it."""
    return {
        "asin": product.asin,
        "title": product.title,
        "subtitle": product.subtitle,
        "authors": list(product.authors),
        "narrators": list(product.narrators),
        "series": product.series,
        "sequence": product.sequence,
        "hours": product.hours,
        "year": product.year,
        "language": product.language,
        "image": product.image,
    }


def mark(state, products: list[dict], index: LibraryIndex | None = None, wanted: set[str] | None = None) -> list[dict]:
    """Each result with ``in_library`` / ``wishlisted`` / ``knockoff`` flags for the page."""
    index = index or LibraryIndex.from_state(state)
    wanted = state.wishlist_asins() if wanted is None else wanted
    out = []
    for product in products:
        row = dict(product)
        owner = index.owner(product)
        row["in_library"] = owner is not None
        row["library_status"] = (owner or {}).get("status") or ""
        row["wishlisted"] = product.get("asin") in wanted
        row["knockoff"] = matching.is_knockoff(product, products)
        out.append(row)
    return out


#: one search page shows at most this many results
SEARCH_LIMIT = 25
MAX_QUERY = 200


def search(state, title: str = "", author: str = "", limit: int = SEARCH_LIMIT) -> dict:
    """One catalog search for the Search page / API: {"ok", "error", "results", ...}.

    One throttled catalog request (catalog.query), single region: the store the catalog
    API answers for (api.audible.com). Results are remembered in catalog_seen so their
    covers can be shown and a "Get this" click stores the catalog's own metadata.
    """
    title = _text(title, MAX_QUERY)
    author = _text(author, MAX_QUERY)
    out = {"ok": True, "error": "", "title": title, "author": author, "results": []}
    if not title and not author:
        return out
    products = catalog.query(title=title, author=author, limit=limit)
    if products is None:
        out["ok"] = False
        out["error"] = "The Audible catalog could not be reached - try again in a minute."
        return out
    rows = [product_dict(p) for p in products if p.asin]
    state.catalog_seen_put(rows)
    out["results"] = mark(state, rows)
    return out


__all__ = [
    "ORIGINS",
    "WISHLIST_WANTED",
    "LibraryIndex",
    "add",
    "known_product",
    "mark",
    "product_dict",
    "public_row",
    "search",
    "series_entry",
    "wishlist_item",
]

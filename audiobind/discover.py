"""Discovery: the wishlist, catalog search, recommendations from what you own.

Phase 2 of the acquisition plan (docs/backlog.md). The wishlist is the hand-off point to
Phase 3: a row lands there only when you click "Get this" / "Want" - nothing in this module
adds one on its own, downloads anything or talks to a tracker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path

from . import catalog, matching
from . import series as series_mod
from .config import CONFIG
from .covers import valid_asin
from .overrides import split_names
from .state import WISHLIST_WANTED
from .util import norm, sim

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


# ------------------------------------------------------------------ For you (recommendations)
# Computed from what the library already knows - nothing is sent anywhere but the catalog
# queries below. A refresh asks the catalog at most MAX_CALLS times; the page only ever
# reads the stored result, so a page load never queries the catalog.

RECS_KEY = "for_you"
#: a stored result older than this is refreshed when the For you page is opened
RECS_TTL_SECONDS = 7 * 24 * 3600
TOP_AUTHORS = 5
TOP_NARRATORS = 3
TOP_GENRES = 2
#: the politeness cap: one catalog query per seed, never more than this per refresh
MAX_CALLS = TOP_AUTHORS + TOP_NARRATORS + TOP_GENRES
#: how many results each query asks for, and how many suggestions each seed may keep
QUERY_SIZE = 30
PER_SEED = 6
#: a contributor in a result must match the seed this well to count as "by" / "read by" them.
#: Strictly above util.sim's 0.85 substring floor: "Lee Child" sits inside "Lee Childs Fan
#: Club", which is a different person; spelling and punctuation variants still pass.
NAME_MATCH = 0.9
#: metadata.json is small; anything bigger is not ours
MAX_METADATA_BYTES = 256 * 1024

SECTION_TITLES = {
    "author": "More by authors you own",
    "narrator": "Same narrator",
    "genre": "Same genre",
}


def _loads(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row_narrators(row: dict) -> list[str]:
    overrides = _loads(row.get("overrides"))
    if overrides.get("narrator"):
        return split_names(overrides["narrator"])
    match = _loads(row.get("detail")).get("match") or {}
    return [str(n) for n in (match.get("narrators") or []) if str(n or "").strip()]


def _row_genres(row: dict) -> list[str]:
    """Genres from the metadata.json Audiobind wrote beside a published book (the state DB
    does not keep genres). Read only inside OUTPUT_ROOT; anything odd counts as none."""
    output = str(row.get("output") or "")
    if not output:
        return []
    try:
        root = Path(CONFIG.output_root).resolve()
        meta = (Path(output).parent / "metadata.json").resolve()
        if root not in meta.parents or not meta.is_file() or meta.stat().st_size > MAX_METADATA_BYTES:
            return []
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    genres = data.get("genres") if isinstance(data, dict) else None
    return [str(g).strip() for g in (genres or []) if isinstance(g, str) and g.strip()]


def _tally(counter: Counter, spellings: dict, names: list[str]) -> None:
    """Count each normalised name once per book, remembering the most used spelling."""
    for key in {norm(n) for n in names if norm(n)}:
        counter[key] += 1
    for name in names:
        spellings.setdefault(norm(name), Counter())[name] += 1


def _top(counter: Counter, spellings: dict, n: int) -> list[tuple[str, int]]:
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [(spellings[key].most_common(1)[0][0], count) for key, count in ranked]


def owned_signals(state, read_genres: bool = True) -> dict:
    """What the library says you like: top authors, narrators and genres by owned count,
    plus the languages your matched books are in (suggestions stay in those)."""
    authors, narrators, genres = Counter(), Counter(), Counter()
    spell_a: dict = {}
    spell_n: dict = {}
    spell_g: dict = {}
    languages: Counter = Counter()
    for row in state.series_rows():
        if not row.get("title"):
            continue
        author = series_mod.unit_author(row)
        if author:
            _tally(authors, spell_a, [author])
        _tally(narrators, spell_n, _row_narrators(row))
        if read_genres:
            _tally(genres, spell_g, _row_genres(row))
        language = norm((_loads(row.get("detail")).get("match") or {}).get("language"))
        if language:
            languages[language] += 1
    return {
        "authors": _top(authors, spell_a, TOP_AUTHORS),
        "narrators": _top(narrators, spell_n, TOP_NARRATORS),
        "genres": _top(genres, spell_g, TOP_GENRES),
        "languages": sorted(languages) or ["english"],
    }


def _plan(signals: dict) -> list[dict]:
    """The catalog queries one refresh makes - one per seed, capped at MAX_CALLS."""
    plan = []
    for kind, key in (("author", "authors"), ("narrator", "narrators"), ("genre", "genres")):
        for name, owned in signals[key]:
            plan.append({"kind": kind, "seed": name, "owned": owned})
    return plan[:MAX_CALLS]


def _run_query(step: dict) -> list | None:
    if step["kind"] == "author":
        return catalog.query(author=step["seed"], limit=QUERY_SIZE)
    if step["kind"] == "narrator":
        return catalog.query(narrator=step["seed"], limit=QUERY_SIZE)
    return catalog.query(keywords=step["seed"], limit=QUERY_SIZE)


def _credited(step: dict, product: dict) -> bool:
    """An author query must return books by that author, a narrator query books read by them
    (the catalog's name filters are fuzzy). Genre keywords are taken as the catalog ranks them."""
    if step["kind"] == "author":
        return any(sim(a, step["seed"]) >= NAME_MATCH for a in product.get("authors") or [])
    if step["kind"] == "narrator":
        return any(sim(n, step["seed"]) >= NAME_MATCH for n in product.get("narrators") or [])
    return True


def _work_key(product: dict) -> tuple[str, str]:
    authors = product.get("authors") or []
    return norm(product.get("title")), norm(authors[0] if authors else "")


def compute_recommendations(state) -> dict:
    """Run the capped catalog queries and build the For you sections. Never adds anything to
    the wishlist: the result is only stored and shown."""
    signals = owned_signals(state)
    index = LibraryIndex.from_state(state)
    wanted = state.wishlist_asins()
    languages = set(signals["languages"])
    seen_asins: set[str] = set()
    seen_works: set[tuple[str, str]] = set()
    sections = {kind: [] for kind in SECTION_TITLES}
    calls = 0
    failed = 0
    remembered: list[dict] = []
    for step in _plan(signals):
        calls += 1
        products = _run_query(step)
        group = {"seed": step["seed"], "owned": step["owned"], "ok": products is not None, "items": []}
        if products is None:
            failed += 1
            sections[step["kind"]].append(group)
            continue
        rows = [product_dict(p) for p in products if p.asin]
        for row in rows:
            if len(group["items"]) >= PER_SEED:
                break
            if row["asin"] in seen_asins or row["asin"] in wanted:
                continue
            if row.get("language") and norm(row["language"]) not in languages:
                continue
            if not _credited(step, row) or matching.is_knockoff(row, rows) or index.owns(row):
                continue
            work = _work_key(row)
            if work[0] and work in seen_works:
                continue  # another edition of a book already suggested
            seen_asins.add(row["asin"])
            if work[0]:
                seen_works.add(work)
            group["items"].append(row)
        remembered.extend(group["items"])
        sections[step["kind"]].append(group)
    if remembered:
        state.catalog_seen_put(remembered)
    return {
        "calls": calls,
        "failed": failed,
        "signals": {k: [list(pair) for pair in signals[k]] for k in ("authors", "narrators", "genres")},
        "sections": [{"kind": kind, "title": SECTION_TITLES[kind], "groups": sections[kind]} for kind in SECTION_TITLES],
    }


def refresh_recommendations(state) -> dict:
    """Compute and store. A refresh that reached the catalog for nothing keeps the last good
    result (marked with the failure) rather than replacing it with an empty page."""
    result = compute_recommendations(state)
    if result["calls"] and result["failed"] == result["calls"]:
        previous = state.discover_cache_get(RECS_KEY)
        if previous and previous["payload"].get("sections"):
            kept = dict(previous["payload"])
            kept["last_error"] = "unreachable"
            state.discover_cache_put(RECS_KEY, kept)
            return kept
    state.discover_cache_put(RECS_KEY, result)
    return result


#: tests replace this; the background refresher calls it with (state)
REFRESH = refresh_recommendations


def recs_stale(record: dict | None, now: float | None = None) -> bool:
    if not record:
        return True
    now = time.time() if now is None else now
    return now - float(record.get("fetched_at") or 0) > RECS_TTL_SECONDS


class RecsRefresher:
    """At most one recommendations refresh in flight; a second request while it runs is dropped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def schedule(self, state) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            thread = threading.Thread(target=self._run, args=(state,), name="for-you", daemon=True)
            self._thread = thread
            thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, state) -> None:
        try:
            REFRESH(state)
        except Exception as exc:  # noqa: BLE001 - a background thread has nobody to raise to
            log.warning("For you refresh stopped: %s", exc)
        finally:
            with self._lock:
                self._running = False


RECS_REFRESHER = RecsRefresher()


def request_refresh(state, force: bool = False) -> bool:
    """Queue a background refresh: always when ``force`` (the Refresh button), otherwise only
    when nothing is stored yet or the stored result is older than RECS_TTL_SECONDS. A fresh
    result means no catalog call at all."""
    if not force and not recs_stale(state.discover_cache_get(RECS_KEY)):
        return False
    return RECS_REFRESHER.schedule(state)


def recommendations(state) -> dict:
    """The stored For you result as the page / API shows it - cache only, never the network.

    Every item is checked again against the library and the wishlist as they are now, so a
    book you got or wished for since the last refresh is no longer suggested.
    """
    record = state.discover_cache_get(RECS_KEY)
    payload = (record or {}).get("payload") or {}
    index = LibraryIndex.from_state(state)
    wanted = state.wishlist_asins()
    sections = []
    for section in payload.get("sections") or []:
        groups = []
        for group in section.get("groups") or []:
            items = [dict(i, in_library=False, wishlisted=False, knockoff=False)
                     for i in group.get("items") or []
                     if i.get("asin") not in wanted and not index.owns(i)]
            groups.append({**group, "items": items})
        sections.append({**section, "groups": groups})
    return {
        "fetched_at": (record or {}).get("fetched_at"),
        "stale": recs_stale(record),
        "refreshing": RECS_REFRESHER.busy,
        "calls": payload.get("calls", 0),
        "failed": payload.get("failed", 0),
        "last_error": payload.get("last_error", ""),
        "signals": payload.get("signals") or {"authors": [], "narrators": [], "genres": []},
        "sections": sections,
    }


__all__ = [
    "MAX_CALLS",
    "ORIGINS",
    "RECS_REFRESHER",
    "WISHLIST_WANTED",
    "LibraryIndex",
    "add",
    "compute_recommendations",
    "known_product",
    "mark",
    "owned_signals",
    "product_dict",
    "public_row",
    "recommendations",
    "refresh_recommendations",
    "request_refresh",
    "search",
    "series_entry",
    "wishlist_item",
]

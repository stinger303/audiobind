"""Series shelf: which series your library holds, how long they are on Audible, what's missing.

Three sources, read in this order of trust, say which series a book belongs to:

* the operator's metadata overrides (series / series index) - a human typed them;
* the series and sequence persisted with the catalog match (0.15.0-dev2 onward);
* the published output path, ``<author>/<series>/<NN> - <title>/<title>.m4b``;
* the source folder, ``Lee Child - Jack Reacher/14 - 61 Hours`` - the weakest guess.

Books are grouped by (series, author). Every series with at least two of your books gets
its full listing from the Audible catalog, once a day at most, in a background thread - a
page view only ever reads the last stored listing. When the listing can't be fetched the
shelf says so and never guesses a total.

Gap rules: only whole-number sequences count. Novellas with no number and "between the
numbers" entries like 23.6 are listed apart and never counted as missing. A sequence is
owned when one of your books is matched or pinned to that entry's ASIN, carries that
number, or has that exact title; "I have this" marks an ASIN as owned without a rip.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path

from . import catalog
from .config import CONFIG
from .state import INGEST_KEY_PREFIX, SOURCE_INGEST, key_source
from .units import split_author_title
from .util import SERIES_DIR_RE, clean, norm, sim

log = logging.getLogger("audiobind")

#: a good listing is re-fetched after a day; a failed or empty one is retried sooner
TTL_OK_SECONDS = 24 * 3600
TTL_FAIL_SECONDS = 2 * 3600
#: a series needs this many of your books before the shelf shows it (and Audible is asked)
MIN_OWNED = 2

_INT_RE = re.compile(r"^\s*0*(\d{1,4})(?:\.0+)?\s*$")
_BETWEEN_RE = re.compile(r"^\d+\.\d+$")
_LEAF_SEQ_RE =re.compile(r"^\s*(?:book\s*|#\s*)?(\d{1,3})(?:\s*[-–—.)]\s*|\s+)(?=\S)", re.I)


# ------------------------------------------------------------------ small helpers
def seq_number(sequence) -> int | None:
    """'19' / '019' / '19.0' -> 19; '', '23.6', '1-3' -> None (not a whole number)."""
    match = _INT_RE.match(str(sequence if sequence is not None else ""))
    return int(match.group(1)) if match else None


def series_key(name: str) -> str:
    """'A Lincoln Lawyer Novel' and 'Lincoln Lawyer' are the same series."""
    key = norm(name)
    key = re.sub(r"^(a|an|the) ", "", key)
    key = re.sub(r" (series|novels?|saga|books?)$", "", key)
    return key.strip()


def group_key(name: str, author: str) -> str:
    return f"{series_key(name)}|{norm(author)}"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(text)).strip("-")


def _loads(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ which series is a book in
def _from_output(output: str) -> tuple[str, str]:
    """``<author>/<series>/<NN> - <title>/<title>.m4b`` -> (series, NN). Anything else -> ('', '')."""
    if not output:
        return "", ""
    path = Path(output)
    try:
        parts = path.relative_to(CONFIG.output_root).parts
    except ValueError:
        parts = path.parts[-4:]
    if len(parts) != 4 or not parts[3].lower().endswith(".m4b"):
        return "", ""
    match = re.match(r"^(.+?) - ", parts[2])
    if not match:
        return "", ""
    number = seq_number(match.group(1))
    return parts[1], str(number) if number is not None else match.group(1)


def _from_folder(rel_path: str, author: str, title: str) -> tuple[str, str]:
    """``Lee Child - Jack Reacher/14 - 61 Hours`` -> ('Jack Reacher', '14'). Conservative."""
    # an ingest row's key carries an "ingest:" prefix that is not part of any folder name
    if key_source(rel_path) == SOURCE_INGEST:
        rel_path = rel_path[len(INGEST_KEY_PREFIX):]
    segments = [s for s in Path(rel_path.split("#", 1)[0]).parts if s]
    if len(segments) < 2:
        return "", ""
    leaf, ancestors = segments[-1], segments[:-1]
    series = ""
    for segment in reversed(ancestors):
        seg_author, seg_rest = split_author_title(segment)
        if seg_author and seg_rest and author and sim(seg_author, author) >= 0.85:
            if norm(seg_rest) not in (norm(title), norm(author)):
                series = clean(seg_rest)
                break
        if SERIES_DIR_RE.search(segment) and (not author or sim(segment, author) < 0.5):
            series = clean(re.sub(r"(?i)\bseries\b", " ", segment))
            break
    if not series:
        return "", ""
    leaf_match = _LEAF_SEQ_RE.match(leaf)
    return series, leaf_match.group(1) if leaf_match else ""


def unit_series(row: dict) -> tuple[str, str]:
    """(series name, sequence) for one state row; ('', '') when nothing says it is in one."""
    overrides = _loads(row.get("overrides"))
    match = _loads(row.get("detail")).get("match") or {}
    if overrides.get("series"):
        return str(overrides["series"]), str(overrides.get("series_index") or "")
    if match.get("series"):
        return str(match["series"]), str(overrides.get("series_index") or match.get("sequence") or "")
    series, sequence = _from_output(row.get("output") or "")
    if series:
        return series, sequence
    return _from_folder(row.get("path") or "", unit_author(row), row.get("title") or "")


def unit_author(row: dict) -> str:
    overrides = _loads(row.get("overrides"))
    if overrides.get("authors"):
        return str(overrides["authors"][0])
    match = _loads(row.get("detail")).get("match") or {}
    if row.get("author"):
        return str(row["author"])
    authors = match.get("authors") or []
    return str(authors[0]) if authors else ""


# ------------------------------------------------------------------ grouping
def group_rows(rows: list[dict]) -> dict[str, dict]:
    """{group key: {"name", "author", "books": [...]}} for every series that has any book."""
    groups: dict[str, dict] = {}
    for row in rows:
        name, sequence = unit_series(row)
        if not name or not series_key(name):
            continue
        author = unit_author(row)
        key = group_key(name, author)
        group = groups.setdefault(key, {"key": key, "names": Counter(), "authors": Counter(), "books": []})
        group["names"][name] += 1
        if author:
            group["authors"][author] += 1
        asins = [a for a in (row.get("asin"), row.get("asin_override")) if a]
        group["books"].append(
            {
                "path": row.get("path") or "",
                "title": row.get("title") or "",
                "sequence": sequence,
                "asins": asins,
                "asin": asins[0] if asins else "",
                "has_output": bool(row.get("output")),
                "status": row.get("status") or "",
            }
        )
    for group in groups.values():
        group["name"] = group["names"].most_common(1)[0][0]
        group["author"] = group["authors"].most_common(1)[0][0] if group["authors"] else ""
    return groups


# ------------------------------------------------------------------ the Audible listing
def pick_series(products: list[dict], name: str, owned_asins: set[str]) -> dict | None:
    """From raw catalog products, the entries of the one series called ``name``.

    The series ASIN is chosen by vote among products whose series title matches; a product
    you own counts extra, so a same-named series by someone else loses. One entry per whole
    number (your edition first, else the most relevant English one with a runtime).
    """
    want = series_key(name)
    votes: Counter[str] = Counter()
    titles: dict[str, str] = {}
    for product in products:
        for entry in product.get("series") or []:
            sasin = str(entry.get("asin") or "")
            if not sasin or series_key(str(entry.get("title") or "")) != want:
                continue
            votes[sasin] += 5 if product.get("asin") in owned_asins else 1
            titles.setdefault(sasin, str(entry.get("title") or ""))
    if not votes:
        return None
    series_asin = votes.most_common(1)[0][0]

    numbered: dict[int, dict] = {}
    extras: dict[str, dict] = {}
    for product in products:
        language = str(product.get("language") or "")
        if language and not language.lower().startswith("english"):
            continue
        for entry in product.get("series") or []:
            if str(entry.get("asin") or "") != series_asin:
                continue
            item = {
                "asin": str(product.get("asin") or ""),
                "title": str(product.get("title") or ""),
                "sequence": str(entry.get("sequence") or "").strip(),
                "hours": round(float(product.get("runtime_length_min") or 0) / 60, 1),
                "image": catalog.product_image(product),
            }
            if not item["asin"]:
                continue
            number = seq_number(item["sequence"])
            if number is None:
                extras.setdefault(norm(item["title"]) or item["asin"], item)
                continue
            current = numbered.get(number)
            if current is None:
                numbered[number] = item
            elif item["asin"] in owned_asins and current["asin"] not in owned_asins:
                numbered[number] = item
            elif not current["hours"] and item["hours"] and current["asin"] not in owned_asins:
                numbered[number] = item
    if not numbered and not extras:
        return None
    entries = [numbered[n] for n in sorted(numbered)] + sorted(extras.values(), key=lambda e: e["title"])
    return {"series_asin": series_asin, "title": titles.get(series_asin, name), "entries": entries}


def fetch_series(name: str, author: str, owned_asins: set[str]) -> dict | None:
    """The full Audible listing for one series, or None when the catalog could not be reached.

    Walks up to SERIES_MAX_PAGES pages (whether the API counts pages from 0 or 1, the
    de-duplication by product ASIN keeps the walk correct). An empty dict means the catalog
    answered but has no such series.
    """
    products: list[dict] = []
    seen: set[str] = set()
    idle_pages = 0
    for page in range(catalog.SERIES_MAX_PAGES):
        batch = catalog.series_products(name, author, page)
        if batch is None:
            if page == 0:
                return None
            break
        fresh = [p for p in batch if p.get("asin") and p["asin"] not in seen]
        seen.update(p["asin"] for p in fresh)
        products.extend(fresh)
        idle_pages = 0 if fresh else idle_pages + 1
        if len(batch) < catalog.SERIES_PAGE_SIZE or idle_pages >= 2:
            break
    picked = pick_series(products, name, owned_asins)
    if picked is None and author:
        # the author filter can hide a series filed under a co-author's name
        batch = catalog.series_products(name, "", 0)
        if batch:
            picked = pick_series(batch, name, owned_asins)
    return picked or {}


#: tests replace this; the refresher calls it with (name, author, owned_asins)
FETCH = fetch_series


def is_stale(record: dict | None, now: float | None = None) -> bool:
    if not record:
        return True
    now = time.time() if now is None else now
    good = record.get("ok") and not (record.get("payload") or {}).get("last_error")
    return now - float(record.get("fetched_at") or 0) > (TTL_OK_SECONDS if good else TTL_FAIL_SECONDS)


def refresh_one(state, job: dict) -> None:
    """Fetch one series and store the outcome. A failure never discards a good old listing."""
    try:
        result = FETCH(job["name"], job["author"], set(job.get("owned_asins") or ()))
    except Exception as exc:  # noqa: BLE001 - one series must not stop the others
        log.debug("series lookup for %s failed: %s", job["name"], exc)
        result = None
    previous = job.get("previous") or {}
    if result and result.get("entries"):
        entries = result["entries"]
        payload = {**result, "fetched_at": time.time()}
        state.series_cache_put(job["key"], True, payload, [(e["asin"], e.get("image", "")) for e in entries])
        return
    reason = "unreachable" if result is None else "not_found"
    if previous.get("ok"):
        old = dict(previous.get("payload") or {})
        old["last_error"] = reason
        asins = [(e["asin"], e.get("image", "")) for e in old.get("entries") or [] if e.get("asin")]
        state.series_cache_put(job["key"], True, old, asins)
    else:
        state.series_cache_put(job["key"], False, {"reason": reason}, [])


class Refresher:
    """At most one background refresh in flight; a second request while it runs is dropped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running

    def schedule(self, state, jobs: list[dict]) -> bool:
        if not jobs:
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
            thread = threading.Thread(target=self._run, args=(state, list(jobs)), name="series", daemon=True)
            self._thread = thread
            thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, state, jobs: list[dict]) -> None:
        try:
            for job in jobs:
                refresh_one(state, job)
        except Exception as exc:  # noqa: BLE001 - a background thread has nobody to raise to
            log.debug("series refresh stopped: %s", exc)
        finally:
            with self._lock:
                self._running = False


REFRESHER = Refresher()


# ------------------------------------------------------------------ the gap computation
#: where a series total comes from - the page says which, and never invents a number
SOURCE_CATALOG = "catalog"          # a stored Audible listing: the total is known
SOURCE_PENDING = "pending"          # never asked yet: the background refresher will
SOURCE_NOT_FOUND = "not_found"      # the catalog answered and has no such series
SOURCE_UNREACHABLE = "unreachable"  # the catalog could not be reached; retried later


def source_of(record: dict | None) -> str:
    if not record:
        return SOURCE_PENDING
    if record.get("ok") and (record.get("payload") or {}).get("entries"):
        return SOURCE_CATALOG
    reason = str((record.get("payload") or {}).get("reason") or "")
    return SOURCE_NOT_FOUND if reason == SOURCE_NOT_FOUND else SOURCE_UNREACHABLE


def compute(group: dict, record: dict | None, marks: set[str]) -> dict:
    """One series card: owned, total (only when known), missing, the cover strip, the extras."""
    books = group["books"]
    view = {
        "key": group["key"],
        "name": group["name"],
        "author": group["author"],
        "owned": len(books),
        "paths": [b["path"] for b in books],
        "known": False,
        "total": None,
        "have": None,
        "missing": [],
        "marked": [],
        "tiles": [],
        "extras": [],
        "unnumbered": 0,
        "between": [],
        "other": 0,
        "next_gap": None,
        "avg_hours": 0.0,
        "fetched_at": None,
        "source": source_of(record),
        "last_error": "",
    }
    payload = (record or {}).get("payload") or {}
    entries = payload.get("entries") or []
    if not (record and record.get("ok") and entries):
        return view

    if payload.get("title"):
        view["name"] = payload["title"]
    view["fetched_at"] = payload.get("fetched_at")
    view["last_error"] = str(payload.get("last_error") or "")
    numbered = {}
    for entry in entries:
        number = seq_number(entry.get("sequence"))
        if number is not None:
            numbered.setdefault(number, entry)
    by_asin = {e["asin"]: n for n, e in numbered.items()}
    by_title = {norm(e.get("title")): n for n, e in numbered.items() if norm(e.get("title"))}

    owned_by_number: dict[int, dict] = {}
    owned_asins: set[str] = set()
    for book in books:
        owned_asins.update(book["asins"])
        number = next((by_asin[a] for a in book["asins"] if a in by_asin), None)
        if number is None:
            number = seq_number(book["sequence"])
        if number is None:
            number = by_title.get(norm(book["title"]))
        if number is not None:
            owned_by_number.setdefault(number, book)

    marked = {n for n, e in numbered.items() if e["asin"] in marks and n not in owned_by_number}
    missing = [n for n in sorted(numbered) if n not in owned_by_number and n not in marked]
    every = sorted(set(numbered) | set(owned_by_number))

    view["known"] = True
    view["total"] = len(every)
    view["have"] = len(every) - len(missing)
    view["missing"] = [{"n": n, **numbered[n]} for n in missing]
    view["marked"] = [{"n": n, **numbered[n]} for n in sorted(marked)]
    hours = [m["hours"] for m in view["missing"] if m.get("hours")]
    view["avg_hours"] = round(sum(hours) / len(hours), 1) if hours else 0.0
    if missing:
        view["next_gap"] = {"n": missing[0], "title": numbered[missing[0]].get("title", "")}
    for n in every:
        if n in owned_by_number:
            book = owned_by_number[n]
            title = numbered[n]["title"] if n in numbered else book["title"]
            view["tiles"].append(
                {"n": n, "kind": "owned", "title": title, "path": book["path"],
                 "asin": book["asin"], "has_output": book["has_output"]}
            )
        elif n in marked:
            view["tiles"].append(
                {"n": n, "kind": "marked", "title": numbered[n]["title"], "path": "",
                 "asin": numbered[n]["asin"], "has_output": False}
            )
        else:
            view["tiles"].append(
                {"n": n, "kind": "gap", "title": numbered[n]["title"], "path": "",
                 "asin": numbered[n]["asin"], "has_output": False}
            )
    for entry in entries:
        sequence = str(entry.get("sequence") or "").strip()
        if seq_number(sequence) is not None:
            continue
        if not sequence:
            kind = "unnumbered"
            view["unnumbered"] += 1
        elif _BETWEEN_RE.match(sequence):
            kind = "between"
            view["between"].append(sequence)
        else:
            kind = "other"  # "1-3", "1, 2": box sets and collections
            view["other"] += 1
        view["extras"].append({**entry, "kind": kind, "owned": entry["asin"] in owned_asins})
    return view


def _order(view: dict) -> tuple:
    """Series with gaps first (most missing first), then complete ones, then unknown sizes."""
    if not view["known"]:
        return (2, 0, norm(view["name"]))
    if view["missing"]:
        return (0, -len(view["missing"]), norm(view["name"]))
    return (1, 0, norm(view["name"]))


def aggregate(state, refresh: bool = True) -> dict:
    """The whole shelf, from the DB and the stored listings only - never the network.

    With ``refresh`` the stale listings are queued for the background refresher.
    """
    groups = group_rows(state.series_rows())
    cache = state.series_cache_all()
    marks = state.owned_marks()
    shelf = [g for g in groups.values() if len(g["books"]) >= MIN_OWNED]
    views = [compute(g, cache.get(g["key"]), marks) for g in shelf]
    views.sort(key=_order)

    used: set[str] = set()
    for view in views:
        slug = _slugify(series_key(view["name"])) or "series"
        if slug in used:
            slug = f"{slug}-{_slugify(view['author']) or 'more'}"
        while slug in used:
            slug += "-x"
        used.add(slug)
        view["slug"] = slug

    by_path = {}
    for view in views:
        for path in view["paths"]:
            by_path.setdefault(path, view)

    if refresh:
        now = time.time()
        jobs = []
        for group in shelf:
            record = cache.get(group["key"])
            if is_stale(record, now):
                owned = {a for b in group["books"] for a in b["asins"]}
                jobs.append(
                    {"key": group["key"], "name": group["name"], "author": group["author"],
                     "owned_asins": sorted(owned), "previous": record}
                )
        REFRESHER.schedule(state, jobs)

    return {
        "series": views,
        "by_path": by_path,
        "missing_total": sum(len(v["missing"]) for v in views if v["known"]),
    }


def missing_total(state) -> int:
    """The rail badge: missing books across every series whose size is known (cache only)."""
    return aggregate(state, refresh=False)["missing_total"]


def public(view: dict) -> dict:
    """A series card as the /api/series JSON shows it."""
    keep = ("slug", "name", "author", "owned", "known", "total", "have", "missing", "marked",
            "extras", "unnumbered", "between", "other", "next_gap", "avg_hours", "fetched_at",
            "source", "last_error")
    out = {k: view.get(k) for k in keep}
    out["missing"] = [{k: m.get(k) for k in ("n", "asin", "title", "hours")} for m in view["missing"]]
    out["marked"] = [{k: m.get(k) for k in ("n", "asin", "title")} for m in view["marked"]]
    out["extras"] = [{k: e.get(k) for k in ("sequence", "asin", "title", "hours", "kind", "owned")}
                     for e in view["extras"]]
    return out

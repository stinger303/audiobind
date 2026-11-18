"""Hardcover (hardcover.app) as an evidence-only second opinion on a catalogue match.

Hardcover is a community book database with a GraphQL API (bearer token from the account's API
page). This module asks it one question per book: does this ASIN - or, failing that, this
title + author - exist there, and what does it say about series membership, position and year?

It is **evidence only**. Nothing here is imported by ``matching``; no score, margin or grade ever
reads an answer from it. The pipeline asks it after the grade and the action are decided, and the
detail panel shows one line comparing the answer with the catalogue.

Rules:

* **No token = off.** Without a token, or with HARDCOVER_EVIDENCE off, nothing here makes a request
  or creates the cache file.
* Answers (found and not-found) are cached on disk in ``STATE_DIR/hardcover-cache.json`` with a
  TTL, beside the cover cache. Failures are never cached - they are retried on the next scan.
* Every failure is a ``HardcoverError`` subclass inside this module; ``evidence_for`` turns it
  into a small dict, so no exception ever escapes into a scan.
* stdlib only (urllib + json), like ``egress.py``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .util import norm, sim, title_sim

API_URL = "https://api.hardcover.app/v1/graphql"
USER_AGENT = "audiobind (+https://github.com/stinger303/audiobind)"
#: the most bytes one answer may carry; a bigger one is a failure, never read into memory
MAX_BODY = 1024 * 1024
TIMEOUT_SECONDS = 15.0
#: Hardcover asks API clients to stay under 60 requests a minute: one request per this many seconds
MIN_GAP_SECONDS = 1.1

CACHE_FILE_NAME = "hardcover-cache.json"
#: a found record is trusted this long; series data on Hardcover changes rarely
FOUND_TTL_SECONDS = 14 * 24 * 3600
#: "not on Hardcover" is re-asked sooner: the community adds books every day
MISS_TTL_SECONDS = 2 * 24 * 3600
CACHE_MAX_ENTRIES = 5000

#: a Hardcover record is "this book" only when its title and an author resemble the catalogue's this much
SAME_BOOK_SIM = 0.85
#: two series names are the same series at this similarity ("The Expanse" ~ "Expanse")
SAME_SERIES_SIM = 0.80


class HardcoverError(Exception):
    """A Hardcover lookup failed. ``kind`` is a short machine word for the panel and the logs."""

    kind = "error"


class AuthError(HardcoverError):
    kind = "bad_token"


class RateLimited(HardcoverError):
    kind = "rate_limited"


class Unreachable(HardcoverError):
    kind = "unreachable"


class BadAnswer(HardcoverError):
    kind = "bad_answer"


# --- transport ------------------------------------------------------------------------------

_gap_lock = threading.Lock()
_last_call = 0.0


def _transport(body: bytes, token: str, timeout: float) -> tuple[int, bytes]:
    """One POST to the GraphQL endpoint: (HTTP status, body). The module's single network call;
    tests replace it. Raises only for "no answer at all" (DNS, TLS, timeout, refused)."""
    request = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(MAX_BODY + 1)
        except Exception:  # noqa: BLE001 - an unreadable error body is still that status
            data = b""
        return exc.code, data


def clean_token(raw: str) -> str:
    """The token as Hardcover's API page shows it may start with "Bearer "; keep only the token."""
    text = (raw or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def _graphql(query: str, variables: dict, token: str, timeout: float = TIMEOUT_SECONDS) -> dict:
    """``data`` of one GraphQL request, or a typed HardcoverError. Never anything else."""
    global _last_call
    token = clean_token(token)
    if not token:
        raise AuthError("no Hardcover token is set")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    with _gap_lock:
        wait = MIN_GAP_SECONDS - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        status, raw = _transport(body, token, timeout)
    except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeout, refused: all "no answer"
        raise Unreachable(f"Hardcover could not be reached ({type(exc).__name__})") from None
    if status in (401, 403):
        raise AuthError("Hardcover refused the token (expired or wrong)")
    if status == 429:
        raise RateLimited("Hardcover is rate limiting requests; try again in a minute")
    if status >= 500:
        raise Unreachable(f"Hardcover answered HTTP {status}")
    if len(raw) > MAX_BODY:
        raise BadAnswer("Hardcover's answer was too large")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise BadAnswer(f"Hardcover answered HTTP {status} with something that is not JSON") from None
    if not isinstance(payload, dict):
        raise BadAnswer("Hardcover's answer was not a JSON object")
    errors = payload.get("errors") or ([payload["error"]] if payload.get("error") else [])
    if errors:
        text = " ".join(
            (str(e.get("message", "")) + " " + str((e.get("extensions") or {}).get("code", "")))
            if isinstance(e, dict) else str(e)
            for e in errors
        ).lower()
        if any(word in text for word in ("jwt", "token", "unauthor", "authenticat", "access-denied")):
            raise AuthError("Hardcover refused the token (expired or wrong)")
        if "rate" in text and "limit" in text:
            raise RateLimited("Hardcover is rate limiting requests; try again in a minute")
        raise BadAnswer("Hardcover rejected the query")
    if status != 200:
        raise BadAnswer(f"Hardcover answered HTTP {status}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BadAnswer("Hardcover's answer had no data")
    return data


# --- queries --------------------------------------------------------------------------------

_BOOK_FIELDS = """
      id title slug release_year
      contributions { author { name } }
      book_series { position series { name } }
"""

ASIN_QUERY = (
    "query AudiobindAsin($asin: String!) {\n"
    "  editions(where: {asin: {_eq: $asin}}, limit: 1) {\n"
    "    id asin release_date audio_seconds\n"
    "    book {" + _BOOK_FIELDS + "    }\n"
    "  }\n"
    "}"
)

SEARCH_QUERY = (
    "query AudiobindSearch($q: String!) {\n"
    '  search(query: $q, query_type: "Book", per_page: 5, page: 1) { results }\n'
    "}"
)

BOOK_QUERY = (
    "query AudiobindBook($id: Int!) {\n"
    "  books(where: {id: {_eq: $id}}, limit: 1) {" + _BOOK_FIELDS + "  }\n"
    "}"
)

ME_QUERY = "query AudiobindMe { me { username } }"


def _str(value) -> str:
    return "" if value is None else str(value).strip()


def _position(value) -> str:
    """1.0 -> "1", 2.5 -> "2.5", None -> ""."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _str(value)
    return str(int(number)) if number.is_integer() else str(number)


def _record_from_book(book: dict, via: str, edition: dict | None = None) -> dict:
    """The JSON-safe record kept in the cache and in a row's detail."""
    authors = []
    for c in book.get("contributions") or []:
        name = _str(((c or {}).get("author") or {}).get("name")) if isinstance(c, dict) else ""
        if name and name not in authors:
            authors.append(name)
    series = ""
    position = ""
    for entry in book.get("book_series") or []:
        if not isinstance(entry, dict):
            continue
        name = _str((entry.get("series") or {}).get("name"))
        if name:
            series, position = name, _position(entry.get("position"))
            if position:
                break  # the first membership with a position is the one worth comparing
    edition_year = _str((edition or {}).get("release_date"))[:4]
    first_year = _str(book.get("release_year"))[:4]
    try:
        audio_seconds = int((edition or {}).get("audio_seconds") or 0)
    except (TypeError, ValueError):
        audio_seconds = 0
    slug = _str(book.get("slug"))
    return {
        "found": True,
        "via": via,
        "id": book.get("id"),
        "title": _str(book.get("title")),
        "authors": authors[:5],
        "series": series,
        "position": position,
        # the edition's own year when the ASIN matched an edition; the book's first publication
        # year otherwise (which is legitimately earlier than an audiobook release)
        "edition_year": edition_year,
        "first_year": first_year,
        "audio_seconds": audio_seconds,
        "url": f"https://hardcover.app/books/{slug}" if slug else "",
    }


NOT_FOUND = {"found": False}


def _by_asin(asin: str, token: str) -> dict | None:
    data = _graphql(ASIN_QUERY, {"asin": asin}, token)
    editions = data.get("editions")
    if not isinstance(editions, list):
        raise BadAnswer("Hardcover's answer had no editions list")
    for edition in editions:
        if isinstance(edition, dict) and isinstance(edition.get("book"), dict):
            return _record_from_book(edition["book"], "asin", edition)
    return None


def _same_book(title: str, authors: list[str], cand_title: str, cand_authors: list[str]) -> bool:
    if title_sim(title, cand_title) < SAME_BOOK_SIM:
        return False
    if authors and cand_authors:
        return any(sim(a, b) >= SAME_BOOK_SIM for a in authors for b in cand_authors)
    return True


def _by_title(title: str, author: str, token: str) -> dict | None:
    """Search Hardcover for title + author; only a hit whose title and author both resemble the
    asked-for ones counts. Then one more request for that book's series membership."""
    data = _graphql(SEARCH_QUERY, {"q": f"{title} {author}".strip()}, token)
    results = (data.get("search") or {}).get("results")
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except ValueError:
            results = None
    hits = (results or {}).get("hits") if isinstance(results, dict) else None
    if not isinstance(hits, list):
        raise BadAnswer("Hardcover's search answer had no hits list")
    wanted_authors = [author] if author else []
    for hit in hits:
        doc = (hit or {}).get("document") if isinstance(hit, dict) else None
        if not isinstance(doc, dict):
            continue
        names = [_str(n) for n in (doc.get("author_names") or []) if _str(n)]
        if not _same_book(title, wanted_authors, _str(doc.get("title")), names):
            continue
        try:
            book_id = int(doc.get("id"))
        except (TypeError, ValueError):
            continue
        books = _graphql(BOOK_QUERY, {"id": book_id}, token).get("books")
        if isinstance(books, list) and books and isinstance(books[0], dict):
            return _record_from_book(books[0], "title")
        return None
    return None


# --- the on-disk cache ----------------------------------------------------------------------

_cache_lock = threading.Lock()


def cache_path() -> Path:
    from .config import CONFIG

    return Path(CONFIG.state_dir) / CACHE_FILE_NAME


def _cache_load() -> dict:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_save(data: dict) -> None:
    path = cache_path()
    if len(data) > CACHE_MAX_ENTRIES:
        keep = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("t", 0), reverse=True)[:CACHE_MAX_ENTRIES]
        data = dict(keep)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written costs a request next time, nothing more


def _cache_get(key: str, now: float) -> dict | None:
    with _cache_lock:
        entry = _cache_load().get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("v"), dict):
        return None
    ttl = FOUND_TTL_SECONDS if entry["v"].get("found") else MISS_TTL_SECONDS
    try:
        if now - float(entry.get("t", 0)) > ttl:
            return None
    except (TypeError, ValueError):
        return None
    return entry["v"]


def _cache_put(key: str, value: dict, now: float) -> None:
    with _cache_lock:
        data = _cache_load()
        data[key] = {"t": now, "v": value}
        _cache_save(data)


def cache_key(asin: str, title: str, author: str) -> str:
    asin = (asin or "").strip().upper()
    if asin:
        return f"asin:{asin}|{norm(title)}|{norm(author)}"
    return f"title:{norm(title)}|{norm(author)}"


# --- the one question -----------------------------------------------------------------------


def lookup(asin: str, title: str, author: str, token: str, now: float | None = None) -> dict:
    """What Hardcover knows about one book: a record (``found`` True) or ``{"found": False}``.

    The ASIN is asked first (an edition-level match); with no edition carrying that ASIN, title +
    author are searched. Cached on disk with a TTL; a failure raises HardcoverError and is never
    cached. ``token`` is required - an empty one raises AuthError before any request.
    """
    if not clean_token(token):
        raise AuthError("no Hardcover token is set")
    now = time.time() if now is None else now
    key = cache_key(asin, title, author)
    cached = _cache_get(key, now)
    if cached is not None:
        return cached
    record = None
    if (asin or "").strip():
        record = _by_asin(asin.strip().upper(), token)
    if record is None and (title or "").strip():
        record = _by_title(title.strip(), (author or "").strip(), token)
    value = record if record is not None else dict(NOT_FOUND)
    _cache_put(key, value, now)
    return value


def test_connection(token: str) -> tuple[bool, str]:
    """(ok, plain message) for Settings > Test connection. One request; nothing is cached."""
    try:
        data = _graphql(ME_QUERY, {}, token)
    except HardcoverError as exc:
        return False, str(exc)
    me = data.get("me")
    user = me[0] if isinstance(me, list) and me else me
    name = _str((user or {}).get("username")) if isinstance(user, dict) else ""
    return True, f"Connected to Hardcover as {name}" if name else "Connected to Hardcover"

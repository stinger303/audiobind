"""The private tracker's acquisition client: the two endpoints the wishlist watcher may automate.

The client does not know which site it talks to. The owner configures the site's URL, the path
of its JSON search endpoint, the path of its torrent download endpoint, the session cookie's name
and its category / freeleech parameters (see ``config.py``, ``TRACKER_*``). This module talks to
exactly those two endpoints:

* search   - the configured search path, POST or GET, answering JSON
* download - the configured download path, ``GET <path>?<param>=<id>`` (plus the configured
  freeleech parameter when a freeleech credit is to be spent)

Nothing else on the site is ever requested, no HTML is ever parsed, and redirects are never
followed (a redirect would be a request to an endpoint nobody authorized). A banned account is
the catastrophic outcome, so every choice here leans conservative.
"""

from __future__ import annotations

import functools
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__
from .util import clean, norm, sim

#: Search answers name the same concept differently from site to site, so every field is read
#: through this table: the first spelling that is present (and not empty) wins. Anything that
#: matches none of them is simply absent - never an error. One table, so a new spelling is a
#: one-line change.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "tid", "torrent_id"),
    "name": ("name", "title"),
    "authors": ("author_info", "authors", "author"),
    "narrators": ("narrator_info", "narrators", "narrator"),
    "series": ("series_info", "series"),
    "size": ("size", "size_bytes"),
    "added": ("added", "added_at", "created"),
    "numfiles": ("numfiles", "files", "file_count"),
    "filetype": ("filetype", "format"),
    "category": ("catname", "category", "cat"),
    "tags": ("tags",),
    "snatched": ("my_snatched", "snatched"),
    "free": ("free", "freeleech"),
    "free_tier": ("fl_vip", "freeleech_vip", "fl_premium"),
    "personal_free": ("personal_freeleech", "personal_fl"),
    "restricted": ("vip", "vip_only", "premium_only"),
    #: the answer's own wrapper around the result list
    "rows": ("data", "results", "torrents"),
}

#: an "error" a search answer starts with when the query simply matched nothing
NO_RESULT_ERRORS = ("nothing returned", "no results", "no torrents")


def _first(entry: dict, concept: str):
    """The value of the first alias of ``concept`` that is present and not empty, else None."""
    for key in FIELD_ALIASES[concept]:
        value = entry.get(key)
        if value is not None and value != "":
            return value
    return None


def _any_flag(entry: dict, concept: str) -> bool:
    return any(flag(entry.get(key)) for key in FIELD_ALIASES[concept])


@dataclass(frozen=True)
class Endpoints:
    """The two URLs this client may ever request, built from configuration."""

    search_url: str = ""
    download_url: str = ""
    search_method: str = "POST"
    download_param: str = "tid"
    freeleech_param: str = ""
    search_category: str = ""
    #: the JSON document template with {{title}} / {{author}} / {{category}} placeholders
    search_body: str = ""
    #: "json" (a JSON body) or "form" (application/x-www-form-urlencoded); POST only
    search_encoding: str = "json"

    @classmethod
    def build(cls, base_url: str, search_path: str, download_path: str, *, search_method: str = "POST",
              download_param: str = "tid", freeleech_param: str = "", search_category: str = "",
              search_body: str = "", search_encoding: str = "json") -> Endpoints:
        base = str(base_url or "").strip().rstrip("/")
        return cls(
            search_url=base + str(search_path or "").strip() if base and search_path else "",
            download_url=base + str(download_path or "").strip() if base and download_path else "",
            search_method=(str(search_method or "").strip().upper() or "POST"),
            download_param=str(download_param or "").strip() or "tid",
            freeleech_param=str(freeleech_param or "").strip(),
            search_category=str(search_category or "").strip(),
            search_body=str(search_body or "").strip(),
            search_encoding=(str(search_encoding or "").strip().lower() or "json"),
        )


_FALSE_WORDS = {"", "0", "false", "no", "off", "null", "none"}


def flag(value) -> bool:
    """A yes/no field read defensively: JSON true/false, 1/0, "1"/"0", "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return False


def has_datetime(value) -> bool:
    """A personal freeleech is a datetime when you hold one, else null.
    An empty string, a zero date or a 0/false also mean "none"."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip()
    if text.lower() in _FALSE_WORDS:
        return False
    return not text.startswith("0000-00-00")


def freeleech_decision(entry: dict, can_spend: bool = True) -> tuple[bool, str]:
    """(spend a freeleech credit?, why) for one search result.

    The torrent already costs nothing when it is freeleech for everyone, freeleech for the
    account's tier, or you hold a personal freeleech on it: download WITHOUT the freeleech
    parameter - a credit spent on those is not refunded. Anything else is downloaded WITH it.
    ``can_spend`` False (no freeleech parameter configured) never spends. Exactly one answer per
    torrent; there is no retry with the other one.
    """
    if _any_flag(entry, "free"):
        return False, "already freeleech (free) - no freeleech credit spent"
    if _any_flag(entry, "free_tier"):
        return False, "already freeleech for this account - no freeleech credit spent"
    if any(has_datetime(entry.get(key)) for key in FIELD_ALIASES["personal_free"]):
        return False, "personal freeleech already held - no freeleech credit spent"
    if not can_spend:
        return False, "not freeleech - no freeleech parameter is configured, downloaded without one"
    return True, "not freeleech - downloaded with a freeleech credit"


def download_url(endpoints: Endpoints, tid, use_freeleech: bool) -> str:
    """The download URL for one torrent id; the freeleech parameter only when ``use_freeleech``
    and one is configured."""
    tid = str(tid).strip()
    if not tid.isdigit():
        raise ValueError("a torrent id is a number")
    url = f"{endpoints.download_url}?{endpoints.download_param}={tid}"
    if use_freeleech and endpoints.freeleech_param:
        url += "&" + endpoints.freeleech_param
    return url


# ---------------------------------------------------------------------------- search request
_PLACEHOLDER_RE = re.compile(r"\{\{(title|author|category)\}\}")
_ONLY_PLACEHOLDERS_RE = re.compile(r"^\s*(?:\{\{(?:title|author|category)\}\}\s*)+$")
_OMIT = object()


def _fill(node, values: dict):
    """One template node with its placeholders substituted. Substituting into parsed JSON strings
    (never into the raw text) means a title with quotes or backslashes can never break the document.
    A string that is nothing but placeholders and comes out empty is _OMIT: its object member / list
    item is left out. A string that mixes literal text with placeholders is trimmed and its runs of
    whitespace collapsed, so "{{title}} {{author}}" with no author is just the title."""
    if isinstance(node, str):
        if not _PLACEHOLDER_RE.search(node):
            return node
        text = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], node)
        if _ONLY_PLACEHOLDERS_RE.match(node):
            return text.strip() or _OMIT
        return " ".join(text.split())
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            filled = _fill(value, values)
            if filled is not _OMIT:
                out[key] = filled
        return out
    if isinstance(node, list):
        return [item for item in (_fill(item, values) for item in node) if item is not _OMIT]
    return node


def build_search_document(template: str, title: str, author: str = "", category: str = "") -> dict:
    """TRACKER_SEARCH_BODY with {{title}}, {{author}} and {{category}} filled in (the category
    from TRACKER_SEARCH_CATEGORY). Rule for empty values: an object member or list item whose value is
    only placeholders, all empty, is omitted - so an unset category (or a row without an author) drops
    its key instead of sending an empty value. An unparseable template raises TrackerError."""
    try:
        parsed = json.loads(template)
    except (TypeError, ValueError):
        raise TrackerError("TRACKER_SEARCH_BODY is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise TrackerError("TRACKER_SEARCH_BODY must be a JSON object")
    values = {"title": " ".join(str(title or "").split()), "author": " ".join(str(author or "").split()),
              "category": str(category or "").strip()}
    return _fill(parsed, values)


def document_params(document: dict) -> list[tuple[str, str]]:
    """The same document as key=value pairs (GET query, or a form-encoded POST): a list repeats its key, a nested object becomes
    ``key[sub]``, true / false are written as such, null is left out."""
    out: list[tuple[str, str]] = []

    def add(key: str, value) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for sub, item in value.items():
                add(f"{key}[{sub}]", item)
        elif isinstance(value, list):
            for item in value:
                add(key, item)
        elif isinstance(value, bool):
            out.append((key, "true" if value else "false"))
        else:
            out.append((key, str(value)))

    for key, value in document.items():
        add(str(key), value)
    return out


# ---------------------------------------------------------------------------- search results
#: a body bigger than this is not a search answer or a .torrent; it is not read further
MAX_BODY = 8 * 1024 * 1024

#: file types an audiobook torrent carries. A result must show one of them (or a category
#: name that says audiobook) before it can be picked - defence in depth, so a category filter
#: that matches the wrong section can never get an e-book downloaded with a freeleech credit
#: the formats preferred over mp3 when two candidates otherwise rank the same
M4_TYPES = {"m4b", "m4a"}
AUDIO_TYPES = {"m4b", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma", "wav", "aax", "mp4"}


@dataclass
class Torrent:
    """One search result, read defensively."""

    tid: str
    name: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    #: [(series name, sequence)]
    series: list[tuple[str, str]] = field(default_factory=list)
    size: str = ""
    added: str = ""
    numfiles: int = 0
    filetype: str = ""
    category: str = ""
    tags: str = ""
    snatched: bool = False
    #: the raw entry - freeleech_decision() reads the freeleech fields from it
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_audio(self) -> bool:
        types = set(re.split(r"[\s,;/|]+", self.filetype.lower())) - {""}
        if types & AUDIO_TYPES:
            return True
        return "audiobook" in self.category.lower().replace(" ", "")

    @property
    def is_m4(self) -> bool:
        """An m4b / m4a source (chapters and one file per book) - preferred over mp3 as the last tie-break."""
        return bool(set(re.split(r"[\s,;/|]+", self.filetype.lower())) & M4_TYPES)


def _json_field(value):
    """Author / narrator / series fields may arrive as JSON *strings*; anything odd is empty."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return {}


def _text(value) -> str:
    return html.unescape(" ".join(str(value).split())) if value is not None else ""


def _names(value) -> list[str]:
    data = _json_field(value)
    items = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for item in items:
        if isinstance(item, (list, tuple)):
            item = item[0] if item else ""
        name = _text(item) if isinstance(item, (str, int, float)) else ""
        if name:
            out.append(name)
    return out


def _series(value) -> list[tuple[str, str]]:
    """The series field: {"<id>": ["Series name", "3"]} (tolerates a bare name or a dict)."""
    data = _json_field(value)
    items = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for item in items:
        if isinstance(item, (list, tuple)):
            name = _text(item[0]) if item else ""
            seq = _text(item[1]) if len(item) > 1 else ""
        elif isinstance(item, dict):
            name, seq = _text(item.get("name") or item.get("title") or ""), _text(item.get("sequence") or "")
        elif isinstance(item, str):
            name, seq = _text(item), ""
        else:
            continue
        if name:
            out.append((name, seq))
    return out


def _int(value) -> int:
    if isinstance(value, (list, tuple)):  # a file list instead of a count
        return len(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _rows_key(payload: dict):
    """The key under which the answer carries its result list (None = this is not a result answer)."""
    for key in FIELD_ALIASES["rows"]:
        if key in payload:
            return key
    return None


def parse_search(payload) -> list[Torrent]:
    """The entries of one search answer ({"data": [...]}). An entry without a numeric id is
    dropped; every other field falls back to empty when it is missing or malformed."""
    key = _rows_key(payload) if isinstance(payload, dict) else None
    rows = payload.get(key) if key else None
    if not isinstance(rows, list):
        return []
    out = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        tid = str(_first(entry, "id") or "").strip()
        if not tid.isdigit():
            continue
        out.append(
            Torrent(
                tid=tid,
                name=_text(_first(entry, "name") or ""),
                authors=_names(_first(entry, "authors")),
                narrators=_names(_first(entry, "narrators")),
                series=_series(_first(entry, "series")),
                size=_text(_first(entry, "size") or ""),
                added=_text(_first(entry, "added") or ""),
                numfiles=_int(_first(entry, "numfiles")),
                filetype=_text(_first(entry, "filetype") or ""),
                category=_text(_first(entry, "category") or ""),
                tags=_text(_first(entry, "tags") or ""),
                snatched=_any_flag(entry, "snatched"),
                raw=entry,
            )
        )
    return out


# ---------------------------------------------------------------------------- picking one
#: a result's title must be at least this similar to the wishlist title (util.sim, after
#: normalisation). Strictly above sim's 0.85 substring floor, so "Dune" never takes
#: "Dune Messiah"
TITLE_MATCH = 0.90
#: an author on the torrent must match a wishlist author this well
AUTHOR_MATCH = 0.90

_BRACKETS_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")


def _title_variants(title: str) -> list[str]:
    """The title as given, without bracketed tags, and without a subtitle after a colon."""
    base = clean(title)
    out = [title, base, _BRACKETS_RE.sub("", title).strip()]
    for text in list(out):
        if ":" in text:
            out.append(text.split(":", 1)[0].strip())
    return [t for t in dict.fromkeys(out) if norm(t)]


def title_score(wanted: str, found: str) -> float:
    """How well a result's name matches a wishlist title. Each side is compared in full against the
    other side's variants (bracketed tags / subtitle dropped) - never both sides shortened at
    once, so "Star Wars: Thrawn" can never match "Star Wars: Heir to the Empire"."""
    best = sim(wanted, found)
    for variant in _title_variants(found):
        best = max(best, sim(wanted, variant))
    for variant in _title_variants(wanted):
        best = max(best, sim(variant, found))
    return best


def author_ok(wanted: list[str], found: list[str]) -> bool:
    if not wanted:
        return True  # nothing to check against
    return any(sim(w, f) >= AUTHOR_MATCH for w in wanted for f in found)


def series_exact(row: dict, torrent: Torrent) -> bool:
    name, seq = str(row.get("series") or ""), norm(str(row.get("sequence") or ""))
    if not name or not seq:
        return False
    return any(sim(name, s) >= TITLE_MATCH and norm(n) == seq for s, n in torrent.series)


def select_candidate(row: dict, torrents: list[Torrent], vip: bool = False, can_spend: bool = True) -> tuple[Torrent | None, str]:
    """(the torrent to fetch, why) or (None, why not) for one wishlist row.

    A candidate must be an audio torrent you have not snatched, whose title scores at least
    TITLE_MATCH and - when the row names authors - credits one of them. Among those: files
    present first, then the exact series/sequence, then the closer title, then one that is
    already freeleech (no credit needed), then an m4b/m4a source over mp3 (only between candidates
    the rules above leave tied), then the tracker's relevance order. Nothing below the
    threshold is ever forced. A torrent restricted to a membership tier is skipped unless ``vip``
    says the account has it; with ``vip`` it is an ordinary candidate.
    """
    if not torrents:
        return None, "no results"
    title = str(row.get("search_title") or row.get("title") or "")
    authors = [str(a) for a in (row.get("authors") or []) if str(a or "").strip()]
    ranked = []
    best_miss = (0.0, "")
    skipped_snatched = 0
    skipped_vip = 0
    for index, torrent in enumerate(torrents):
        score = title_score(title, torrent.name)
        if score < TITLE_MATCH or not author_ok(authors, torrent.authors) or not torrent.is_audio:
            if best_miss[0] < score < TITLE_MATCH:
                best_miss = (score, torrent.name)
            continue
        if torrent.snatched:
            skipped_snatched += 1
            continue
        if not vip and _any_flag(torrent.raw, "restricted"):
            skipped_vip += 1
            continue
        free = not freeleech_decision(torrent.raw, can_spend)[0]
        key = (torrent.numfiles > 0, series_exact(row, torrent), score, free, torrent.is_m4, -index)
        ranked.append((key, torrent, score))
    if not ranked:
        if skipped_vip:
            return None, f"only match(es) need a membership tier this account does not have ({skipped_vip}) - skipped"
        if skipped_snatched:
            return None, f"only match(es) already snatched ({skipped_snatched}) - skipped"
        if best_miss[1]:
            return None, f"closest was {best_miss[1]!r} ({best_miss[0]:.2f}, needs {TITLE_MATCH:.2f})"
        return None, "no result with a matching title, author and audio files"
    ranked.sort(key=lambda r: r[0], reverse=True)
    key, torrent, score = ranked[0]
    bits = [f"title {score:.2f}", f"{torrent.numfiles} file(s)"]
    if key[1]:
        bits.append("series match")
    return torrent, f"#{torrent.tid} {torrent.name!r} ({', '.join(bits)})"


# ---------------------------------------------------------------------------- politeness
#: at least this many seconds between two tracker requests
MIN_SPACING = 3.0
#: backoff after any non-200: 60 s, doubling, capped at 30 minutes
BACKOFF_START = 60.0
BACKOFF_CAP = 1800.0
#: a Retry-After longer than the cap is honoured up to this
RETRY_AFTER_MAX = 6 * 3600.0


class RateLimiter:
    """At least ``spacing`` seconds from the end of one request to the start of the next."""

    def __init__(self, spacing: float = MIN_SPACING, clock=time.monotonic, sleep=time.sleep):
        self.spacing = float(spacing)
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is None:
            return
        gap = self._last + self.spacing - self.clock()
        if gap > 0:
            self.sleep(gap)

    def done(self) -> None:
        self._last = self.clock()


class Backoff:
    """Exponential backoff shared by both endpoints: one failure blocks every request."""

    def __init__(self, start: float = BACKOFF_START, cap: float = BACKOFF_CAP, clock=time.time):
        self.start = float(start)
        self.cap = float(cap)
        self.clock = clock
        self.failures = 0
        self.until = 0.0

    def remaining(self) -> float:
        return max(0.0, self.until - self.clock())

    def fail(self, retry_after: float = 0.0) -> float:
        self.failures += 1
        delay = min(self.cap, self.start * (2 ** (self.failures - 1)))
        if retry_after > delay:
            delay = min(RETRY_AFTER_MAX, retry_after)
        self.until = self.clock() + delay
        return delay

    def ok(self) -> None:
        self.failures = 0
        self.until = 0.0

    def restore(self, until: float, failures: int = 1) -> None:
        """Carry a backoff across a restart (a crash loop must not reset the wait)."""
        if until > self.clock():
            self.until = float(until)
            self.failures = max(self.failures, int(failures or 1))


# ---------------------------------------------------------------------------- the client
class TrackerError(Exception):
    """A tracker request failed. ``status`` is the HTTP status (0 = no answer)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class TrackerBackoff(TrackerError):
    """Refused locally: the tracker is in backoff, so no request was sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: it would be a request to an endpoint nobody authorized."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorized_url(url: str, endpoints: Endpoints) -> bool:
    """True only for the two endpoints configured for automation: the search URL (exactly, or with
    a query string when the search method is GET) and the download URL with its id parameter."""
    if not endpoints.search_url or not endpoints.download_url:
        return False
    if url == endpoints.search_url:
        return True
    if endpoints.search_method == "GET" and url.startswith(endpoints.search_url + "?"):
        return True
    return url.startswith(f"{endpoints.download_url}?{endpoints.download_param}=")


_OPENERS: dict = {}
_OPENERS_LOCK = threading.Lock()


def _opener(proxy: str = ""):
    """The opener for ``proxy`` ("" = direct), built once and shared by search and download.
    An explicit ProxyHandler is always installed: the configured proxy for http and https (an
    https target goes through an HTTP CONNECT tunnel), or an empty map (no proxy at all) when
    there is none, so the environment's proxy variables are never consulted. ``addheaders`` is
    emptied so urllib's own ``User-agent: Python-urllib/x`` is never sent; the only User-Agent
    on the wire is the one TrackerClient sets (``audiobind/<version>`` or TRACKER_USER_AGENT)."""
    with _OPENERS_LOCK:
        if proxy not in _OPENERS:
            mapping = {"http": proxy, "https": proxy} if proxy else {}
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(mapping), _NoRedirect())
            opener.addheaders = []
            _OPENERS[proxy] = opener
        return _OPENERS[proxy]


def urllib_transport(method: str, url: str, data: bytes | None, headers: dict, timeout: float,
                     proxy: str = "", endpoints: Endpoints | None = None):
    """(status, headers, body) - the one place a tracker request leaves the process.

    With ``proxy`` set this is fail-closed: one attempt through the proxy, and any failure is a
    TrackerError. There is no retry and never a direct fallback."""
    if endpoints is None or not authorized_url(url, endpoints):
        raise TrackerError("refusing a URL outside the two configured endpoints")
    opener = _opener(proxy)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except OSError:
            body = b""
        finally:
            exc.close()
        return exc.code, dict(exc.headers or {}), body
    except Exception as exc:  # noqa: BLE001
        if proxy:
            raise TrackerError(f"the request through the proxy failed ({type(exc).__name__}); not sent directly") from exc
        raise


def _retry_after(headers: dict) -> float:
    for key, value in (headers or {}).items():
        if str(key).lower() == "retry-after":
            try:
                return max(0.0, float(str(value).strip()))
            except ValueError:
                return 0.0
    return 0.0


def looks_like_torrent(body: bytes) -> bool:
    """A .torrent is a bencoded dictionary with an info dictionary in it."""
    return bool(body) and len(body) <= MAX_BODY and body[:1] == b"d" and b"4:info" in body


def resolve_user_agent(setting: str = "") -> str:
    """The User-Agent to send for a TRACKER_USER_AGENT setting: empty = ``audiobind/<version>``,
    the literal ``none`` (any case) = "" (send none at all), anything else verbatim."""
    value = str(setting or "").strip()
    if not value:
        return f"audiobind/{__version__}"
    if value.lower() == "none":
        return ""
    return value


class TrackerClient:
    """Search and download - one request at a time, MIN_SPACING apart, with shared backoff.

    ``transport(method, url, data, headers, timeout) -> (status, headers, body)`` is the
    network; tests hand in a fake. Every non-200 (and every answer that is not what the
    endpoint is expected to give) starts or extends the backoff.
    """

    TIMEOUT = 30.0

    def __init__(self, session: str, endpoints: Endpoints, cookie_name: str, transport=None,
                 limiter: RateLimiter | None = None, backoff: Backoff | None = None,
                 proxy: str = "", user_agent: str = ""):
        self._session = str(session or "").strip()
        self._cookie_name = str(cookie_name or "").strip()
        self.endpoints = endpoints
        self._user_agent = resolve_user_agent(user_agent)
        # the default transport carries the proxy; a test's fake transport is used as given
        self.transport = transport or functools.partial(
            urllib_transport, proxy=str(proxy or "").strip(), endpoints=endpoints)
        self.limiter = limiter or RateLimiter()
        self.backoff = backoff or Backoff()
        self._lock = threading.Lock()
        self.requests = 0

    def __repr__(self) -> str:  # never the cookie
        return f"TrackerClient(requests={self.requests}, backoff={self.backoff.remaining():.0f}s)"

    @property
    def can_spend(self) -> bool:
        """True when a freeleech parameter is configured (else a credit is never spent)."""
        return bool(self.endpoints.freeleech_param)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Cookie": f"{self._cookie_name}={self._session}"}
        if self._user_agent:  # empty only when the owner set TRACKER_USER_AGENT=none
            headers["User-Agent"] = self._user_agent
        return {**headers, **(extra or {})}

    def _request(self, method: str, url: str, data: bytes | None = None, extra: dict | None = None) -> bytes:
        if not self._session or not self._cookie_name:
            raise TrackerError("the tracker session is not set (TRACKER_COOKIE_NAME / TRACKER_SESSION)")
        if not authorized_url(url, self.endpoints):
            raise TrackerError("refusing a URL outside the two configured endpoints")
        with self._lock:
            remaining = self.backoff.remaining()
            if remaining > 0:
                raise TrackerBackoff(f"tracker backoff - next request allowed in {int(remaining) + 1}s")
            self.limiter.wait()
            self.requests += 1
            try:
                status, headers, body = self.transport(method, url, data, self._headers(extra), self.TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS: all "no answer"
                self.limiter.done()
                delay = self.backoff.fail()
                raise TrackerError(f"the tracker could not be reached ({type(exc).__name__}); backing off {delay:.0f}s") from exc
            self.limiter.done()
            if status != 200:
                delay = self.backoff.fail(_retry_after(headers))
                hint = ""
                if status == 400 and not self._user_agent:
                    hint = ". Some trackers refuse requests with no User-Agent - set TRACKER_USER_AGENT"
                raise TrackerError(f"the tracker answered HTTP {status}{hint}; backing off {delay:.0f}s", int(status or 0))
            if body is None or len(body) > MAX_BODY:
                delay = self.backoff.fail()
                raise TrackerError(f"the tracker's answer was too large; backing off {delay:.0f}s", 200)
            return body

    def _bad_answer(self, why: str) -> TrackerError:
        delay = self.backoff.fail()
        return TrackerError(f"{why}; backing off {delay:.0f}s", 200)

    def search_document(self, title: str, author: str = "") -> dict:
        """The search request's document: the configured template, filled in for this row."""
        return build_search_document(self.endpoints.search_body, title, author, self.endpoints.search_category)

    def search(self, title: str, author: str = "") -> list[Torrent]:
        """The search endpoint: the configured document in, JSON out."""
        if not self.endpoints.search_body:
            raise TrackerError("the search request is not configured (TRACKER_SEARCH_BODY)")
        document = self.search_document(title, author)
        if self.endpoints.search_method == "GET":
            query = urllib.parse.urlencode(document_params(document))
            body = self._request("GET", f"{self.endpoints.search_url}?{query}", None,
                                 {"Accept": "application/json"})
        elif self.endpoints.search_encoding == "form":
            body = self._request(
                "POST", self.endpoints.search_url,
                urllib.parse.urlencode(document_params(document)).encode(),
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        else:
            body = self._request(
                "POST", self.endpoints.search_url, json.dumps(document).encode(),
                {"Content-Type": "application/json", "Accept": "application/json"},
            )
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            # an HTML page here usually means the session is gone - it is never parsed
            raise self._bad_answer("the tracker's search did not answer JSON (session expired or not authorized?)") from None
        if not isinstance(payload, dict):
            raise self._bad_answer("the tracker's search answered an unexpected JSON shape")
        if _rows_key(payload) is None:
            error = str(payload.get("error") or "")
            if error.lower().startswith(NO_RESULT_ERRORS):
                self.backoff.ok()
                return []  # the tracker's way of saying "no results"
            raise self._bad_answer(f"the tracker's search refused: {error[:120] or 'no data'}")
        self.backoff.ok()
        return parse_search(payload)

    def download(self, tid, use_freeleech: bool) -> bytes:
        """The .torrent bytes. ``use_freeleech`` comes from freeleech_decision()."""
        url = download_url(self.endpoints, tid, use_freeleech)
        body = self._request("GET", url, extra={"Accept": "application/x-bittorrent"})
        if not looks_like_torrent(body):
            raise self._bad_answer("the tracker's download did not return a .torrent")
        self.backoff.ok()
        return body

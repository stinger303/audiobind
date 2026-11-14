"""MyAnonamouse (MAM): the two endpoints the wishlist watcher is allowed to automate.

MAM authorizes automation ONLY on its listed endpoints. This module talks to exactly two:

* Torrent Search JSON - ``POST /tor/js/loadSearchJSONbasic.php``
* Download Torrent    - ``GET  /tor/download.php?tid=<id>`` (``&fl`` spends a freeleech wedge)

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

from .util import clean, norm, sim

#: the site; the two endpoint URLs below are the only URLs this module builds
SITE = "https://www.myanonamouse.net"
SEARCH_URL = SITE + "/tor/js/loadSearchJSONbasic.php"
DOWNLOAD_URL = SITE + "/tor/download.php"

_FALSE_WORDS = {"", "0", "false", "no", "off", "null", "none"}


def flag(value) -> bool:
    """A MAM yes/no field read defensively: JSON true/false, 1/0, "1"/"0", "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return False


def has_datetime(value) -> bool:
    """``personal_freeleech`` is a datetime when you hold a personal freeleech, else null.
    An empty string, a zero date or a 0/false also mean "none"."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip()
    if text.lower() in _FALSE_WORDS:
        return False
    return not text.startswith("0000-00-00")


def wedge_decision(entry: dict) -> tuple[bool, str]:
    """(spend a wedge?, why) for one search result.

    The torrent already costs nothing when it is site-wide freeleech (``free``), VIP
    freeleech (``fl_vip``) or you hold a personal freeleech on it (``personal_freeleech``):
    download WITHOUT ``&fl`` - MAM "will even spend on VIP torrents, no refunds". Anything
    else is downloaded WITH ``&fl``. Exactly one answer per torrent; there is no retry with
    the other one.
    """
    if flag(entry.get("free")):
        return False, "already freeleech (free) - no wedge spent"
    if flag(entry.get("fl_vip")):
        return False, "already freeleech (VIP) - no wedge spent"
    if has_datetime(entry.get("personal_freeleech")):
        return False, "personal freeleech already held - no wedge spent"
    return True, "not freeleech - downloaded with a freeleech wedge (&fl)"


def download_url(tid, use_wedge: bool) -> str:
    """The Download Torrent URL for one torrent id: ``&fl`` only when ``use_wedge``."""
    tid = str(tid).strip()
    if not tid.isdigit():
        raise ValueError("a MAM torrent id is a number")
    return f"{DOWNLOAD_URL}?tid={tid}" + ("&fl" if use_wedge else "")


# ---------------------------------------------------------------------------- search results
#: what the watcher asks the Torrent Search JSON endpoint for (fields as MAM documents them)
SEARCH_LANG_ENGLISH = "1"
#: the audiobook MAIN category, sent as tor[main_cat][]. tor[cat][] takes a LEAF category id
#: and "14" there returns nothing at all ("Nothing returned, out of 0") for every query.
SEARCH_MAIN_CAT_AUDIOBOOKS = "13"
PER_PAGE = 25
#: a body bigger than this is not a search answer or a .torrent; it is not read further
MAX_BODY = 8 * 1024 * 1024

#: file types an audiobook torrent carries. A result must show one of them (or a category
#: name that says audiobook) before it can be picked - defence in depth, so a category filter
#: that matches the wrong section can never get an e-book downloaded with a wedge
AUDIO_TYPES = {"m4b", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma", "wav", "aax", "mp4"}


@dataclass
class Torrent:
    """One Torrent Search JSON entry, read defensively."""

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
    #: the raw entry - wedge_decision() reads free / fl_vip / personal_freeleech from it
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_audio(self) -> bool:
        types = set(re.split(r"[\s,;/|]+", self.filetype.lower())) - {""}
        if types & AUDIO_TYPES:
            return True
        return "audiobook" in self.category.lower().replace(" ", "")


def _json_field(value):
    """author_info / narrator_info / series_info arrive as JSON *strings*; anything odd is empty."""
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
    """series_info: {"<id>": ["Series name", "3"]} (tolerates a bare name or a dict)."""
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
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_search(payload) -> list[Torrent]:
    """The entries of one search answer ({"data": [...]}). An entry without a numeric id is
    dropped; every other field falls back to empty when it is missing or malformed."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        tid = str(entry.get("id") or "").strip()
        if not tid.isdigit():
            continue
        out.append(
            Torrent(
                tid=tid,
                name=_text(entry.get("name") or entry.get("title") or ""),
                authors=_names(entry.get("author_info")),
                narrators=_names(entry.get("narrator_info")),
                series=_series(entry.get("series_info")),
                size=_text(entry.get("size") or ""),
                added=_text(entry.get("added") or ""),
                numfiles=_int(entry.get("numfiles")),
                filetype=_text(entry.get("filetype") or ""),
                category=_text(entry.get("catname") or entry.get("cat") or ""),
                tags=_text(entry.get("tags") or ""),
                snatched=flag(entry.get("my_snatched")),
                raw=entry,
            )
        )
    return out


# ---------------------------------------------------------------------------- picking one
#: a MAM title must be at least this similar to the wishlist title (util.sim, after
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
    """How well a MAM name matches a wishlist title. Each side is compared in full against the
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


def select_candidate(row: dict, torrents: list[Torrent], vip: bool = False) -> tuple[Torrent | None, str]:
    """(the torrent to fetch, why) or (None, why not) for one wishlist row.

    A candidate must be an audio torrent you have not snatched, whose title scores at least
    TITLE_MATCH and - when the row names authors - credits one of them. Among those: files
    present first, then the exact series/sequence, then the closer title, then one that is
    already freeleech (no wedge needed), then MAM's relevance order. Nothing below the
    threshold is ever forced. A VIP-only torrent (``vip`` field) is skipped unless ``vip`` says
    the account has VIP membership; with ``vip`` it is an ordinary candidate.
    """
    if not torrents:
        return None, "no results"
    title = str(row.get("title") or "")
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
        if not vip and flag(torrent.raw.get("vip")):
            skipped_vip += 1
            continue
        free = not wedge_decision(torrent.raw)[0]
        key = (torrent.numfiles > 0, series_exact(row, torrent), score, free, -index)
        ranked.append((key, torrent, score))
    if not ranked:
        if skipped_vip:
            return None, f"only match(es) need VIP membership ({skipped_vip}) - skipped"
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
    return torrent, f"MAM #{torrent.tid} {torrent.name!r} ({', '.join(bits)})"


# ---------------------------------------------------------------------------- politeness
#: at least this many seconds between two MAM requests
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
class MamError(Exception):
    """A MAM request failed. ``status`` is the HTTP status (0 = no answer)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class MamBackoff(MamError):
    """Refused locally: MAM is in backoff, so no request was sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: it would be a request to an endpoint nobody authorized."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorized_url(url: str) -> bool:
    """True only for the two endpoints MAM authorizes for automation."""
    return url == SEARCH_URL or url.startswith(DOWNLOAD_URL + "?tid=")


_OPENERS: dict = {}
_OPENERS_LOCK = threading.Lock()


def _opener(proxy: str = ""):
    """The opener for ``proxy`` ("" = direct), built once and shared by search and download.
    With a proxy only that proxy handler is installed (an https target goes through an HTTP
    CONNECT tunnel); the environment's proxy variables are never consulted."""
    with _OPENERS_LOCK:
        if proxy not in _OPENERS:
            handlers: list = [_NoRedirect()]
            if proxy:
                handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            _OPENERS[proxy] = urllib.request.build_opener(*handlers)
        return _OPENERS[proxy]


def urllib_transport(method: str, url: str, data: bytes | None, headers: dict, timeout: float,
                     proxy: str = ""):
    """(status, headers, body) - the one place a MAM request leaves the process.

    With ``proxy`` set this is fail-closed: one attempt through the proxy, and any failure is a
    MamError. There is no retry and never a direct fallback."""
    if not authorized_url(url):
        raise MamError("refusing a URL outside the two authorized MAM endpoints")
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
            raise MamError(f"MAM request through the proxy failed ({type(exc).__name__}); not sent directly") from exc
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


class MamClient:
    """Search and download - one request at a time, MIN_SPACING apart, with shared backoff.

    ``transport(method, url, data, headers, timeout) -> (status, headers, body)`` is the
    network; tests hand in a fake. Every non-200 (and every answer that is not what the
    endpoint documents) starts or extends the backoff.
    """

    TIMEOUT = 30.0

    def __init__(self, mam_id: str, transport=None, limiter: RateLimiter | None = None,
                 backoff: Backoff | None = None, proxy: str = "", user_agent: str = ""):
        self._mam_id = str(mam_id or "").strip()
        self._user_agent = str(user_agent or "").strip()
        # the default transport carries the proxy; a test's fake transport is used as given
        self.transport = transport or functools.partial(urllib_transport, proxy=str(proxy or "").strip())
        self.limiter = limiter or RateLimiter()
        self.backoff = backoff or Backoff()
        self._lock = threading.Lock()
        self.requests = 0

    def __repr__(self) -> str:  # never the cookie
        return f"MamClient(requests={self.requests}, backoff={self.backoff.remaining():.0f}s)"

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Cookie": f"mam_id={self._mam_id}"}
        if self._user_agent:  # no override unless the owner set MAM_USER_AGENT; never our name or version
            headers["User-Agent"] = self._user_agent
        return {**headers, **(extra or {})}

    def _request(self, method: str, url: str, data: bytes | None = None, extra: dict | None = None) -> bytes:
        if not self._mam_id:
            raise MamError("MAM_ID is not set")
        if not authorized_url(url):
            raise MamError("refusing a URL outside the two authorized MAM endpoints")
        with self._lock:
            remaining = self.backoff.remaining()
            if remaining > 0:
                raise MamBackoff(f"MAM backoff - next request allowed in {int(remaining) + 1}s")
            self.limiter.wait()
            self.requests += 1
            try:
                status, headers, body = self.transport(method, url, data, self._headers(extra), self.TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS: all "no answer"
                self.limiter.done()
                delay = self.backoff.fail()
                raise MamError(f"MAM could not be reached ({type(exc).__name__}); backing off {delay:.0f}s") from exc
            self.limiter.done()
            if status != 200:
                delay = self.backoff.fail(_retry_after(headers))
                raise MamError(f"MAM answered HTTP {status}; backing off {delay:.0f}s", int(status or 0))
            if body is None or len(body) > MAX_BODY:
                delay = self.backoff.fail()
                raise MamError(f"MAM answer too large; backing off {delay:.0f}s", 200)
            return body

    def _bad_answer(self, why: str) -> MamError:
        delay = self.backoff.fail()
        return MamError(f"{why}; backing off {delay:.0f}s", 200)

    def search(self, title: str, author: str = "") -> list[Torrent]:
        """Torrent Search JSON: audiobooks, English, by relevance."""
        form = [
            ("tor[text]", str(title or "")),
            ("tor[searchIn]", "torrents"),
            ("tor[browse_lang][]", SEARCH_LANG_ENGLISH),
            ("tor[main_cat][]", SEARCH_MAIN_CAT_AUDIOBOOKS),
            ("tor[sortType]", "relevance"),
            ("perpage", str(PER_PAGE)),
        ]
        if author:
            form.insert(1, ("tor[author]", str(author)))
        body = self._request(
            "POST", SEARCH_URL, urllib.parse.urlencode(form).encode(),
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            # an HTML page here usually means the session is gone - it is never parsed
            raise self._bad_answer("MAM search did not answer JSON (session expired or not authorized?)") from None
        if not isinstance(payload, dict):
            raise self._bad_answer("MAM search answered an unexpected JSON shape")
        if "data" not in payload:
            error = str(payload.get("error") or "")
            if error.lower().startswith("nothing returned"):
                self.backoff.ok()
                return []  # MAM's way of saying "no results"
            raise self._bad_answer(f"MAM search refused: {error[:120] or 'no data'}")
        self.backoff.ok()
        return parse_search(payload)

    def download(self, tid, use_wedge: bool) -> bytes:
        """Download Torrent: the .torrent bytes. ``use_wedge`` comes from wedge_decision()."""
        url = download_url(tid, use_wedge)
        body = self._request("GET", url, extra={"Accept": "application/x-bittorrent"})
        if not looks_like_torrent(body):
            raise self._bad_answer("MAM download did not return a .torrent")
        self.backoff.ok()
        return body

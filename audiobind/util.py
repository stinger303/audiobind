"""Text normalisation, similarity and filesystem helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

NOISE_RE = re.compile(
    r"\(unabridged\)|\(abridged\)|\[unabridged\]|\[audiobook\]|\[audobook\]|"
    r"\bunabridged\b|\babridged\b|\baudio\s*book\b|\(audiobook\)|\bmp3\b|"
    r"\b\d+\s*kbps\b|\bdisc\s*\d+\b|\bcd\s*\d+\b|\bbook\s*mark\b",
    re.I,
)
def redact_url(url) -> str:
    """``url`` with any login in it (``user:pass@``) shown as ``***@`` - safe for a log line or
    an error message (SEC-05). Everything up to the LAST ``@`` after the scheme is hidden, so a
    password with a ``/`` or ``@`` in it is hidden too."""
    text = str(url or "")
    start = text.find("://")
    start = start + 3 if start >= 0 else 0
    at = text.rfind("@", start)
    return text if at < 0 else text[:start] + "***" + text[at:]


YEAR_RE = re.compile(r"[\(\[]\s*(19|20)\d{2}\s*[\)\]]")
#: a number followed by one of these is the title itself ("13 Hours", "7 Habits"), never a sequence
SEQ_UNIT_WORDS = (
    "hours days minutes seconds weeks months years miles steps rules laws ways things secrets "
    "lessons reasons questions habits"
).split()
# leading track/series number, only when a real title follows it (keeps "1984", "2001").
# "Book 3 Dune" and "3. Dune" / "01 - Dune" always strip; with no separator the number must be
# two digits or a decimal ("20 Make Me", "02 Die Trying", "15.5 Second Son") and must not be
# followed by a unit word, so "13 Hours" and "3 Body Problem" survive
SEQ_RE = re.compile(
    r"^\s*(?:"
    r"book\s*\d{1,3}(?:\.\d)?\s*[-–—.)]*\s*"
    r"|\d{1,3}(?:\.\d)?\s*[-–—.)]+\s*"
    r"|(?:\d{2,3}(?:\.\d)?|\d\.\d)\s*(?!(?:" + "|".join(SEQ_UNIT_WORDS) + r")\b)"
    r")(?=[A-Za-z])",
    re.I,
)
PART_RE = re.compile(r"(?i)(part|pt|disc|disk|cd|vol|volume)\s*\.?\s*\d*")

STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "and",
    "or",
    "with",
    "my",
    "your",
    "how",
    "why",
    "what",
    "when",
    "who",
    "it",
    "that",
    "this",
    "from",
    "at",
    "by",
    "is",
    "are",
    "no",
    "not",
    "up",
    "out",
    "if",
    "into",
    "about",
    "volume",
    "vol",
    "book",
    "series",
    "collection",
    "novels",
    "complete",
    "saga",
    "chronicle",
    "trilogy",
}

SERIES_DIR_RE = re.compile(r"\b(series|saga|chronicle|novels|collection|omnibus)\b", re.I)
COLLECTION_RE = re.compile(
    r"\b(\d{1,2}\s*books?|complete\s+collection|box\s*set|collection|trilogy|"
    r"novels\s+\d+|all\s+\d+\s+books|omnibus)\b",
    re.I,
)


_NORM_APOSTROPHES_RE = re.compile(r"[\u2019'`]")
_NORM_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")  # one space per run, so no whitespace collapse after


def norm(text: str | None) -> str:
    """Lowercase, strip accents and punctuation - the form used for every comparison."""
    if not text:
        return ""
    return _norm(text)


@lru_cache(maxsize=8192)
def _norm(text: str) -> str:
    # pure and called with the same few strings over and over (sim/title_sim normalise the
    # query once per candidate); the cache is bounded, so a long-running daemon cannot grow it
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("_", " ")
    text = _NORM_APOSTROPHES_RE.sub("", text)
    return _NORM_NON_ALNUM_RE.sub(" ", text).strip()


def clean(text: str | None) -> str:
    """Drop rip noise (unabridged, kbps, disc 2...) and tidy separators."""
    text = YEAR_RE.sub(" ", text or "")
    text = NOISE_RE.sub(" ", text)
    text = re.sub(r"[\[\]{}]", " ", text)
    text = re.sub(r"\s*[,;]\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -_.")


def sim(a: str | None, b: str | None) -> float:
    """Similarity in [0, 1], tolerant of word order and series suffixes."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    if at and bt:
        ratio = max(ratio, len(at & bt) / max(len(at), len(bt)) * 0.95)
    if a in b or b in a:
        # containment counts by the share of words it covers, and only for a multi-word side:
        # "Dune" inside "Dune Messiah" is one word of two, not a near-match
        at_, bt_ = a.split(), b.split()
        short, long_ = (at_, bt_) if len(at_) <= len(bt_) else (bt_, at_)
        if len(short) >= 2:
            ratio = max(ratio, 0.85 * len(short) / max(len(long_), 1))
    return round(min(ratio, 1.0), 3)


#: a stopword counts this much in title_sim's weighted Dice; a content word counts 1.0
TITLE_STOPWORD_WEIGHT = 0.2
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+(?=\S)")


def _weighted_dice(a: str, b: str) -> float:
    from collections import Counter

    ca, cb = Counter(a.split()), Counter(b.split())

    def weight(counts: Counter) -> float:
        return sum((TITLE_STOPWORD_WEIGHT if t in STOPWORDS else 1.0) * n for t, n in counts.items())

    total = weight(ca) + weight(cb)
    return 2 * weight(ca & cb) / total if total else 0.0


def title_sim(a: str | None, b: str | None) -> float:
    """Title similarity in [0, 1]: a weighted Dice over words, article- and stopword-aware.

    Stopwords weigh 0.2 and content words 1.0, a repeated word counts once per occurrence, and a
    leading "the" / "a" / "an" may be dropped from both sides ("Midnight Line" ~ "The Midnight
    Line"). The character ratio is a floor, so a typo still scores ("Steeleart" ~ "Steelheart").
    Unlike ``sim`` there is no containment boost: "Dune" is half of "Dune Messiah", not 0.85 of it.
    """
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pairs = [(a, b), (_LEADING_ARTICLE_RE.sub("", a), _LEADING_ARTICLE_RE.sub("", b))]
    value = max(max(_weighted_dice(x, y), SequenceMatcher(None, x, y).ratio()) for x, y in pairs)
    return round(min(value, 1.0), 3)


def natkey(text: str) -> list:
    """Sort key so 'Part 2' precedes 'Part 10'."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


#: a path component's UTF-8 size cap; NAME_MAX is 255 bytes, and the ".m4b" suffix and
#: "NN - " sequence prefix need room
MAX_COMPONENT_BYTES = 200


def truncate_utf8(text: str, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """At most ``max_bytes`` of UTF-8, never splitting a character."""
    raw = text.encode("utf-8", "replace")  # a lone surrogate counts as one byte
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def safe_name(text: str | None, fallback: str = "Unknown") -> str:
    """Make a string safe as a single path component (at most 200 UTF-8 bytes)."""
    out = re.sub(r"[\x00-\x1f\x7f]", " ", text or "")  # a NUL would raise, a newline would split a name
    out = re.sub(r'[\\/:*?"<>|]+', "-", out.strip())
    out = re.sub(r"\s+", " ", out).strip(" .-")
    out = truncate_utf8(out).strip(" .-")
    return out or fallback


def human_hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f}h"


def loads_dict(raw) -> dict:
    """A stored JSON column (detail, overrides) as a dict: {} when empty, unreadable, or valid
    JSON that is not an object (``[]``, ``null``, ``5``) - a hand-edited row must not crash the
    caller's ``.get``. The one copy of what abs_layout, series, discover and the status page
    each used to write out."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

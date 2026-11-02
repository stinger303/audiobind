"""Text normalisation, similarity and filesystem helpers."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

NOISE_RE = re.compile(
    r"\(unabridged\)|\(abridged\)|\[unabridged\]|\[audiobook\]|\[audobook\]|"
    r"\bunabridged\b|\babridged\b|\baudio\s*book\b|\(audiobook\)|\bmp3\b|"
    r"\b\d+\s*kbps\b|\bdisc\s*\d+\b|\bcd\s*\d+\b|\bbook\s*mark\b",
    re.I,
)
YEAR_RE = re.compile(r"[\(\[]\s*(19|20)\d{2}\s*[\)\]]")
# leading track/series number, only when a real title follows it (keeps "1984", "2001")
SEQ_RE = re.compile(r"^\s*(?:book\s*)?\d{1,3}(?:\.\d)?\s*[-–—.)]*\s*(?=[A-Za-z])", re.I)
PART_RE = re.compile(r"(?i)(part|pt|disc|disk|cd|vol|volume)\s*\.?\s*\d*")

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with", "my", "your",
    "how", "why", "what", "when", "who", "it", "that", "this", "from", "at", "by", "is",
    "are", "no", "not", "up", "out", "if", "into", "about", "volume", "vol", "book",
    "series", "collection", "novels", "complete", "saga", "chronicle", "trilogy",
}

SERIES_DIR_RE = re.compile(r"\b(series|saga|chronicle|novels|collection|omnibus)\b", re.I)
COLLECTION_RE = re.compile(
    r"\b(\d{1,2}\s*books?|complete\s+collection|box\s*set|collection|trilogy|"
    r"novels\s+\d+|all\s+\d+\s+books|omnibus)\b",
    re.I,
)


def norm(text: str | None) -> str:
    """Lowercase, strip accents and punctuation - the form used for every comparison."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("_", " ")
    text = re.sub(r"[\u2019'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
        ratio = max(ratio, 0.85)
    return round(min(ratio, 1.0), 3)


def natkey(text: str) -> list:
    """Sort key so 'Part 2' precedes 'Part 10'."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


def safe_name(text: str | None, fallback: str = "Unknown") -> str:
    """Make a string safe as a single path component."""
    out = re.sub(r'[\\/:*?"<>|]+', "-", (text or "").strip())
    out = re.sub(r"\s+", " ", out).strip(" .-")
    return out or fallback


def human_hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f}h"

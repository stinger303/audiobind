"""A book is not a directory: discover book *units* on disk and guess what they are.

A unit is either one directory holding audio, a loose audio file, or several sibling
part/disc folders that together make up a single title (``Book/Part 1``, ``Book/Disc 2``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import (COLLECTION_RE, NOISE_RE, PART_RE, SEQ_RE, SERIES_DIR_RE, STOPWORDS,
                   clean, natkey, norm)

AUDIO_EXT = {".mp3", ".m4b", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}


@dataclass
class Part:
    name: str
    files: list[str]


@dataclass
class BookUnit:
    path: str
    name: str
    parent: str
    parts: list[Part]
    author_guess: str = ""
    title_guess: str = ""
    series_guess: str = ""

    @property
    def rel(self) -> str:
        return self.rel_path

    rel_path: str = ""

    @property
    def files(self) -> list[str]:
        return [f for part in self.parts for f in part.files]

    @property
    def n_files(self) -> int:
        return len(self.files)

    @property
    def multi_part(self) -> bool:
        return len(self.parts) > 1


def audio_files(directory: str) -> list[str]:
    try:
        with os.scandir(directory) as entries:
            found = [e.path for e in entries
                     if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXT]
    except OSError:
        return []
    return sorted(found, key=lambda p: natkey(os.path.basename(p)))


def _skip(name: str, ignore: list[str]) -> bool:
    """Hidden directories are always ignored; the list adds exact names to skip."""
    if name.startswith("."):
        return True
    return any(name == entry for entry in ignore if not entry.startswith("."))


def group_key(name: str) -> str:
    """Text of a folder name with part markers and digits removed.

    Handles CamelCase runs like ``StoneColdUnabridgedPart1`` where \\b-anchored regexes
    never fire: markers and digits are stripped as plain substrings. Returns "" when the
    name was nothing but a marker (``Disc1``), so the caller can key on the parent instead.
    """
    stripped = PART_RE.sub(" ", name)
    stripped = NOISE_RE.sub(" ", stripped)
    stripped = re.sub(r"\d+", " ", stripped)
    return norm(stripped)


def discover(root: str | Path, ignore: list[str]) -> list[dict]:
    """Every directory that directly contains audio, plus loose audio files."""
    root = str(root)
    leaves, loose = [], []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip(d, ignore))
        files = audio_files(dirpath)
        if not files:
            continue
        if os.path.abspath(dirpath) == os.path.abspath(root):
            loose.extend(files)
        else:
            leaves.append({"path": dirpath, "name": os.path.basename(dirpath),
                           "parent": os.path.dirname(dirpath), "files": files})
    for path in loose:
        leaves.append({"path": path, "name": os.path.splitext(os.path.basename(path))[0],
                       "parent": root, "files": [path], "loose": True})
    return leaves


def group(leaves: list[dict]) -> list[BookUnit]:
    """Bucket sibling part directories into single book units."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for leaf in leaves:
        key = group_key(leaf["name"]) or ("g:" + norm(os.path.basename(leaf["parent"])))
        buckets.setdefault((leaf["parent"], key), []).append(leaf)

    units: list[BookUnit] = []
    for (parent, _key), members in buckets.items():
        members.sort(key=lambda m: natkey(os.path.basename(m["path"])))
        single = len(members) == 1
        unit = BookUnit(
            path=members[0]["path"] if single else parent,
            name=(os.path.basename(members[0]["path"]) if single else os.path.basename(parent)),
            parent=parent,
            parts=[Part(name=os.path.basename(m["path"]), files=m["files"]) for m in members],
        )
        units.append(unit)
    units.sort(key=lambda u: u.path)
    return units


def discover_units(root: str | Path, ignore: list[str]) -> list[BookUnit]:
    root = Path(root)
    units = group(discover(root, ignore))
    for unit in units:
        unit.rel_path = os.path.relpath(unit.path, root)
        if unit.rel_path == os.curdir:          # the library root is itself one book
            unit.rel_path = os.path.basename(os.path.abspath(unit.path))
        _guess(unit)
    return units


# ------------------------------------------------------------------ name parsing
def author_like(text: str | None) -> float:
    """0..1 confidence that a string is a person's name, not a title."""
    if not text:
        return 0.0
    text = text.strip()
    if re.search(r"\d", text) or text.isupper():
        return 0.1
    tokens = [t for t in re.split(r"[\s,]+", text) if t]
    if not 1 <= len(tokens) <= 5:
        return 0.05
    if any(t.lower().strip(".") in STOPWORDS for t in tokens):
        return 0.1
    if re.search(r"\b[A-Za-z]\.[A-Za-z]\.?[A-Za-z]?\.?\b", text):
        return 0.15                      # "L.A", "U.S.A" - abbreviations, not people
    score = {1: 0.35, 2: 0.85, 3: 0.8, 4: 0.6, 5: 0.4}.get(len(tokens), 0.1)
    if "," in text and len(tokens) >= 3:
        score = max(score, 0.9)          # "Author One, Author Two"
    capitals = sum(1 for t in tokens if t[:1].isupper())
    if capitals < len(tokens) * 0.6:
        score *= 0.5
    if re.search(r"\b(jr|sr|iii|phd|md)\b", text, re.I):
        score = max(score, 0.8)
    return round(score, 2)


def split_author_title(name: str) -> tuple[str | None, str | None]:
    """Best (author, title) guess from one path segment, correcting 'Title - Author'."""
    text = clean(name)
    text = re.sub(r"^\s*\d{1,3}\s*(?:[-–—]\s*|\.(?!\d)\s*)", "", text)
    if not text:
        return None, None
    text = SEQ_RE.sub("", text)
    match = re.search(r"^(?P<title>.+?)\s+by\s+(?P<author>[^-]+?)(?:\s*\(.*\))?$", text, re.I)
    if match and 1 <= len(match.group("author").split()) <= 5:
        return match.group("author").strip(), match.group("title").strip()
    for sep in (" - ", " \u2013 ", " \u2014 ", " -", "- ", "; ", " ; "):
        if sep in text:
            left, right = text.split(sep, 1)
            left, right = left.strip(" -;"), right.strip(" -;")
            if not left or not right:
                continue
            if author_like(right) - author_like(left) >= 0.15:
                return right, left
            return left, right
    return None, text


def _guess(unit: BookUnit) -> None:
    segments = [p for p in Path(unit.rel_path).parts if p]
    stem = segments[-1] if segments else unit.name
    parent = segments[-2] if len(segments) > 1 else ""
    grandparent = segments[-3] if len(segments) > 2 else ""

    author_stem, title_stem = split_author_title(stem)
    author_parent, title_parent = split_author_title(parent) if parent else (None, None)
    author_grandparent, _ = split_author_title(grandparent) if grandparent else (None, None)

    author = ""
    for candidate in (author_stem, author_parent, author_grandparent):
        if candidate and author_like(candidate) >= 0.3:
            author = candidate
            break
    if not author:
        for candidate in (parent, grandparent):
            if candidate and author_like(clean(candidate)) >= 0.6 and len(clean(candidate).split()) <= 3:
                author = clean(candidate)
                break

    title = title_stem or ""
    if author and title and norm(title) == norm(author) and title_parent:
        title = title_parent
    title = SEQ_RE.sub("", title)
    title = title.strip(" -_,")
    if not title:
        title = clean(title_parent or parent or stem)

    series = ""
    for candidate in (parent, grandparent):
        if candidate and SERIES_DIR_RE.search(candidate) and author_like(candidate) < 0.5:
            series = clean(candidate)
            break

    unit.author_guess = clean(author)
    unit.title_guess = clean(title)
    unit.series_guess = series


def looks_like_collection(unit: BookUnit, avg_hours: float) -> bool:
    """Several whole books sitting in one folder, or a collection-shaped name."""
    if len(unit.parts) > 1 and avg_hours > 8:
        return True
    if 1 < unit.n_files <= 20 and avg_hours > 8:
        return True
    return bool(COLLECTION_RE.search(os.path.basename(unit.path)))

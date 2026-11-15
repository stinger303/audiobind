"""Target layout: where a finished book belongs in the library.

The layout is Audiobookshelf's folder convention: ``<Author>/<Series>/<Book>/`` or
``<Author>/<Book>/``. A book folder inside a series starts ``<sequence> - `` when the catalog
gives a sequence; several authors share one folder name joined with `` & ``. Narrator, ASIN and
series details are carried by the file's tags, so the folder names stay plain.
"""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG
from .util import safe_name

#: how Audiobookshelf separates several authors in a folder name
AUTHOR_JOIN = " & "


def author_names(authors) -> list[str]:
    """Every author, in order, once. An entry holding ``A; B`` counts as two authors."""
    names: list[str] = []
    for raw in authors or []:
        for part in str(raw or "").split(";"):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return names


def destination(series: str, authors: list[str], title: str, sequence: str = "") -> Path:
    """<author>/<title>/, <author>/<series>/<title>/ or <author>/<series>/<seq> - <title>/."""
    author = safe_name(AUTHOR_JOIN.join(author_names(authors)) or "Unknown")
    book_title = safe_name(title or "Unknown")
    # the sequence comes from the catalog or an operator override: it is a path component
    # like every other one, so "/" or ".." in it must never become a directory step
    sequence = safe_name(sequence, fallback="") if sequence else ""
    series_name = safe_name(series, fallback="") if series else ""
    if series_name:
        folder = f"{sequence} - {book_title}" if sequence else book_title
        return CONFIG.output_root / author / series_name / safe_name(folder)
    return CONFIG.output_root / author / book_title


def m4b_path(series: str, authors: list[str], title: str, sequence: str = "", ext: str = "m4b") -> Path:
    directory = destination(series, authors, title, sequence)
    path = directory / f"{safe_name(title or 'Unknown')}.{ext}"
    # belt and braces: whatever the metadata says, the book is written inside OUTPUT_ROOT
    root = Path(CONFIG.output_root).resolve()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise ValueError(f"output path escapes the output root: {path}")
    return path

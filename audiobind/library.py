"""Target layout: where a finished book belongs in the library."""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG
from .util import safe_name


def destination(series: str, authors: list[str], title: str, sequence: str = "") -> Path:
    """<author>/<title>/ or <author>/<series>/<NN> - <title>/ under OUTPUT_ROOT."""
    author = safe_name((authors[0] if authors else "Unknown").split(";")[0])
    book_title = safe_name(title or "Unknown")
    if series and sequence:
        folder = f"{sequence.zfill(2) if sequence.replace('.', '').isdigit() else sequence} - {book_title}"
        return CONFIG.output_root / author / safe_name(series) / folder
    return CONFIG.output_root / author / book_title


def m4b_path(series: str, authors: list[str], title: str, sequence: str = "") -> Path:
    directory = destination(series, authors, title, sequence)
    return directory / f"{safe_name(title or 'Unknown')}.m4b"

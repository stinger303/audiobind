"""Target layout: where a finished book belongs in the library."""

from __future__ import annotations

from pathlib import Path

from .config import CONFIG
from .util import safe_name


def destination(series: str, authors: list[str], title: str, sequence: str = "") -> Path:
    """<author>/<title>/ or <author>/<series>/<NN> - <title>/ under OUTPUT_ROOT."""
    author = safe_name((authors[0] if authors else "Unknown").split(";")[0])
    book_title = safe_name(title or "Unknown")
    # the sequence comes from the catalog or an operator override: it is a path component
    # like every other one, so "/" or ".." in it must never become a directory step
    sequence = safe_name(sequence, fallback="") if sequence else ""
    if series and sequence:
        folder = f"{sequence.zfill(2) if sequence.replace('.', '').isdigit() else sequence} - {book_title}"
        return CONFIG.output_root / author / safe_name(series) / safe_name(folder)
    return CONFIG.output_root / author / book_title


def m4b_path(series: str, authors: list[str], title: str, sequence: str = "") -> Path:
    directory = destination(series, authors, title, sequence)
    path = directory / f"{safe_name(title or 'Unknown')}.m4b"
    # belt and braces: whatever the metadata says, the book is written inside OUTPUT_ROOT
    root = Path(CONFIG.output_root).resolve()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise ValueError(f"output path escapes the output root: {path}")
    return path

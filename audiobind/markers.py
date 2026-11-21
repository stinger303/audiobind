"""Edition markers read from a title's ORIGINAL words, before any bracket removal.

Matching (``matching.py``) and the tracker pick (``tracker.py``) both score titles with their
bracketed groups removed, so "(Abridged)", "[Synopsis]" or "(Box Set)" never weighs on a title
score. These markers are therefore read here, from the untouched strings, and compared on their
own: a folder and a candidate that disagree about one are different products, whatever the titles
score. Pure functions, no catalog calls.
"""

from __future__ import annotations

import re

#: one product holding several books: "Box Set", "Boxed Set", "Omnibus", "The Complete Series",
#: "Books 1-3", "Books 1 to 3"
_SCOPE_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"box(?:ed)?[\W_]*set"
    r"|omnibus"
    r"|complete[\W_]+(?:series|collection|saga|trilogy|set)"
    r"|books?[\W_]*\d{1,3}[\W_]*(?:-|–|—|to|through|thru|&|and)[\W_]*\d{1,3}"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
#: an abridgment ("Unabridged" is the ordinary reading and is not one)
_ABRIDGED_RE = re.compile(r"(?<![a-z0-9])(?<!un)(?<!un-)abridged(?![a-z0-9])", re.IGNORECASE)
#: something written about a book, not an edition of it ("[Synopsis]", "Summary of ...",
#: "A Study Guide", "Workbook")
_DERIVATIVE_RE = re.compile(
    r"(?<![a-z0-9])(?:summar(?:y|ies|i[sz]ed)|synopsis|study[\W_]+guide|workbook)(?![a-z0-9])",
    re.IGNORECASE,
)

SCOPE, ABRIDGED, DERIVATIVE = "box set", "abridged", "summary"
#: how each marker reads in a note ("catalog hit is an abridged edition ...")
LABELS = {
    SCOPE: "a box set / multi-book collection",
    ABRIDGED: "an abridged edition",
    DERIVATIVE: "a summary / synopsis / study guide",
}
_PATTERNS = ((SCOPE, _SCOPE_RE), (ABRIDGED, _ABRIDGED_RE), (DERIVATIVE, _DERIVATIVE_RE))


def edition_markers(*texts: str) -> set[str]:
    """The edition markers (SCOPE, ABRIDGED, DERIVATIVE) any of ``texts`` carries, read from the
    original strings - brackets included."""
    return {name for name, pattern in _PATTERNS if any(pattern.search(t or "") for t in texts)}


def edition_mismatch(wanted: set[str], found: set[str]) -> str:
    """The note for the first marker one side carries and the other does not, else ""."""
    for name, _pattern in _PATTERNS:
        if name in found and name not in wanted:
            return f"catalog hit is {LABELS[name]} and your folder does not say so"
        if name in wanted and name not in found:
            return f"your folder says {LABELS[name]} and the catalog hit is not one"
    return ""

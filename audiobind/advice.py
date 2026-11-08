"""Turn a book's state into "why is it like this" and "what should I do".

Kept separate from the web layer so it can be tested directly and reused by the CLI.
"""
from __future__ import annotations

import re


def _n(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def explain(status: str, confidence: str, note: str = "", detail: dict | None = None) -> tuple[str, str]:
    """Return (why, what_to_do) in plain English for one unit."""
    why, what = _explain_base(status, confidence, note, detail)
    match = (detail or {}).get("match") or {}
    pin = str(match.get("asin_pin_failed") or "").strip()
    if pin:
        why = (f"Your pinned ASIN {pin} has no catalog data - Audible serves nothing for it - "
               "so the catalog search result was used. " + why)
        what = (what + " The pin stays saved - re-pin with a live ASIN to replace it.")
    return why, what


def _explain_base(status: str, confidence: str, note: str = "", detail: dict | None = None) -> tuple[str, str]:
    """The status-specific explanation, without the pinned-ASIN annotation."""
    detail = detail or {}
    match = detail.get("match") or {}
    src = detail.get("source") or {}
    hours = _n(src.get("hours")) or _n(detail.get("hours"))
    catalog_hours = _n(match.get("hours"))
    note = (note or "").strip()

    # rows recorded before the detail blob existed keep both numbers in the note
    if not catalog_hours:
        found = re.search(r"catalog\s+([\d.]+)\s*h", note, re.I)
        catalog_hours = _n(found.group(1)) if found else 0.0
    if not hours:
        found = re.search(r"runtime\s+([\d.]+)\s*h", note, re.I)
        hours = _n(found.group(1)) if found else 0.0

    if status == "converted":
        out = detail.get("output") or {}
        chapters = out.get("chapters")
        tail = f" with {chapters} chapters" if chapters else ""
        return (f"Converted and published{tail}.",
                "Nothing to do - it is in your output library.")

    if status == "ready":
        return ("Everything checked out, but dry run is on so nothing was written.",
                "Turn Dry run off, then either press Scan now or use Convert anyway on this row.")

    if status == "interrupted":
        return ("Encoding was stopped before it finished. The partial file was discarded, so "
                "nothing half-written reached the library.",
                "Press Retry now - it starts again from scratch.")

    if status == "failed":
        reason = note or "the encoder returned an error"
        return (f"The conversion failed: {reason}",
                "Check the source files first (a damaged file is the usual cause), then Retry now.")

    if status == "held":
        if catalog_hours and hours:
            delta = hours - catalog_hours
            pct = abs(delta) / catalog_hours * 100
            why = (f"Your files run {hours:.2f} h but Audible lists {catalog_hours:.2f} h for this "
                   f"edition - {pct:.0f}% {'shorter' if delta < 0 else 'longer'}.")
        else:
            why = note or "The runtime does not match the catalog entry."
        return (why + " That usually means a different edition (radio drama, abridged, "
                "omnibus) or missing parts.",
                "If you are sure the files are complete, use Convert anyway. If the catalog entry "
                "is the wrong edition, paste the correct ASIN and press Retry now.")

    if status == "review":
        lower = note.lower()
        unreadable = re.search(r"(\d+)\s+unreadable", lower)
        if unreadable:
            return (f"{unreadable.group(1)} source file(s) cannot be decoded by ffmpeg.",
                    "Replace or re-rip those files, then press Retry now. Convert anyway will "
                    "also fail until the file is fixed.")
        if confidence == "NONE":
            return ("No Audible entry matched this title, so there is nothing trustworthy to "
                    "label the book with.",
                    "Find it on Audible and paste its ASIN (best), then Retry now. Convert anyway "
                    "will convert it using the folder name only.")
        if confidence == "COLLECTION":
            return ("This folder looks like several books at once rather than a single title.",
                    "Split it into one folder per book and press Scan now, or pin this folder to "
                    "one book with its ASIN and use Convert anyway.")
        if confidence == "LOW":
            return ("The automatic match was not confident enough to trust - the folder name and "
                    "the catalog entry disagree.",
                    "Paste the correct ASIN and press Retry now. If you know the title is right, "
                    "Convert anyway skips the check.")
        return (note or "This book needs a decision before it is converted.",
                "Retry now after fixing the source, or Convert anyway to proceed as it is.")

    return (note or "No state recorded yet.",
            "Press Scan now to assess this book.")

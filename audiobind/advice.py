"""Turn a book's state into "why is it like this" and "what should I do".

Kept separate from the web layer so it can be tested directly and reused by the CLI.

The copy here is operator-facing: plain sentences that say what happened, why the
book is in this state, and what pressing each button will do. Approved wording,
v0.12.3 - keep new strings in the same voice.
"""

from __future__ import annotations

import re


def _n(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def explain(
    status: str, confidence: str, note: str = "", detail: dict | None = None, tier: str = "high"
) -> tuple[str, str]:
    """Return (why, what_to_do) in plain English for one unit."""
    why, what = _explain_base(status, confidence, note, detail, tier)
    match = (detail or {}).get("match") or {}
    pin = str(match.get("asin_pin_failed") or "").strip()
    if pin:
        why = (
            f"Your pinned ASIN {pin} is not in Audible's catalog - nothing is published "
            f"under it - so nothing was matched. {why}"
        )
        what = what + " The pin stays saved until you replace it with a live ASIN."
    return why, what


def _explain_base(
    status: str, confidence: str, note: str = "", detail: dict | None = None, tier: str = "high"
) -> tuple[str, str]:
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
        return (f"Converted and filed in your library{tail}.", "Nothing to do. It's already on your shelf.")

    if status == "ready":
        return (
            "This book passed every check, but Dry Run is on, so nothing was actually written.",
            "Turn Dry Run off in Settings, then press Scan now - or hit Convert anyway on this row.",
        )

    if status == "interrupted":
        return (
            "The encoding was stopped partway through. The half-written file was thrown "
            "away, so nothing broken reached your library.",
            "Press Retry now - it starts over from the beginning.",
        )

    if status == "failed":
        reason = note or "the encoder returned an error"
        return (
            f"This one failed: {reason}. A damaged source file is the usual suspect.",
            "Check the source files, then press Retry now.",
        )

    if status == "held":
        if catalog_hours and hours:
            delta = hours - catalog_hours
            pct = abs(delta) / catalog_hours * 100
            why = (
                f"Your files run {hours:.2f} hours, but Audible lists {catalog_hours:.2f} for "
                f"this edition - {pct:.0f}% {'longer' if delta > 0 else 'shorter'}."
            )
        else:
            why = note or "The runtime does not match the catalog entry."
        return (
            why + " That gap usually means a different edition (radio drama, abridged, "
            "omnibus) or missing files.",
            "Sure the files are complete? Press Convert anyway. Wrong edition in the "
            "catalog? Paste the right ASIN and press Retry now.",
        )

    if status == "review":
        lower = note.lower()
        unreadable = re.search(r"(\d+)\s+unreadable", lower)
        if unreadable:
            return (
                f"{unreadable.group(1)} source files are damaged or in a format we can't read.",
                "Replace or re-rip those files, then press Retry now. Convert anyway won't "
                "help - it hits the same broken file.",
            )
        if confidence == "NONE":
            return (
                "We couldn't find this book on Audible, so there's nothing trustworthy to label it with.",
                "Find it on Audible and paste its ASIN, then press Retry now. Or Convert "
                "anyway to go with the folder name.",
            )
        if confidence == "COLLECTION":
            return (
                "This folder looks like several books bundled together, not one title.",
                "Split it into one folder per book and press Scan now - or pin it to one "
                "book with its ASIN and Convert anyway.",
            )
        if confidence == "LOW":
            return (
                "The match we found isn't trustworthy - the folder name and the Audible entry disagree.",
                "Paste the correct ASIN and press Retry now. Sure the title is right? "
                "Convert anyway skips the check.",
            )
        low_tier = (tier or "high").lower()
        if low_tier == "off":
            return (
                "We found the Audible entry for this book and it matched, but "
                "auto-convert is off - every conversion waits for your OK.",
                "Cover and title below look right? Press Convert anyway. Want it "
                "locked in? Paste the ASIN and press Use this ASIN.",
            )
        return (
            note
            or (
                "We found the Audible entry for this book, but the match is only "
                "Medium confidence - the folder name and the listing don't line up "
                f"exactly. Auto-convert is set to {low_tier.capitalize()}, so anything less than a sure "
                "match waits for your OK."
            ),
            "Cover and title below look right? Press Convert anyway. Want it locked in? "
            "Paste the ASIN and press Use this ASIN.",
        )

    return (note or "We haven't looked at this book yet.", "Press Scan now and we'll take a look.")

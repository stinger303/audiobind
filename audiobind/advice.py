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


def _hours_pair(note: str, detail: dict | None) -> tuple[float, float]:
    """(your files' hours, the catalog's hours); 0.0 for whichever is unknown."""
    detail = detail or {}
    match = detail.get("match") or {}
    src = detail.get("source") or {}
    hours = _n(src.get("hours")) or _n(detail.get("hours"))
    catalog_hours = _n(match.get("hours"))
    note = note or ""
    # rows recorded before the detail blob existed keep both numbers in the note
    if not catalog_hours:
        found = re.search(r"catalog\s+([\d.]+)\s*h", note, re.I)
        catalog_hours = _n(found.group(1)) if found else 0.0
    if not hours:
        found = re.search(r"runtime\s+([\d.]+)\s*h", note, re.I)
        hours = _n(found.group(1)) if found else 0.0
    return hours, catalog_hours


def _unreadable(note: str, detail: dict | None) -> tuple[int, int]:
    """(unreadable files, total source files) from the note; 0 for whichever is unknown."""
    found = re.search(r"(\d+)\s+unreadable", note or "", re.I)
    if not found:
        return 0, 0
    src = (detail or {}).get("source") or {}
    try:
        total = int(src.get("files") or 0)
    except (TypeError, ValueError):
        total = 0
    return int(found.group(1)), max(total, 0)


# --- the one-line WHY cell and the "Needs you" groups (0.21.0) -----------------------------

#: the statuses that wait on the operator: the "Needs you" view lists exactly these
ATTENTION_STATUSES = ("failed", "held", "interrupted", "review")

#: status -> group title in the "Needs you" view
GROUP_TITLES = {"failed": "Failed", "held": "Held", "interrupted": "Stopped", "review": "Review"}

_RUNTIME_HEADER = (
    "Your files and Audible's runtime disagree by more than your 25 % tolerance. That gap "
    "usually means a different edition (radio drama, abridged, omnibus) or missing files."
)

#: short-reason key -> (group title, the one sentence under the group header)
GROUP_REASONS = {
    "damaged_files": (
        "Review",
        "Some of your source files can't be read. Replace or re-rip them, then press Retry now.",
    ),
    "not_on_audible": (
        "Review",
        "We couldn't find these books on Audible. Paste the right ASIN, or convert them with "
        "the folder name.",
    ),
    "collection": (
        "Review",
        "These folders look like several books bundled together. Split them into one folder per book.",
    ),
    "low_match": (
        "Review",
        "The folder name and the Audible entry disagree on these books. Check the match before "
        "anything is written.",
    ),
    "medium_match": (
        "Review",
        "We found these books on Audible, but the match isn't sure enough to convert on its own. "
        "Check the cover and title, then convert.",
    ),
    "check_match": ("Review", "These books are waiting for your OK before they convert."),
    "unsafe_name": (
        "Held",
        "A file or folder name has a line break or another unsafe character in it. Rename it on "
        "the share, then scan again.",
    ),
    "no_catalog_match": (
        "Held",
        "Nothing in the catalog looked like these folders. Find the book on Audible and paste its ASIN.",
    ),
    "runtime_short": ("Held", _RUNTIME_HEADER),
    "runtime_long": ("Held", _RUNTIME_HEADER),
    "runtime_gap": ("Held", _RUNTIME_HEADER),
    "encoder_error": (
        "Failed",
        "The encoder gave up on these books. A damaged source file is the usual suspect.",
    ),
    "stopped": (
        "Stopped",
        "These conversions were stopped part way. Nothing broken reached your library; Retry "
        "now starts them over.",
    ),
}


def group_order() -> tuple:
    """The "Needs you" groups, most severe first."""
    return ("failed", "held", "interrupted", "review")


def short_reason(status, confidence="", note="", detail=None) -> tuple[str, str]:
    """(key, label) for the one-line WHY cell. Label is 2-6 words, plain words, no codes."""
    status = (status or "").lower()
    confidence = (confidence or "").upper()
    note = (note or "").strip()
    lower = note.lower()
    if status == "failed":
        return "encoder_error", "Encoder error"
    if status == "interrupted":
        return "stopped", "Stopped part way"
    if status == "review":
        if re.search(r"\d+\s+unreadable", lower):
            return "damaged_files", "Damaged files"
        if confidence == "NONE":
            return "not_on_audible", "Not on Audible"
        if confidence == "COLLECTION":
            return "collection", "Looks like a collection"
        if confidence == "LOW":
            return "low_match", "Low match"
        if confidence == "MEDIUM":
            return "medium_match", "Medium match"
        return "check_match", "Check the match"
    if status == "held":
        if lower.startswith("unsafe file name"):
            return "unsafe_name", "Unsafe file name"
        if lower.startswith("no catalog match"):
            return "no_catalog_match", "No catalog match"
        if re.search(r"\d+\s+unreadable", lower):
            # a damaged rip, not an edition question (0.21.0: never blamed on the edition)
            return "damaged_files", "Damaged files"
        hours, catalog_hours = _hours_pair(note, detail)
        if hours and catalog_hours and hours != catalog_hours:
            pct = round(abs(hours - catalog_hours) / catalog_hours * 100)
            if hours < catalog_hours:
                return "runtime_short", f"Runtime {pct}% short"
            return "runtime_long", f"Runtime {pct}% long"
        return "runtime_gap", "Runtime differs"
    return "converted", ""


def reason_numbers(status, confidence="", note="", detail=None) -> str:
    """The specific numbers beside the WHY label ("1.38 h vs 7.72 h · 82% short"); may be empty."""
    key, _label = short_reason(status, confidence, note, detail)
    hours, catalog_hours = _hours_pair(note or "", detail)
    both = bool(hours and catalog_hours)
    pair = f"{hours:.2f} h vs {catalog_hours:.2f} h" if both else ""
    if key in ("runtime_short", "runtime_long"):
        return pair
    if key == "damaged_files":
        bad, _total = _unreadable(note or "", detail)
        parts = [f"{bad} file{'' if bad == 1 else 's'} unreadable"] if bad else []
        if both and (status or "").lower() == "held":
            delta = hours - catalog_hours
            pct = round(abs(delta) / catalog_hours * 100)
            parts += [pair, f"{pct}% {'long' if delta > 0 else 'short'}"]
        return " · ".join(parts)
    return ""


def explain(
    status: str, confidence: str, note: str = "", detail: dict | None = None, tier: str = "high"
) -> tuple[str, str]:
    """Return (why, what_to_do) in plain English for one unit."""
    why, what = _explain_base(status, confidence, note, detail, tier)
    match = (detail or {}).get("match") or {}
    pin = str(match.get("asin_pin_failed") or "").strip()
    if pin:
        why = (
            f"The ASIN you set ({pin}) is not in Audible's catalog - nothing is published "
            f"under it - so nothing was matched. {why}"
        )
        what = what + " The ASIN you set stays saved until you replace it with a live one."
    return why, what


def _explain_base(
    status: str, confidence: str, note: str = "", detail: dict | None = None, tier: str = "high"
) -> tuple[str, str]:
    """The status-specific explanation, without the hand-set-ASIN annotation."""
    detail = detail or {}
    note = (note or "").strip()
    hours, catalog_hours = _hours_pair(note, detail)

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

    if status == "held" and note.startswith("unsafe file name"):
        return (
            f"Held: {note[len('unsafe file name: '):] or 'a file or folder name is unsafe'}. "
            "Audiobind won't hand a name with a line break or NUL byte to ffmpeg - it could "
            "smuggle extra instructions into the merge.",
            "Rename the file or folder on the share (no line breaks), then press Scan now.",
        )

    if status == "held" and note.startswith("no catalog match"):
        return (
            f"{note[0].upper()}{note[1:]}. Nothing in the catalog looked like this folder's title, "
            "so no other book's title was put on it.",
            "Find the book on Audible and paste its ASIN, then press Retry now.",
        )

    if status == "held":
        options = (
            "Sure the files are complete? Press Convert anyway. Wrong edition in the "
            "catalog? Paste the right ASIN and press Retry now."
        )
        bad, total = _unreadable(note, detail)
        if bad:
            # a damaged rip is the cause, not the edition: say so and never blame the edition
            if bad == 1 and total:
                lead = f"One of your {total} source files can't be read"
            elif bad > 1 and total:
                lead = f"{bad} of your {total} source files can't be read"
            elif bad > 1:
                lead = f"{bad} source files can't be read"
            else:
                lead = "One source file can't be read"
            if catalog_hours and hours:
                delta = hours - catalog_hours
                pct = abs(delta) / catalog_hours * 100
                why = (
                    f"{lead}, which probably explains why your files run {hours:.2f} hours when "
                    f"Audible lists {catalog_hours:.2f} for this edition "
                    f"({pct:.0f}% {'longer' if delta > 0 else 'shorter'})."
                )
            else:
                why = f"{lead}."
            return why, options
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
            options,
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
                "Split it into one folder per book and press Scan now - or set it to one "
                "exact Audible entry with its ASIN and Convert anyway.",
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
                "Medium Confidence - the folder name and the listing don't line up "
                f"exactly. Auto-convert is set to {low_tier.capitalize()}, so anything less than a sure "
                "match waits for your OK."
            ),
            "Cover and title below look right? Press Convert anyway. Want it locked in? "
            "Paste the ASIN and press Use this ASIN.",
        )

    return (note or "We haven't looked at this book yet.", "Press Scan now and we'll take a look.")

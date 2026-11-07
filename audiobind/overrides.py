"""Operator metadata corrections, stored per unit and applied at conversion time.

The catalog is usually right; when it is not, the operator types the correction into the detail
panel and it is kept in the ``overrides`` column of the units table - the same mechanism as the
existing ASIN override, which it sits beside.

Overrides win over both the Audnex book payload and the Audible product, because they are the
only field a human looked at. They are applied to the ``book`` dict before anything is written,
so the tags, ``metadata.json`` and the author/title filing layout all agree.
"""
from __future__ import annotations

from copy import deepcopy

#: the fields the panel can edit; "authors" is a list, the rest are plain strings
FIELDS = ("title", "authors", "narrator", "series", "series_index")
#: one field cannot be allowed to become a 4 KB filename
MAX_LENGTH = 300
#: several authors or narrators in one input, separated by a semicolon
SEPARATOR = ";"


def split_names(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    else:
        values = str(raw or "").split(SEPARATOR)
    return [name.strip()[:MAX_LENGTH] for name in values if name and name.strip()]


def normalise(raw) -> dict:
    """Keep only known, non-empty fields; trim whitespace and absurd lengths.

    An empty value means "no override" rather than "blank it out", so clearing a box in the UI
    hands the field back to the catalog.
    """
    if not isinstance(raw, dict):
        return {}
    clean: dict = {}
    for field in FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if field == "authors":
            names = split_names(value)
            if names:
                clean["authors"] = names
            continue
        text = str(value if value is not None else "").strip()[:MAX_LENGTH]
        if text:
            clean[field] = text
    return clean


def custom_fields(data: dict) -> set[str]:
    """Which fields carry an override, for the "custom" marker in the UI."""
    return {field for field in FIELDS if (data or {}).get(field)}


def apply(book: dict, data: dict) -> dict:
    """Return a copy of an Audnex ``book`` payload with the overrides written in.

    Shaped exactly like the catalog payload, so every existing reader (write_ffmetadata,
    write_abs_metadata, the filing layout) picks the override up without knowing it exists.
    """
    data = normalise(data or {})
    merged = deepcopy(book or {})
    if not data:
        return merged

    if data.get("title"):
        merged["title"] = data["title"]
    if data.get("authors"):
        merged["authors"] = [{"name": name} for name in data["authors"]]
    if data.get("narrator"):
        merged["narrators"] = [{"name": name} for name in split_names(data["narrator"])]

    if data.get("series") or data.get("series_index"):
        series = dict(merged.get("seriesPrimary") or {})
        if data.get("series"):
            series["name"] = data["series"]
        if data.get("series_index"):
            series["position"] = data["series_index"]
        # a series index with no series anywhere is meaningless - drop it rather than write junk
        if series.get("name"):
            merged["seriesPrimary"] = series
    return merged


def describe(data: dict) -> dict:
    """Flatten overrides for display: every field as the string the operator typed."""
    data = normalise(data or {})
    return {
        "title": data.get("title", ""),
        "authors": SEPARATOR.join(data.get("authors", [])) if data.get("authors") else "",
        "narrator": data.get("narrator", ""),
        "series": data.get("series", ""),
        "series_index": data.get("series_index", ""),
    }

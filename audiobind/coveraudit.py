"""Inspect published cover sidecars without fetching or decoding images."""

from __future__ import annotations

import hashlib
import struct
from collections import defaultdict
from pathlib import Path
from typing import BinaryIO

from .covers import published_cover
from .drift import published_units
from .state import State

ROUTE = "/api/library/covers"
CLASSES = ("ok", "missing", "too_small", "not_square", "unreadable")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# C4, C8 and CC are DHT, JPG and DAC, not start-of-frame markers.
JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_dimensions(image: BinaryIO) -> tuple[int, int] | None:
    while image.read(1) == b"\xff":
        marker = image.read(1)
        while marker == b"\xff":
            marker = image.read(1)
        if not marker or marker in (b"\x00", b"\xd9", b"\xda"):
            break
        if marker == b"\x01":
            continue  # TEM is a standalone marker
        size = int.from_bytes(image.read(2), "big")
        if size < 2:
            break
        if marker[0] in JPEG_SOF:
            if size < 8:
                break
            _, height, width = struct.unpack(">BHH", image.read(5))
            return width, height
        image.seek(size - 2, 1)
    return None


def dimensions(path: Path) -> tuple[int, int] | None:
    """Read PNG or JPEG dimensions from the header, or None if unavailable."""
    try:
        with path.open("rb") as image:
            if image.read(2) == b"\xff\xd8":
                size = _jpeg_dimensions(image)
            else:
                image.seek(0)
                if image.read(16) != PNG_SIGNATURE + b"\x00\x00\x00\rIHDR":
                    return None
                size = struct.unpack(">II", image.read(8))
    except (OSError, struct.error):
        return None
    return size if size and all(size) else None


def _md5(path: Path) -> str | None:
    digest = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as image:
            for block in iter(lambda: image.read(65536), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _classification(size: tuple[int, int] | None) -> str:
    if size is None:
        return "unreadable"
    width, height = size
    if min(size) < 500:
        return "too_small"
    if not 9 * height <= 10 * width <= 11 * height:
        return "not_square"
    return "ok"


def report(state: State) -> dict:
    counts = dict.fromkeys(CLASSES, 0)
    offenders = []
    cached = {}
    shared = defaultdict(list)
    for unit in published_units(state):
        entry = {"path": unit.path}
        if unit.asin:
            entry["asin"] = unit.asin
        try:
            cover = published_cover(state, unit.path)
        except (OSError, RuntimeError):
            classification = "unreadable"
        else:
            classification = "missing"
            if cover is not None:
                if cover not in cached:
                    cached[cover] = dimensions(cover), _md5(cover)
                size, digest = cached[cover]
                classification = _classification(size)
                entry["cover"] = str(cover)
                if size:
                    entry["width"], entry["height"] = size
                if digest and unit.asin:
                    shared[digest].append(entry)
        counts[classification] += 1
        if classification != "ok":
            offenders.append({**entry, "classification": classification})

    duplicates = [
        {"md5": digest, "units": entries}
        for digest, entries in sorted(shared.items())
        if len({entry["asin"].strip().upper() for entry in entries} - {""}) > 1
    ]
    return {"counts": counts, "offenders": offenders, "duplicates": duplicates}

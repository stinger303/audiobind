"""The one thing every adapter needs from a .torrent file: its v1 info hash (no client involved)."""

from __future__ import annotations

import hashlib


def _skip(data: bytes, i: int) -> int:
    """Index just past the bencoded value that starts at ``i``."""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            if not data[i:i + 1]:
                raise ValueError("truncated")
            i = _skip(data, i)
        return i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        end = colon + 1 + int(data[i:colon])
        if end > len(data):
            raise ValueError("truncated")
        return end
    raise ValueError("not bencode")


def info_hash(torrent: bytes) -> str:
    """The v1 info hash (SHA-1 of the bencoded info dictionary), or "" when unreadable."""
    try:
        if torrent[:1] != b"d":
            return ""
        i = 1
        while torrent[i:i + 1] != b"e":
            key_end = _skip(torrent, i)
            colon = torrent.index(b":", i)
            key = torrent[colon + 1:key_end]
            value_end = _skip(torrent, key_end)
            if key == b"info":
                return hashlib.sha1(torrent[key_end:value_end]).hexdigest()  # noqa: S324 - BitTorrent v1
            i = value_end
    except (ValueError, IndexError, RecursionError):
        return ""
    return ""


def is_hex_hash(value) -> bool:
    """A v1 (40) or v2 (64) hex info hash."""
    return isinstance(value, str) and len(value) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in value)

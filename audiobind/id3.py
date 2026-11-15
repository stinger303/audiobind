"""Write ID3v2.3 CHAP/CTOC chapter frames into an mp3, standard library only.

ffmpeg silently drops chapters when it writes an mp3, so after the encode we add them to the
tag ffmpeg already wrote: the existing frames (TIT2, TPE1, APIC ...) are kept byte for byte,
one CHAP frame per chapter and one CTOC frame are appended, and the tag's size is rewritten.
ffprobe ``-show_chapters`` reads the result.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

NO_OFFSET = 0xFFFFFFFF
_COPY_BLOCK = 1024 * 1024
_FLAG_UNSYNC = 0x80
_FLAG_EXTENDED = 0x40


def syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def unsyncsafe(raw: bytes) -> int:
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def frame(frame_id: str, body: bytes) -> bytes:
    """ID3v2.3 frame: id + 4-byte plain (not syncsafe) size + 2 flag bytes + body."""
    return frame_id.encode("latin-1") + struct.pack(">I", len(body)) + b"\x00\x00" + body


def text_frame(frame_id: str, text: str) -> bytes:
    """A text frame in the narrowest encoding v2.3 allows: latin-1, else UTF-16 with a BOM."""
    text = text.replace("\x00", " ")
    try:
        return frame(frame_id, b"\x00" + text.encode("latin-1"))
    except UnicodeEncodeError:
        return frame(frame_id, b"\x01\xff\xfe" + text.encode("utf-16-le") + b"\x00\x00")


MAX_CTOC_CHILDREN = 255


def _ms(seconds) -> int:
    return max(0, min(int(round(float(seconds) * 1000)), 0xFFFFFFFE))


def chapter_frames(chapters: list[dict]) -> bytes:
    """One CHAP per chapter (element ids ch1..chN) and a CTOC listing them in order."""
    out = b""
    ids = []
    for index, chapter in enumerate(chapters, start=1):
        element = f"ch{index}"
        ids.append(element)
        body = element.encode("latin-1") + b"\x00"
        body += struct.pack(">IIII", _ms(chapter["start"]), _ms(chapter["end"]), NO_OFFSET, NO_OFFSET)
        body += text_frame("TIT2", str(chapter.get("title") or element))
        out += frame("CHAP", body)
    # CTOC body: element id "toc" NUL, flags byte, child count, then each child id NUL-terminated.
    # Flags 0x03 = top-level (bit 1) + ordered (bit 0). Players treat a CTOC without the
    # top-level bit as a nested table and ignore it as the chapter list; 0x00 (the
    # prototype's value) therefore hides the chapters.
    # The count is one byte: list the first MAX_CTOC_CHILDREN and stop (a wrapped count would
    # claim 0 children while every id was still written). Every CHAP frame is still written.
    listed = ids[:MAX_CTOC_CHILDREN]
    toc = b"toc\x00" + b"\x03" + struct.pack(">B", len(listed))
    for element in listed:
        toc += element.encode("latin-1") + b"\x00"
    return out + frame("CTOC", toc)


def read_header(head: bytes):
    """(major, flags, tag_body_size) of an ID3v2 header, or None when ``head`` is not one."""
    if len(head) < 10 or head[:3] != b"ID3":
        return None
    if any(b & 0x80 for b in head[6:10]):
        return None
    return head[3], head[5], unsyncsafe(head[6:10])


def walk_frames(body: bytes) -> int | None:
    """Offset where the real frames of a v2.3 tag body end (padding excluded), or None if malformed."""
    pos = 0
    while pos + 10 <= len(body):
        if body[pos] == 0:  # padding starts
            break
        fid = body[pos : pos + 4]
        if not all(48 <= b <= 57 or 65 <= b <= 90 for b in fid):
            return None
        size = struct.unpack(">I", body[pos + 4 : pos + 8])[0]
        if pos + 10 + size > len(body):
            return None
        pos += 10 + size
    if any(body[pos:]):  # whatever follows the frames must be zero padding
        return None
    return pos


def write_chapters(path: str | Path, chapters: list[dict]) -> str:
    """Add the chapters to the mp3 at ``path`` and return how: "appended" or "fresh".

    "appended": the existing v2.3 tag was kept and the frames added to it.
    "fresh": there was no usable tag (none, v2.4, unsynchronised, extended header, malformed),
    so any existing tag is replaced by a new v2.3 tag holding only the chapters.
    """
    path = Path(path)
    new_frames = chapter_frames(chapters)
    with path.open("rb") as handle:
        header = read_header(handle.read(10))
        kept = b""
        audio_start = 0
        mode = "fresh"
        if header is not None:
            major, flags, size = header
            audio_start = 10 + size  # an unusable tag is still skipped, never left in front
            if major == 3 and not flags & (_FLAG_UNSYNC | _FLAG_EXTENDED):
                body = handle.read(size)
                end = walk_frames(body) if len(body) == size else None
                if end is not None:
                    kept = body[:end]
                    mode = "appended"
        frames = kept + new_frames
        if len(frames) >= 1 << 28:
            raise ValueError("ID3 tag too large")
        tag = b"ID3\x03\x00\x00" + syncsafe(len(frames)) + frames
        temp = path.with_name(path.name + ".id3tmp")
        try:
            handle.seek(audio_start)
            with temp.open("wb") as out:
                out.write(tag)
                while True:
                    block = handle.read(_COPY_BLOCK)
                    if not block:
                        break
                    out.write(block)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
    return mode

"""ffprobe wrappers - duration, codec and channel layout of a media file."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("audiobind")


@dataclass
class Probe:
    duration: float = 0.0
    codec: str = ""
    kbps: int = 0
    sample_rate: str = ""
    channels: str = ""

    @property
    def ok(self) -> bool:
        return self.duration > 0


#: Untrusted media (torrents) is opened with only local files and pipes allowed: a crafted
#: file (an HLS/concat playlist renamed .mp3, say) cannot make ffmpeg fetch URLs or open
#: other protocols. An input option, so it goes before every ``-i`` / input path.
PROTOCOL_WHITELIST = ["-protocol_whitelist", "file,pipe"]


def probe(path: str | Path, timeout: int = 60) -> Probe:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                *PROTOCOL_WHITELIST,
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate:stream=codec_name,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(result.stdout or "{}")
        fmt = data.get("format", {})
        stream = (data.get("streams") or [{}])[0]
        return Probe(
            duration=float(fmt.get("duration") or 0),
            codec=stream.get("codec_name") or "",
            kbps=int(fmt.get("bit_rate") or 0) // 1000,
            sample_rate=str(stream.get("sample_rate") or ""),
            channels=str(stream.get("channels") or ""),
        )
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out after %ss on %s: its duration is unknown", timeout, path)
        return Probe()
    except (subprocess.SubprocessError, ValueError, OSError):
        return Probe()


def probe_tags(path: str | Path, timeout: int = 30) -> dict[str, str]:
    """The file's embedded text tags (title, album, artist, album_artist, composer, ...).

    Keys are lower-cased (FLAC/Vorbis write ``TITLE``, MP4 writes ``title``); container
    (format) tags win over per-stream ones. {} when ffprobe fails or the file has none.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                *PROTOCOL_WHITELIST,
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_tags",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}
    tags: dict[str, str] = {}
    sources = [(s or {}).get("tags") or {} for s in (data.get("streams") or [])]
    sources.append((data.get("format") or {}).get("tags") or {})
    for source in sources:  # later sources (the format) overwrite stream tags
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            text = str(value or "").strip()
            if text:
                tags[str(key).lower()] = text
    return tags


#: probe_chapters / probe_streams read only container metadata, so a healthy run is
#: sub-second; the cap is for an ffprobe wedged on a dying NAS disk, which used to hold the
#: encoder (or the manual-action thread) forever
LIST_PROBE_TIMEOUT = 120


class ProbeTimeout(Exception):
    """ffprobe did not answer within its cap. Only raised on request (``raise_on_timeout=True``):
    by default a timeout reads as an empty list, which a caller cannot tell from a file that
    genuinely has no chapters / streams."""

    def __init__(self, path: str | Path, timeout: int, what: str):
        super().__init__(f"ffprobe {what} timed out after {timeout}s on {path}")
        self.path = str(path)
        self.timeout = timeout


def probe_chapters(path: str | Path, timeout: int = LIST_PROBE_TIMEOUT, raise_on_timeout: bool = False) -> list[dict]:
    """The file's chapters; [] when there are none, ffprobe's output is unreadable, or it timed out.

    With ``raise_on_timeout`` a timeout raises ProbeTimeout instead, so the caller can tell a
    file that answered "no chapters" from one that never answered."""
    try:
        result = subprocess.run(
            ["ffprobe", *PROTOCOL_WHITELIST, "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if raise_on_timeout:
            log.warning("ffprobe -show_chapters timed out after %ss on %s", timeout, path)
            raise ProbeTimeout(path, timeout, "-show_chapters") from None
        # an empty answer reads as "no chapters" to the caller, so say why it is empty
        log.warning("ffprobe -show_chapters timed out after %ss on %s: treating it as no chapters", timeout, path)
        return []
    try:
        return json.loads(result.stdout or "{}").get("chapters", []) or []
    except ValueError:
        return []


def probe_streams(path: str | Path, timeout: int = LIST_PROBE_TIMEOUT, raise_on_timeout: bool = False) -> list[dict]:
    """The file's streams; [] when ffprobe's output is unreadable or it timed out (with
    ``raise_on_timeout``, a timeout raises ProbeTimeout instead)."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                *PROTOCOL_WHITELIST,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,channels,bit_rate:stream_disposition=attached_pic",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if raise_on_timeout:
            log.warning("ffprobe -show_entries stream timed out after %ss on %s", timeout, path)
            raise ProbeTimeout(path, timeout, "-show_entries stream") from None
        log.warning("ffprobe -show_entries stream timed out after %ss on %s: treating it as no streams", timeout, path)
        return []
    try:
        return json.loads(result.stdout or "{}").get("streams", []) or []
    except ValueError:
        return []


def has_embedded_cover(path: str | Path) -> bool:
    return any(
        s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
        for s in probe_streams(path)
    )

"""ffprobe wrappers - duration, codec and channel layout of a media file."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


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


def probe_chapters(path: str | Path) -> list[dict]:
    result = subprocess.run(
        ["ffprobe", *PROTOCOL_WHITELIST, "-v", "error", "-show_chapters", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout or "{}").get("chapters", []) or []
    except ValueError:
        return []


def probe_streams(path: str | Path) -> list[dict]:
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
    )
    try:
        return json.loads(result.stdout or "{}").get("streams", []) or []
    except ValueError:
        return []


def has_embedded_cover(path: str | Path) -> bool:
    return any(
        s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
        for s in probe_streams(path)
    )

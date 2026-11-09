"""Merge a unit into one m4b: chapters, tags, cover, verification."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import catalog
from .config import CONFIG, effective_val
from .control import CONTROL, Stopped
from .probe import has_embedded_cover, probe, probe_chapters, probe_streams
from .units import BookUnit

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


@dataclass
class EncodeResult:
    path: Path
    duration: float
    size: int
    chapters: int
    chapters_source: str


#: never encode below the configured floor, never chase a source above this ceiling
BITRATE_CEILING = 320


def choose_bitrate(source_kbps: float, configured: str) -> int:
    """AAC bitrate in kbps: the configured floor, but keep a richer source as-is.

    A 256 kbps mp3 stays 256 kbps; a 64 kbps mp3 is lifted to the floor (default 128).
    """
    try:
        floor = int(str(configured).lower().rstrip("k"))
    except ValueError:
        floor = 128
    if source_kbps <= 0:
        return floor
    return int(min(max(floor, round(source_kbps)), BITRATE_CEILING))


def _nice_prefix() -> list[str]:
    n = effective_val("nice")
    return ["nice", "-n", str(n)] if n else []


#: containers that usually carry the publisher's own chapter tags
_CHAPTERED_EXTS = (".m4b", ".m4a", ".mp3")


def _source_chapters(unit: BookUnit) -> list[dict]:
    """Chapter markers embedded in the source files themselves.

    Offset-accumulated across parts so a multi-disc rip becomes one continuous
    timeline; titles are preserved exactly as tagged.  Returns [] when the
    sources carry no usable chapter metadata (fewer than two usable markers,
    or a file whose duration cannot be probed, since then the offsets
    cannot be trusted).
    """
    chapters: list[dict] = []
    cursor = 0.0
    for part in unit.parts:
        for path in sorted(part.files, key=lambda p: os.path.basename(p).lower()):
            length = probe(path).duration
            if length <= 0:
                return []           # cannot place chapters on a timeline we cannot measure
            for item in probe_chapters(path):
                try:
                    start = float(item.get("start_time") or 0)
                    end = float(item.get("end_time") or 0)
                except (TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                title = ((item.get("tags") or {}).get("title")
                         or f"Chapter {len(chapters) + 1}").strip()
                chapters.append({
                    "start": round(cursor + min(start, length), 3),
                    "end": round(cursor + min(end, length), 3),
                    "title": title,
                })
            cursor += length
    return chapters if len(chapters) >= 2 else []


def _audnex_chapters(audnex_chapters: list[dict], duration: float) -> list[dict] | None:
    """Audnex timings mapped onto the merged timeline, or None when they disagree."""
    if not audnex_chapters:
        return None
    total = sum(catalog.chapter_ms(c) for c in audnex_chapters) / 1000.0
    if total <= 0 or not 0.85 <= duration / total <= 1.20:
        return None
    scale = duration / total
    chapters, cursor = [], 0.0
    for item in audnex_chapters:
        length = catalog.chapter_ms(item) / 1000.0 * scale
        chapters.append({
            "start": round(cursor, 3),
            "end": round(cursor + length, 3),
            "title": (item.get("title") or f"Chapter {len(chapters) + 1}").strip(),
        })
        cursor += length
    if chapters:
        chapters[-1]["end"] = round(duration, 3)
    return chapters


def build_chapters(audnex_chapters: list[dict], duration: float,
                   unit: BookUnit) -> tuple[list[dict], str]:
    """Chapter plan for the merged m4b, using the best evidence available.

    Three tiers, in priority order:

    1. ``source-embedded`` - chapter tags inside the source files themselves,
       offset-accumulated across parts, titles preserved.  This is the
       publisher's own data, so when it is present AND Audible chapter data is
       also available, it wins for single-part m4b/mp3 sources, where the tags
       are almost certainly the real thing.  Multi-part merges and other
       containers prefer Audnex timings instead, because per-file tags can
       repeat or drift at part boundaries.
    2. ``audnex`` - Audible's chapter list for the matched ASIN, scaled onto
       the measured runtime (durations must agree within 0.85-1.20x).
    3. ``per-file`` - one chapter per source file, the last-resort fallback.

    Returns (chapters, source-label); the label names the winning tier so the
    "chapters: N from X" log line records which evidence was used.
    """
    src_chapters = _source_chapters(unit)
    audnex_plan = _audnex_chapters(audnex_chapters, duration)

    if src_chapters and audnex_plan:
        single_part_tagged = (not unit.multi_part
                              and all(os.path.splitext(f)[1].lower() in _CHAPTERED_EXTS
                                      for f in unit.files))
        if single_part_tagged:
            return src_chapters, f"source-embedded ({len(src_chapters)} chapters)"
        return audnex_plan, f"audnex ({len(audnex_plan)} chapters, scaled)"
    if src_chapters:
        return src_chapters, f"source-embedded ({len(src_chapters)} chapters)"
    if audnex_plan:
        return audnex_plan, f"audnex ({len(audnex_plan)} chapters, scaled)"

    chapters, cursor = [], 0.0
    for part in unit.parts:
        for path in sorted(part.files, key=lambda p: os.path.basename(p).lower()):
            length = probe(path).duration
            chapters.append({"start": round(cursor, 3), "end": round(cursor + length, 3),
                             "title": os.path.splitext(os.path.basename(path))[0][:90]})
            cursor += length
    return chapters, f"per-file ({len(chapters)} chapters)"


def _escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("\n", " ").replace("=", "\\=").replace(";", "\\;").strip()


def write_ffmetadata(path: Path, book: dict, match, chapters: list[dict]) -> dict:
    """Write the ffmetadata input (tags + chapters) and return the tag dict."""
    series_primary = book.get("seriesPrimary") or {}
    series = ""
    if series_primary.get("name"):
        series = f"{series_primary['name']} #{series_primary.get('position', '')}".strip(" #")

    authors = [a.get("name") for a in (book.get("authors") or []) if a.get("name")] or \
              (match.product.authors if match.product else [])
    narrators = [n.get("name") for n in (book.get("narrators") or []) if n.get("name")] or \
                (match.product.narrators if match.product else [])
    description = clean_description(book.get("description") or (match.product.summary if match.product else ""))
    year = (book.get("releaseDate") or (match.product.year if match.product else ""))[:4]

    tags = {
        "title": book.get("title") or (match.product.title if match.product else ""),
        "album": book.get("title") or (match.product.title if match.product else ""),
        "artist": "; ".join(authors),
        "album_artist": "; ".join(authors),
        "composer": "; ".join(narrators),          # convention for narrator in m4b
        "genre": "Audiobook",
        "date": year,
        "grouping": series,                        # the atom mp4 actually keeps
        "comment": description,
        "description": description,
        "publisher": book.get("publisherName") or "",
        "asin": book.get("asin") or "",
        "narrator": "; ".join(narrators),
        "series": series,
    }

    lines = [";FFMETADATA1"]
    for key, value in tags.items():
        if value:
            lines.append(f"{key}={_escape(value)}")
    for chapter in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(chapter['start'] * 1000)}", f"END={int(chapter['end'] * 1000)}",
                  f"title={_escape(chapter['title'])}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tags


def _metadata_map_args(has_cover: bool) -> list:
    """Point tags and chapters at the ffmetadata file, whichever input it landed on.

    Input 0 is the concat list; the cover (when there is one) is input 1 and the
    metadata file comes last. Without the cover the metadata file IS input 1, so a
    hardcoded index of 2 pointed at nothing and every no-cover book lost its tags.
    ``-map_chapters -1`` first stops the muxer from also carrying the concat
    input's embedded chapters, which would double the list.
    """
    idx = 2 if has_cover else 1
    return ["-map_metadata", str(idx), "-map_chapters", "-1", "-map_chapters", str(idx)]


def clean_description(html: str) -> str:
    import re
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def encode(unit: BookUnit, match, book: dict, work_dir: Path, out_path: Path,
           log=print, copy_audio: bool = False) -> EncodeResult:
    """Run ffmpeg. Raises RuntimeError when the result fails verification."""
    work_dir.mkdir(parents=True, exist_ok=True)
    total_duration = sum(probe(f).duration for f in unit.files)

    chapters, source = build_chapters(catalog.chapters(match.asin), total_duration, unit)
    log(f"  chapters: {len(chapters)} from {source}")

    concat_file = work_dir / "concat.txt"
    with concat_file.open("w", encoding="utf-8") as handle:
        for path in unit.files:
            handle.write("file '" + str(path).replace("'", "'\\''") + "'\n")

    metadata_file = work_dir / "meta.txt"
    write_ffmetadata(metadata_file, book, match, chapters)

    cover_file = work_dir / "cover.jpg"
    has_cover = catalog.download(catalog.cover_url(book, match.asin), str(cover_file))
    log(f"  cover: {'ok' if has_cover else 'not found'}")

    temp_out = work_dir / "output.m4b"
    command = _nice_prefix() + [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
    ]
    if has_cover:
        command += ["-i", str(cover_file)]
    command += ["-i", str(metadata_file), "-map", "0:a:0"]
    if has_cover:
        command += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    command += _metadata_map_args(has_cover)
    if copy_audio:
        # already AAC in an MP4 container: keep the samples, only rewrite the wrapper
        log("  audio: copied (source is already AAC) - no re-encode, no quality loss")
        command += ["-c:a", "copy"]
    else:
        source_bytes = sum(os.path.getsize(f) for f in unit.files if os.path.exists(f))
        source_kbps = (source_bytes * 8 / total_duration / 1000) if total_duration else 0
        eff_bitrate = effective_val("audio_bitrate")
        bitrate = choose_bitrate(source_kbps, eff_bitrate)
        log(f"  audio: aac {bitrate}k (source {source_kbps:.0f}k, floor {eff_bitrate})")
        command += ["-c:a", "aac", "-b:a", f"{bitrate}k",
                    "-ac", str(CONFIG.audio_channels), "-ar", str(CONFIG.sample_rate)]
    command += [
                # use_metadata_tags makes the mp4 muxer drop the cover: never add it
                "-movflags", "+faststart", "-f", "mp4", str(temp_out)]

    # Popen (not run) so the status page can abort mid-book and show a progress bar
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    CONTROL.attach_process(proc)

    errors: list[str] = []

    def drain_stderr() -> None:
        if proc.stderr is not None:
            errors.append(proc.stderr.read() or "")

    threading.Thread(target=drain_stderr, daemon=True, name="ffmpeg-stderr").start()

    started = time.time()
    speed = 0.0
    try:
        for line in proc.stdout or []:
            key, _, value = line.strip().partition("=")
            if key == "speed":
                try:
                    speed = float(value.rstrip("x"))
                except ValueError:
                    speed = 0.0
            elif key == "out_time" and ":" in value:
                # out_time is HH:MM:SS.ffffff. Do NOT use out_time_ms: despite the name ffmpeg
                # emits it in microseconds, so dividing by 1000 pins the bar at 100%.
                parts = value.split(":")
                done = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                if total_duration > 0:
                    percent = min(99.9, done / total_duration * 100)
                    remaining = max(0.0, total_duration - done)
                    CONTROL.set_progress(
                        percent=round(percent, 1),
                        elapsed=round(time.time() - started),
                        eta=round(remaining / speed) if speed > 0.05 else None,
                        speed=round(speed, 2),
                    )
        proc.wait()
    finally:
        CONTROL.detach_process()
        CONTROL.clear_progress()

    if CONTROL.stop_requested:
        raise Stopped("stopped by operator")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(errors[0] if errors else '')[-800:]}")

    duration = float(probe(temp_out).duration)
    chapter_count = len(probe_chapters(temp_out))
    streams = probe_streams(temp_out)
    audio_ok = any(s.get("codec_name") == "aac" for s in streams)

    if not audio_ok:
        raise RuntimeError("output has no AAC audio stream")
    if abs(duration - total_duration) > max(120, total_duration * 0.02):
        hint = ""
        for line in (errors[0] if errors else "").splitlines():
            low = line.lower()
            if any(word in low for word in ("invalid", "error", "could not", "failed")):
                hint = " | ffmpeg: " + line.strip()[:160]
                break
        raise RuntimeError(
            f"output is {duration / 3600:.2f}h but the sources are {total_duration / 3600:.2f}h - "
            f"files were dropped during the merge{hint}")
    if chapter_count != len(chapters):
        raise RuntimeError(f"expected {len(chapters)} chapters, found {chapter_count}")
    if has_cover and not has_embedded_cover(temp_out):
        raise RuntimeError("cover art is missing from the output")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_out), str(out_path))
    if has_cover:
        shutil.copy2(cover_file, out_path.parent / "cover.jpg")

    return EncodeResult(path=out_path, duration=duration, size=out_path.stat().st_size,
                        chapters=chapter_count, chapters_source=source)


def write_abs_metadata(path: Path, book: dict, match, chapters: list[dict],
                       duration: float) -> dict:
    """The metadata.json shape Audiobookshelf reads (and rewrites on its own scans)."""
    product = match.product
    series_primary = book.get("seriesPrimary") or {}
    data = {
        "tags": [g.get("name") for g in (book.get("genres") or []) if g.get("name")][:8],
        "chapters": [{"start": c["start"], "end": c["end"], "title": c["title"], "id": i}
                     for i, c in enumerate(chapters)],
        "title": book.get("title") or (product.title if product else ""),
        "subtitle": book.get("subtitle") or (product.subtitle if product else "") or None,
        "authors": [a.get("name") for a in (book.get("authors") or []) if a.get("name")]
                   or (product.authors if product else []),
        "narrators": [n.get("name") for n in (book.get("narrators") or []) if n.get("name")]
                     or (product.narrators if product else []),
        "series": [f"{series_primary['name']} #{series_primary.get('position', '')}".strip()]
                  if series_primary.get("name") else [],
        "genres": [g.get("name") for g in (book.get("genres") or []) if g.get("name")],
        "publishedYear": (book.get("releaseDate") or (product.year if product else ""))[:4] or None,
        "publishedDate": book.get("releaseDate") or None,
        "publisher": book.get("publisherName") or None,
        "description": clean_description(book.get("description") or ""),
        "isbn": book.get("isbn") or None,
        "asin": book.get("asin") or match.asin,
        "language": book.get("language") or (product.language if product else "") or "english",
        "explicit": bool(book.get("isAdultProduct", False)),
        "abridged": bool(book.get("isAbridged", False)),
        "duration": round(duration, 2),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data

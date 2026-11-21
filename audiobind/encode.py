"""Merge a unit into one m4b (or mp3): chapters, tags, cover, verification."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import catalog, id3
from .config import CONFIG, effective_val
from .control import CONTROL, Stopped
from .probe import PROTOCOL_WHITELIST, ProbeTimeout, has_embedded_cover, probe, probe_chapters, probe_streams
from .units import BookUnit, unsafe_path_reason

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


@dataclass
class EncodeResult:
    path: Path
    duration: float
    size: int
    chapters: int
    chapters_source: str
    cover: Path | None = None  # the downloaded cover, copied beside the book on publish


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


@dataclass
class ChapterPlan:
    """Chapters for one conversion, worked out once and shared by the encoder and metadata.json.

    ``durations`` holds every source file's probed length, so the encoder does not run
    ffprobe over the same files a second time.
    """

    chapters: list[dict]
    source: str
    durations: dict[str, float]
    #: the audio codecs across the source files; None when nobody probed them (CORE-01: more
    #: than one means the parts cannot go through the concat demuxer as they are)
    codecs: set[str] | None = None

    @property
    def total(self) -> float:
        return sum(self.durations.values())


def _durations(unit: BookUnit, durations: dict[str, float] | None) -> dict[str, float]:
    if durations is not None:
        return durations
    return _probe_sources(unit)[0]


def _probe_sources(unit: BookUnit) -> tuple[dict[str, float], set[str]]:
    """Every source file's duration, and the set of audio codecs among them."""
    result: dict[str, float] = {}
    codecs: set[str] = set()
    for path in unit.files:
        try:
            info = probe(path, raise_on_timeout=True)
            result[path] = info.duration
            if info.codec:
                codecs.add(info.codec)
        except ProbeTimeout as exc:
            # not "no duration": a 0.0 here would drop the whole book's source chapters (and skew the
            # total the chapter distribution is scaled against) while the book converted anyway
            raise RuntimeError(
                f"ffprobe timed out after {exc.timeout}s reading the duration of {path}; "
                "the book is not converted so it cannot lose chapters - check that the disk is healthy "
                "and retry"
            ) from exc
    return result, codecs


def plan_chapters(
    unit: BookUnit, asin: str, durations: dict[str, float] | None = None, codecs: set[str] | None = None
) -> ChapterPlan:
    """Build the chapter plan for ``encode()``, probing each source file at most once.

    Pass ``durations`` (and ``codecs``) when the files were already probed (the audit does), and
    nothing is probed again.
    """
    if durations is None or set(durations) != set(unit.files):
        durations, codecs = _probe_sources(unit)
    chapters, source = build_chapters(catalog.chapters(asin), sum(durations.values()), unit, durations)
    return ChapterPlan(chapters=chapters, source=source, durations=durations, codecs=codecs)


def _empty(start: float, end: float) -> bool:
    """True when a chapter spans no time once written as whole ms (ffmetadata START/END)."""
    return int(end * 1000) <= int(start * 1000)


def _source_chapters(unit: BookUnit, durations: dict[str, float] | None = None) -> list[dict]:
    """Chapter markers embedded in the source files themselves.

    Offset-accumulated across parts so a multi-disc rip becomes one continuous
    timeline; titles are preserved exactly as tagged.  Returns [] when the
    sources carry no usable chapter metadata (fewer than two usable markers,
    or a file whose duration cannot be probed, since then the offsets
    cannot be trusted).

    Raises RuntimeError when ffprobe times out on a source file: that is not "no chapters", and
    dropping the file's markers silently would convert the book with chapters missing (the
    output check compares the result against this already-shortened list, so it would pass).
    """
    durations = _durations(unit, durations)
    chapters: list[dict] = []
    cursor = 0.0
    # unit.files is the concat order (natural sort: Track 2 before Track 10); the
    # timeline must walk the files in exactly the order the audio is joined
    for path in unit.files:
        length = durations.get(path, 0.0)
        if length <= 0:
            return []  # cannot place chapters on a timeline we cannot measure
        try:
            found = probe_chapters(path, raise_on_timeout=True)
        except ProbeTimeout as exc:
            raise RuntimeError(
                f"ffprobe timed out after {exc.timeout}s reading the chapters of {path}; "
                "the book is not converted so it cannot lose chapters - check that the disk is healthy "
                "and retry"
            ) from exc
        for item in found:
            try:
                start = float(item.get("start_time") or 0)
                end = float(item.get("end_time") or 0)
            except (TypeError, ValueError):
                continue
            placed_start = round(cursor + min(start, length), 3)
            placed_end = round(cursor + min(end, length), 3)
            if _empty(placed_start, placed_end):  # includes a tag past the file's end, clamped
                continue
            title = ((item.get("tags") or {}).get("title") or f"Chapter {len(chapters) + 1}").strip()
            chapters.append({"start": placed_start, "end": placed_end, "title": title})
        cursor += length
    return chapters if len(chapters) >= 2 else []


def _start_ms(chapter: dict) -> int | None:
    """Audnex ``startOffsetMs`` (older payloads: ``start_offset_ms``), or None when absent."""
    for key in ("startOffsetMs", "start_offset_ms"):
        value = chapter.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _audnex_chapters(audnex_chapters: list[dict], duration: float) -> list[dict] | None:
    """Audnex timings mapped onto the merged timeline, or None when they disagree.

    Each chapter starts at its own ``startOffsetMs`` when Audnex gives one, so a gap or a
    brand intro before a chapter does not shift every chapter after it. Only when the
    offsets are missing (or out of order) do the starts fall back to a running sum of
    ``lengthMs``. Either way the timeline is scaled onto the measured runtime.
    """
    if not audnex_chapters:
        return None
    lengths = [catalog.chapter_ms(c) / 1000.0 for c in audnex_chapters]
    raw_offsets = [_start_ms(c) for c in audnex_chapters]
    offsets = [o / 1000.0 for o in raw_offsets if o is not None]
    use_offsets = len(offsets) == len(lengths) and all(b >= a for a, b in zip(offsets, offsets[1:]))
    total = offsets[-1] + lengths[-1] if use_offsets else sum(lengths)
    if total <= 0 or not 0.85 <= duration / total <= 1.20:
        return None
    scale = duration / total
    chapters, cursor = [], 0.0
    for index, item in enumerate(audnex_chapters):
        start = offsets[index] * scale if use_offsets else cursor
        length = lengths[index] * scale
        chapters.append(
            {
                "start": round(start, 3),
                "end": round(start + length, 3),
                "title": (item.get("title") or f"Chapter {len(chapters) + 1}").strip(),
            }
        )
        cursor = start + length
    for current, following in zip(chapters, chapters[1:]):
        current["end"] = following["start"]  # a gap belongs to the chapter before it
    if chapters:
        chapters[-1]["end"] = round(duration, 3)
    # a zero-length entry (lengthMs 0, or two chapters on one offset) would be an empty
    # chapter in the output; dropping it leaves the timeline contiguous, because the
    # chapter before it already ends where the next one starts
    chapters = [c for c in chapters if not _empty(c["start"], c["end"])]
    return chapters or None


def build_chapters(
    audnex_chapters: list[dict], duration: float, unit: BookUnit, durations: dict[str, float] | None = None
) -> tuple[list[dict], str]:
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
    durations = _durations(unit, durations)
    src_chapters = _source_chapters(unit, durations)
    audnex_plan = _audnex_chapters(audnex_chapters, duration)

    if src_chapters and audnex_plan:
        single_part_tagged = not unit.multi_part and all(
            os.path.splitext(f)[1].lower() in _CHAPTERED_EXTS for f in unit.files
        )
        if single_part_tagged:
            return src_chapters, f"source-embedded ({len(src_chapters)} chapters)"
        return audnex_plan, f"audnex ({len(audnex_plan)} chapters, scaled)"
    if src_chapters:
        return src_chapters, f"source-embedded ({len(src_chapters)} chapters)"
    if audnex_plan:
        return audnex_plan, f"audnex ({len(audnex_plan)} chapters, scaled)"

    chapters, cursor = [], 0.0
    for path in unit.files:  # concat order - see _source_chapters
        length = durations.get(path, 0.0)
        start, end = round(cursor, 3), round(cursor + length, 3)
        if not _empty(start, end):  # a file that measured 0 s gets no empty chapter
            chapters.append({"start": start, "end": end, "title": os.path.splitext(os.path.basename(path))[0][:90]})
        cursor += length
    return chapters, f"per-file ({len(chapters)} chapters)"


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _escape(value: str) -> str:
    """One ffmetadata tag value: control characters (\\n, \\r, tab, NUL...) become spaces, so
    a value can never end its line and start a new key or [CHAPTER] section; then the
    ffmetadata specials - backslash, '=', ';' and '#' - are backslash-escaped."""
    text = _CONTROL_CHARS.sub(" ", value or "")
    for special in ("\\", "=", ";", "#"):
        text = text.replace(special, "\\" + special)
    return text.strip()


def write_ffmetadata(path: Path, book: dict, match, chapters: list[dict]) -> dict:
    """Write the ffmetadata input (tags + chapters) and return the tag dict."""
    series_primary = book.get("seriesPrimary") or {}
    series = ""
    if series_primary.get("name"):
        series = f"{series_primary['name']} #{series_primary.get('position', '')}".strip(" #")

    authors = [a.get("name") for a in (book.get("authors") or []) if a.get("name")] or (
        match.product.authors if match.product else []
    )
    narrators = [n.get("name") for n in (book.get("narrators") or []) if n.get("name")] or (
        match.product.narrators if match.product else []
    )
    description = clean_description(
        book.get("description") or (match.product.summary if match.product else "")
    )
    year = (book.get("releaseDate") or (match.product.year if match.product else ""))[:4]

    tags = {
        "title": book.get("title") or (match.product.title if match.product else ""),
        "album": book.get("title") or (match.product.title if match.product else ""),
        "artist": "; ".join(authors),
        "album_artist": "; ".join(authors),
        "composer": "; ".join(narrators),  # convention for narrator in m4b
        "genre": "Audiobook",
        "date": year,
        "grouping": series,  # the atom mp4 actually keeps
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
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(chapter['start'] * 1000)}",
            f"END={int(chapter['end'] * 1000)}",
            f"title={_escape(chapter['title'])}",
        ]
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


#: output formats the encoder can build; "M4B + MP3" is two encodes, see pipeline.formats_for
FORMATS = ("m4b", "mp3")

#: bitrates (kbps) an mp3 can carry; lame would round anything else anyway
MP3_BITRATES = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


def mp3_bitrate(kbps: int) -> int:
    """The valid mp3 bitrate closest to ``kbps`` (64k/128k/192k/256k are all valid already)."""
    return min(MP3_BITRATES, key=lambda b: (abs(b - kbps), b))


def formats_for(setting: str) -> list[str]:
    """The files to publish for an ``output_format`` setting, archival m4b first."""
    value = str(setting or "M4B").strip().upper().replace(" ", "")
    if value == "MP3":
        return ["mp3"]
    if value in ("M4B+MP3", "MP3+M4B"):
        return ["m4b", "mp3"]
    return ["m4b"]


def mp3_tags(tags: dict, sequence: str = "") -> dict:
    """ffmpeg ``-metadata`` keys (ID3v2.3 frames) from the tag dict the m4b gets."""
    mapping = {
        "title": "title",
        "artist": "artist",
        "album_artist": "album_artist",
        "album": "album",
        "genre": "genre",
        "comment": "comment",
        "composer": "composer",
        "date": "date",
        "publisher": "publisher",
    }
    out = {}
    for src, dst in mapping.items():
        value = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", str(tags.get(src) or "")).strip()
        if value:
            out[dst] = value
    if str(sequence or "").strip().isdigit():
        out["track"] = str(int(sequence))
    return out


def build_command(
    fmt: str,
    concat_file,
    cover_file,
    metadata_file,
    temp_out,
    has_cover: bool,
    copy_audio: bool = False,
    bitrate: int = 128,
    tags: dict | None = None,
) -> list:
    """The ffmpeg argv (without the nice prefix) for one output file.

    m4b: ffmetadata input carries tags and chapters. mp3: chapters cannot ride along (ffmpeg
    drops them), so tags are ``-metadata`` args and the chapters are written afterwards by
    ``id3.write_chapters``; the cover becomes an APIC frame.
    """
    command = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-progress",
        "pipe:1",
        "-nostats",
        *PROTOCOL_WHITELIST,
        "-f",
        "concat",
        # -safe 0 is required: concat's "safe" mode rejects every absolute path and any
        # name with a space or other non-portable character, i.e. every real library
        # path. Instead each path is sanitised - names with \n, \r or NUL are held
        # (unsafe_path_reason) so they cannot start a new concat directive, ' is escaped -
        # and the protocol whitelist (inherited by every file the list opens) keeps it to
        # local files.
        "-safe",
        "0",
        "-i",
        str(concat_file),
    ]
    if fmt == "mp3":
        # every -i first, then every -map: ffmpeg binds an output option that sits before a
        # later -i to that input and refuses ("cannot be applied to input url")
        if has_cover:
            command += [*PROTOCOL_WHITELIST, "-i", str(cover_file)]
        command += ["-map", "0:a:0"]
        if has_cover:
            command += ["-map", "1:v:0", "-c:v", "mjpeg"]
            command += ["-id3v2_version", "3", "-metadata:s:v", "title=Album cover"]
            command += ["-metadata:s:v", "comment=Cover (front)"]
        else:
            command += ["-id3v2_version", "3"]
        # nothing from the concat input (its own tags, chapters) may leak into the book
        command += ["-map_metadata", "-1", "-map_chapters", "-1"]
        for key, value in (tags or {}).items():
            command += ["-metadata", f"{key}={value}"]
        command += [
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{mp3_bitrate(bitrate)}k",
            "-ac",
            str(CONFIG.audio_channels),
            "-ar",
            str(CONFIG.sample_rate),
            "-f",
            "mp3",
            str(temp_out),
        ]
        return command
    if has_cover:
        command += [*PROTOCOL_WHITELIST, "-i", str(cover_file)]
    command += [*PROTOCOL_WHITELIST, "-i", str(metadata_file), "-map", "0:a:0"]
    if has_cover:
        command += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    command += _metadata_map_args(has_cover)
    if copy_audio:
        command += ["-c:a", "copy"]
    else:
        command += [
            "-c:a",
            "aac",
            "-b:a",
            f"{bitrate}k",
            "-ac",
            str(CONFIG.audio_channels),
            "-ar",
            str(CONFIG.sample_rate),
        ]
    command += [
        # use_metadata_tags makes the mp4 muxer drop the cover: never add it
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp_out),
    ]
    return command


_STDERR_TAIL_BYTES = 256 * 1024


def _stderr_tail(path: Path) -> str:
    """The last 256 KB of ffmpeg's stderr log ("" when there is none)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _STDERR_TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


#: a decoder that rejects this many packets is not reading the audio, whatever the exit code: a
#: FLAC fed to the MP3 decoder after a concat boundary logs one rejected packet per frame (CORE-01).
#: A handful is a damaged frame or two in an otherwise good rip and still passes
DECODE_ERROR_STORM = 25
#: ffmpeg 6+ "Error submitting packet to decoder", older builds "Error while decoding stream #0:0"
_DECODE_ERROR_RE = re.compile(r"error (?:submitting packet to decoder|while decoding stream)", re.I)
_REPEATED_RE = re.compile(r"last message repeated (\d+) times", re.I)


def decode_errors(stderr: str) -> int:
    """How many packets ffmpeg's decoders rejected, per its stderr (collapsed repeats included)."""
    count, last_was_error = 0, False
    for line in stderr.splitlines():
        repeated = _REPEATED_RE.search(line)
        if repeated:
            if last_was_error:
                count += int(repeated.group(1))
            continue
        last_was_error = bool(_DECODE_ERROR_RE.search(line))
        count += last_was_error
    return count


def _check_decode(stderr: str, what: str) -> None:
    rejected = decode_errors(stderr)
    if rejected >= DECODE_ERROR_STORM:
        first = next((line.strip() for line in stderr.splitlines() if _DECODE_ERROR_RE.search(line)), "")
        raise RuntimeError(
            f"ffmpeg could not decode {rejected} packets of {what} - audio would be missing | ffmpeg: {first[:160]}"
        )


def normalise_command(source, dest) -> list:
    """ffmpeg argv decoding one part of a mixed-codec book to the uniform lossless intermediate.

    Every part comes out as 16-bit PCM WAV at the final encode's rate and layout, so the concat
    demuxer sees one stream it can join: it has a single decoder for all parts (CORE-01). Not FLAC:
    the demuxer applies the first file's STREAMINFO to every part and rejects the second one's
    frames just the same. ``-rf64 auto`` lifts the 4 GB RIFF cap (about 6.7 hours of one file).
    """
    return [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        *PROTOCOL_WHITELIST,
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:a",
        "pcm_s16le",
        "-ac",
        str(CONFIG.audio_channels),
        "-ar",
        str(CONFIG.sample_rate),
        "-rf64",
        "auto",
        "-f",
        "wav",
        str(dest),
    ]


#: CORE-01c: the PCM parts may take at most this share of the scratch filesystem's free space; the
#: rest is headroom for the encoded output, the logs and whatever else shares that disk
PCM_SPACE_FRACTION = 0.8
#: every live parts directory has this file beside it (``parts.owner``) under flock(2): the kernel drops
#: the lock when its process dies, however it dies, so parts whose owner file is unlocked are stranded.
#: Beside, not inside: some filesystems (FUSE, NFS) keep an open file's deleted name around, and the
#: directory it was in then cannot be removed while the lock is held
_PARTS_OWNER = "parts.owner"
#: a parts directory with no owner file at all is stranded once it is this old (seconds): one from
#: before the owner file existed, or a run killed between creating the file and locking it
_UNOWNED_PARTS_AGE = 60.0
#: the parts directories this process holds, and the descriptor holding each one's lock
_PART_CLAIMS: dict[str, int] = {}


def pcm_bytes(seconds: float) -> int:
    """What ``seconds`` of audio takes as the join's 16-bit PCM at the configured rate and layout."""
    return int(seconds * CONFIG.audio_channels * CONFIG.sample_rate * 2)


def check_pcm_space(seconds: float, work_dir: Path) -> None:
    """Refuse (RuntimeError) a PCM join whose parts would not fit on the scratch disk with headroom."""
    where = next(p for p in (work_dir, *work_dir.parents) if p.exists())
    free = shutil.disk_usage(where).free
    need, budget = pcm_bytes(seconds), int(free * PCM_SPACE_FRACTION)
    if need > budget:
        raise RuntimeError(
            f"not enough scratch space to join mixed codecs: {seconds / 3600:.1f}h of PCM needs "
            f"{need / 1e9:.1f} GB, {free / 1e9:.1f} GB free on {where} (at most "
            f"{PCM_SPACE_FRACTION:.0%} used) - free space there or mount a larger /work; nothing was decoded"
        )


def _claim_parts(parts_dir: Path) -> None:
    """Lock ``parts_dir``'s owner file, then create the directory empty; held until ``_release_parts``."""
    parts_dir.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(parts_dir.with_name(_PARTS_OWNER), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutil.rmtree(parts_dir, ignore_errors=True)
        parts_dir.mkdir()
    except BaseException:
        os.close(fd)
        raise
    _PART_CLAIMS[str(parts_dir)] = fd


def _drop_owner(parts_dir: Path, fd: int) -> None:
    """Remove the parts, then unlock and delete the owner file (closed first: see _PARTS_OWNER)."""
    try:
        shutil.rmtree(parts_dir, ignore_errors=True)
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            parts_dir.with_name(_PARTS_OWNER).unlink()


def _release_parts(parts_dir: Path) -> None:
    """Delete a parts directory this process claimed, and its lock. Safe to call twice."""
    fd = _PART_CLAIMS.pop(str(parts_dir), None)
    if fd is not None:
        _drop_owner(parts_dir, fd)


def reclaim_stale_parts(root: Path | None = None) -> list[Path]:
    """Delete the PCM parts directories under the work root that no running conversion owns.

    A hard kill (or a power cut) skips every ``finally``; its parts would otherwise stay on the
    scratch disk until that same book happened to be converted again. Returns what was removed.
    """
    root = Path(CONFIG.work_dir if root is None else root)
    removed = []
    for parts_dir in sorted({*root.glob("*/parts"), *root.glob("*/*/parts")}):
        if str(parts_dir) in _PART_CLAIMS or parts_dir.is_symlink() or not parts_dir.is_dir():
            continue
        try:
            fd = os.open(parts_dir.with_name(_PARTS_OWNER), os.O_RDWR)
        except FileNotFoundError:
            try:
                if time.time() - parts_dir.stat().st_mtime < _UNOWNED_PARTS_AGE:
                    continue
            except OSError:
                continue
            shutil.rmtree(parts_dir, ignore_errors=True)
            removed.append(parts_dir)
            continue
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)  # a live conversion, in this process or another, holds it
            continue
        _drop_owner(parts_dir, fd)
        removed.append(parts_dir)
    return removed


def _normalise_parts(files: list[str], work_dir: Path) -> list[str]:
    """Decode each source to ``work_dir/parts/NNNN.wav``; the paths, in concat order.

    The caller has claimed ``work_dir/parts`` (``_claim_parts``) and releases it."""
    parts_dir = work_dir / "parts"
    out = []
    for n, source in enumerate(files, 1):
        dest = parts_dir / f"{n:04d}.wav"
        stderr_path = parts_dir / f"{n:04d}.log"
        with stderr_path.open("wb") as stderr_file:
            proc = subprocess.Popen(
                _nice_prefix() + normalise_command(source, dest), stdout=subprocess.DEVNULL, stderr=stderr_file
            )
        CONTROL.attach_process(proc)
        try:
            proc.wait()
        finally:
            CONTROL.detach_process()
        if CONTROL.stop_requested:
            raise Stopped("stopped by operator")
        tail = _stderr_tail(stderr_path)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg could not decode {os.path.basename(source)}: {tail[-800:]}")
        _check_decode(tail, os.path.basename(source))
        out.append(str(dest))
    return out


#: what an output may differ from the summed probed sources by: container rounding is a few tens of
#: milliseconds plus a frame or so per joined file; 0.2% keeps the allowance long books always had
DURATION_SLACK = 2.0
DURATION_SLACK_PER_FILE = 0.05
DURATION_SLACK_RATIO = 0.002


def duration_slack(total: float, n_files: int) -> float:
    return max(DURATION_SLACK + DURATION_SLACK_PER_FILE * n_files, total * DURATION_SLACK_RATIO)


def encode(
    unit: BookUnit,
    match,
    book: dict,
    work_dir: Path,
    out_path: Path,
    log=print,
    copy_audio: bool = False,
    plan: ChapterPlan | None = None,
    fmt: str = "m4b",
    publish: bool = True,
    sequence: str = "",
) -> EncodeResult:
    """Run ffmpeg. Raises RuntimeError when the result fails verification.

    ``fmt`` is "m4b" (default, unchanged) or "mp3" (one file, chapters written as ID3 CHAP
    frames). With ``publish=False`` the verified file stays in ``work_dir`` and
    ``result.path`` points at it; ``publish_result`` moves it into place later.

    ``plan`` is the chapter plan the caller already built (``plan_chapters``); it is only
    worked out here when the caller did not pass one.
    """
    try:
        return _encode(unit, match, book, work_dir, out_path, log=log, copy_audio=copy_audio, plan=plan,
                       fmt=fmt, publish=publish, sequence=sequence)
    finally:
        # CORE-01c: a mixed-codec join's PCM goes however the encode ends, also for a direct caller
        # with no pipeline cleanup around it (a no-op when nothing was decoded)
        _release_parts(work_dir / "parts")


def _encode(unit, match, book, work_dir, out_path, log, copy_audio, plan, fmt, publish, sequence):
    work_dir.mkdir(parents=True, exist_ok=True)
    if plan is None:
        plan = plan_chapters(unit, match.asin)
    total_duration = plan.total
    chapters, source = plan.chapters, plan.source
    log(f"  chapters: {len(chapters)} from {source}")

    unsafe = unsafe_path_reason(unit)
    if unsafe:  # process_unit holds these first; never let one reach the concat list
        raise RuntimeError(unsafe)
    joined = list(unit.files)
    if len(joined) > 1 and len(plan.codecs or ()) > 1:
        # the concat demuxer decodes every part with the first part's decoder: a FLAC after an MP3
        # is thrown away packet by packet with exit 0 (CORE-01). One codec skips all of this.
        log(f"  audio: mixed codecs {sorted(plan.codecs)} - decoding each file to PCM before joining")
        # CORE-01c: never start a decode the scratch disk cannot hold to the end
        check_pcm_space(total_duration, work_dir)
        _claim_parts(work_dir / "parts")
        joined = _normalise_parts(joined, work_dir)
        copy_audio = False
    concat_file = work_dir / "concat.txt"
    with concat_file.open("w", encoding="utf-8") as handle:
        for path in joined:
            handle.write("file '" + str(path).replace("'", "'\\''") + "'\n")

    metadata_file = work_dir / "meta.txt"
    tags = write_ffmetadata(metadata_file, book, match, chapters)

    cover_file = work_dir / "cover.jpg"
    has_cover = catalog.download(catalog.cover_url(book, match.asin), str(cover_file))
    log(f"  cover: {'ok' if has_cover else 'not found'}")

    temp_out = work_dir / f"output.{fmt}"
    mp3 = fmt == "mp3"
    if mp3:
        copy_audio = False  # a remux keeps AAC samples; an mp3 is always an encode
    bitrate = 128
    if copy_audio:
        # already AAC in an MP4 container: keep the samples, only rewrite the wrapper
        log("  audio: copied (source is already AAC) - no re-encode, no quality loss")
    else:
        source_bytes = sum(os.path.getsize(f) for f in unit.files if os.path.exists(f))
        source_kbps = (source_bytes * 8 / total_duration / 1000) if total_duration else 0
        eff_bitrate = effective_val("audio_bitrate")
        bitrate = choose_bitrate(source_kbps, eff_bitrate)
        if mp3:
            bitrate = mp3_bitrate(bitrate)
        log(f"  audio: {'mp3' if mp3 else 'aac'} {bitrate}k (source {source_kbps:.0f}k, floor {eff_bitrate})")
    command = _nice_prefix() + build_command(
        fmt,
        concat_file,
        cover_file,
        metadata_file,
        temp_out,
        has_cover,
        copy_audio=copy_audio,
        bitrate=bitrate,
        tags=mp3_tags(tags, sequence) if mp3 else None,
    )

    # Popen (not run) so the status page can abort mid-book and show a progress bar.
    # stderr goes to a real file, not a pipe: a pipe nobody drains fills at 64 KB and a
    # chatty ffmpeg then blocks forever on its next write. The file is read once ffmpeg
    # has exited, so the tail is always complete (no reader thread, nothing to join).
    stderr_path = work_dir / "ffmpeg-stderr.log"
    with stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
    CONTROL.attach_process(proc)

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
        # the PCM parts (about 635 MB an hour) are spent once joined
        _release_parts(work_dir / "parts")

    errors = [_stderr_tail(stderr_path)]
    if CONTROL.stop_requested:
        raise Stopped("stopped by operator")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(errors[0] if errors else '')[-800:]}")
    # exit 0 is not proof: a decoder that rejected a whole file's packets still exits cleanly
    _check_decode(errors[0], "the sources")

    if mp3:
        # ffmpeg drops chapters for mp3, so they are written into its ID3 tag here
        how = id3.write_chapters(temp_out, chapters)
        log(f"  chapters: {len(chapters)} written as ID3 CHAP frames ({how})")
    duration = float(probe(temp_out).duration)
    chapter_count = len(probe_chapters(temp_out))
    streams = probe_streams(temp_out)
    want_codec = "mp3" if mp3 else "aac"
    audio = [s for s in streams if s.get("codec_name") == want_codec]
    if not audio:
        raise RuntimeError(f"output has no {want_codec.upper()} audio stream")
    try:
        # the audio itself, not the container: a chapter track can carry the container to the
        # planned length while the audio stops short (CORE-01)
        duration = float(audio[0].get("duration") or 0) or duration
    except (TypeError, ValueError):
        pass
    # total_duration is the sum of the probed sources; a merge of the same audio matches it to
    # within container rounding, so seconds missing from a short book fail too (CORE-01)
    if abs(duration - total_duration) > duration_slack(total_duration, len(unit.files)):
        hint = ""
        for line in (errors[0] if errors else "").splitlines():
            low = line.lower()
            if any(word in low for word in ("invalid", "error", "could not", "failed")):
                hint = " | ffmpeg: " + line.strip()[:160]
                break
        raise RuntimeError(
            f"output is {duration / 3600:.2f}h but the sources are {total_duration / 3600:.2f}h - "
            f"files were dropped during the merge{hint}"
        )
    if chapter_count != len(chapters):
        raise RuntimeError(f"expected {len(chapters)} chapters, found {chapter_count}")
    if has_cover and not has_embedded_cover(temp_out):
        raise RuntimeError("cover art is missing from the output")

    result = EncodeResult(
        path=temp_out,
        duration=duration,
        size=temp_out.stat().st_size,
        chapters=chapter_count,
        chapters_source=source,
        cover=cover_file if has_cover else None,
    )
    if publish:
        publish_result(result, out_path)
    return result


def _place(source: Path, dest: Path, move: bool) -> None:
    """Put ``source`` at ``dest`` so that ``dest`` is never a partial file.

    A move on the same filesystem is a single rename. Otherwise the bytes go to a
    temporary file in ``dest``'s own directory (same filesystem, so the final rename is
    atomic), are fsynced, and only then renamed over ``dest``; any failure removes the
    temporary file and re-raises, leaving ``dest`` as it was.
    """
    if move:
        try:
            os.replace(source, dest)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    fd, temp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle, open(source, "rb") as src:
            shutil.copyfileobj(src, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copystat(source, temp)
        os.replace(temp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    if move:
        os.unlink(source)


def publish_result(result: EncodeResult, out_path: Path, placed: list | None = None) -> EncodeResult:
    """Move a verified encode into the library (and drop cover.jpg beside it).

    Atomic: the library never holds a half-written file, and cover.jpg is only
    written once the audio is in place. ``placed`` (optional) gets each path appended the
    moment it is in place, so a caller rolling back knows exactly what this call created.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _place(Path(result.path), out_path, move=True)
    if placed is not None:
        placed.append(out_path)
    if result.cover is not None:
        _place(Path(result.cover), out_path.parent / "cover.jpg", move=False)
        if placed is not None:
            placed.append(out_path.parent / "cover.jpg")
    result.path = out_path
    result.size = out_path.stat().st_size
    return result


def write_abs_metadata(path: Path, book: dict, match, chapters: list[dict], duration: float) -> dict:
    """The metadata.json shape Audiobookshelf reads (and rewrites on its own scans)."""
    product = match.product
    series_primary = book.get("seriesPrimary") or {}
    data = {
        "tags": [g.get("name") for g in (book.get("genres") or []) if g.get("name")][:8],
        "chapters": [
            {"start": c["start"], "end": c["end"], "title": c["title"], "id": i}
            for i, c in enumerate(chapters)
        ],
        "title": book.get("title") or (product.title if product else ""),
        "subtitle": book.get("subtitle") or (product.subtitle if product else "") or None,
        "authors": [a.get("name") for a in (book.get("authors") or []) if a.get("name")]
        or (product.authors if product else []),
        "narrators": [n.get("name") for n in (book.get("narrators") or []) if n.get("name")]
        or (product.narrators if product else []),
        "series": [f"{series_primary['name']} #{series_primary.get('position', '')}".strip()]
        if series_primary.get("name")
        else [],
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
    # SEC-03: a temp file beside it, then a rename over it - a rename replaces a symlink at
    # ``path`` instead of writing through it (write_text would follow it). O_EXCL refuses a
    # planted temp name; 0o666 keeps the umask-based mode write_text gave. SEC-03b: an existing
    # regular sidecar keeps its own mode (lstat: a symlink lends nothing - it is replaced).
    try:
        old = os.lstat(path)
        keep_mode = stat.S_IMODE(old.st_mode) if stat.S_ISREG(old.st_mode) else None
    except FileNotFoundError:
        keep_mode = None
    temp = path.parent / f".{path.name}.{os.urandom(6).hex()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if keep_mode is not None:
                os.fchmod(handle.fileno(), keep_mode)
            handle.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    return data

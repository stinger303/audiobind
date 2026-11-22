"""One unit in, one finished book out: audit and convert."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, encode, hardcover, ingest, library, matching
from . import overrides as overrides_mod
from .config import CONFIG, effective_auto_tiers, effective_val
from .control import CONTROL, Stopped
from .probe import ProbeTimeout, probe
from .state import SOURCE_INGEST, State, UnitState
from .units import BookUnit, looks_like_collection, unsafe_path_reason
from .util import norm

#: the daemon and the manual "Retry / Convert anyway" buttons must not encode two books at once
CONVERT_LOCK = threading.Lock()

#: Audit.name_source values, recorded in the detail's match block
NAME_SOURCE_FOLDER = "folder"
NAME_SOURCE_EMBEDDED = "embedded"


@dataclass
class Audit:
    unit: BookUnit
    hours: float = 0.0
    codecs: set[str] = field(default_factory=set)
    channels: set[str] = field(default_factory=set)
    #: AB-MONO: each file's probed sample rate (Hz, as ffprobe gives it), read by choose_action
    sample_rates: set[str] = field(default_factory=set)
    unreadable: int = 0
    match: matching.Match = field(default_factory=matching.Match)
    confidence: str = "NONE"
    action: str = "review"
    notes: list[str] = field(default_factory=list)
    chapters_available: int = 0
    src_mb: float = 0.0
    #: operator metadata corrections for this unit, from the state DB
    overrides: dict = field(default_factory=dict)
    #: each source file's probed length, reused by the chapter plan (no second ffprobe)
    durations: dict = field(default_factory=dict)
    #: where convert_unit would file the book, and whether it was already there
    out_path: str = ""
    already_published: bool = False
    #: which names the catalog match was searched with: "folder" (the folder/file name, as
    #: always) or "embedded" (an ingest unit's own audio tags, after the folder found nothing)
    name_source: str = NAME_SOURCE_FOLDER
    #: Hardcover's second opinion (hardcover.evidence_for) for the detail panel; None while it is off.
    #: Never read by the grade or the action
    hardcover: dict | None = None
    #: CORE-04a: the names (in the book's folder) the last publish placed, the backups it renamed
    #: aside (original name -> backup name), and the merged record ({name: [size, mtime_ns]},
    #: CORE-04d) kept as detail.output.wrote / wrote_id
    placed: list = field(default_factory=list)
    moved_aside: dict = field(default_factory=dict)
    wrote: dict | None = None
    #: Q-B12: the Audnex record convert_unit fetched for this match (before the overrides), so the
    #: recorded series is the one the tags were written from; None when it never asked
    audnex: dict | None = None

    @property
    def runtime_matches(self) -> bool:
        if not self.match.product:
            return True
        return matching.runtime_ok(self.hours, self.match.product.hours, effective_val("max_runtime_delta"))


def audit_unit(unit: BookUnit, asin_override: str = "", pin_failures: int = 0) -> Audit:
    """Read-only assessment: durations, codecs, catalog match, what we would do.

    ``pin_failures``: how many checks in a row ``asin_override`` has had no catalog data (see
    matching.PIN_GIVE_UP_AFTER)."""
    audit = Audit(unit=unit)
    total = 0.0
    timed_out = 0
    for path in unit.files:
        try:
            info = probe(path, raise_on_timeout=True)
        except ProbeTimeout:
            # never measured, so never recorded: a stored 0.0 would be trusted by plan_chapters (the
            # key is present) and a forced conversion would drop every source chapter. With the key
            # missing, plan_chapters re-probes and fails the book instead
            timed_out += 1
            audit.unreadable += 1
            continue
        audit.durations[path] = info.duration
        total += info.duration
        if info.codec:
            audit.codecs.add(info.codec)
        if info.channels:
            audit.channels.add(str(info.channels))
        if info.sample_rate:
            audit.sample_rates.add(str(info.sample_rate))
        if not info.ok:
            audit.unreadable += 1
    # a total with an unmeasured file in it is not the book's length: unknown (0), the same as when
    # nothing could be read, rather than a short figure that matching and the runtime check would use
    audit.hours = 0.0 if timed_out else round(total / 3600, 2)
    if timed_out:
        audit.notes.append(f"ffprobe timed out on {timed_out} file(s): runtime unknown")
    src_bytes = 0
    for f in unit.files:
        try:
            src_bytes += os.path.getsize(f)
        except OSError:
            continue  # removed since the folder was listed: left out, never a failed scan
    audit.src_mb = round(src_bytes / 1e6, 1)

    audit.match = matching.match_unit(unit, asin_override, local_hours=audit.hours, pin_failures=pin_failures)
    abandoned = audit.match.pin_abandoned
    if abandoned:
        audit.notes.append(
            f"ASIN {abandoned} you set had no catalog data on {pin_failures + 1} checks in a row - "
            "matched without it; not converted automatically"
        )
    searched = not asin_override or bool(abandoned)
    if unit.source == SOURCE_INGEST and searched and audit.match.confidence in ("NONE", "LOW"):
        # a release name can match the wrong book at LOW as easily as it matches nothing, so the
        # files' own tags get a say in both cases - but only a genuinely better match replaces
        # the folder's
        embedded = _match_embedded(unit, audit.hours)
        if embedded is not None and _embedded_is_better(embedded, audit.match):
            audit.match = embedded
            audit.name_source = NAME_SOURCE_EMBEDDED
            audit.notes.append("matched on the audio file's embedded tags")
        elif embedded is not None and embedded.asin != audit.match.asin:
            audit.notes.append(
                f"embedded tags suggest {embedded.product.title} ({embedded.confidence}) - "
                "kept the folder match"
            )
    audit.confidence = audit.match.confidence
    if audit.match.product and audit.match.unasked_adaptation:
        audit.notes.append(
            f"catalog hit is {audit.match.unasked_adaptation} and your folder does not say so - "
            "not graded HIGH"
        )
    if audit.match.product and audit.match.missing_adaptation:
        audit.notes.append(
            f"your folder says {audit.match.missing_adaptation} and the catalog hit is not one - "
            "not graded HIGH"
        )
    if audit.match.product and audit.match.edition_conflict:
        audit.notes.append(f"{audit.match.edition_conflict} - graded LOW, not converted automatically")
    if audit.match.product and audit.match.veto:
        audit.notes.append(f"{audit.match.veto} - graded LOW, not converted automatically")
    if audit.confidence == "NONE" and audit.match.rejected:
        audit.notes.append(audit.match.rejected)

    # each file's own length; nothing when one went unmeasured (the total is unknown then too)
    file_hours = [] if timed_out else [audit.durations[f] / 3600 for f in unit.files if f in audit.durations]
    if looks_like_collection(unit, file_hours) and audit.confidence in ("LOW", "NONE"):
        audit.confidence = "COLLECTION"

    if audit.confidence in ("HIGH", "MEDIUM"):
        audit.chapters_available = len(catalog.chapters(audit.match.asin))

    if len(audit.codecs) > 1:
        audit.notes.append(f"mixed codecs {sorted(audit.codecs)}")
    if len(audit.channels) > 1:
        audit.notes.append(f"mixed channels {sorted(audit.channels)}")
    if audit.unreadable:
        audit.notes.append(f"{audit.unreadable} unreadable file(s)")
    if unit.multi_part:
        audit.notes.append(f"{len(unit.parts)} parts combined")
    searched_author = unit.author_guess or (
        audit.match.query_author if audit.name_source == NAME_SOURCE_EMBEDDED else ""
    )
    if not searched_author and audit.match.product:
        audit.notes.append("author taken from catalog")
    if audit.match.product and not audit.runtime_matches:
        audit.notes.append(f"runtime {audit.hours}h vs catalog {audit.match.product.hours}h")
    elif unit.multi_part and audit.match.product:
        audit.notes.append("parts verified complete")

    audit.action = choose_action(unit, audit.confidence, audit.runtime_matches, audit.codecs,
                                 channels=audit.channels, sample_rates=audit.sample_rates)
    if audit.action == "convert" and _single_aac_mp4(unit, audit.codecs):
        # AB-MONO-CAP: say why an AAC book that could have been copied is re-encoded instead
        audit.notes.append(
            f"audio is {_shape_text(audit.channels, audit.sample_rates)}, not within the configured caps "
            f"{CONFIG.audio_channels} ch / {CONFIG.sample_rate} Hz - re-encoded, not copied"
        )
    # evidence only: asked after the grade and the action are final, and nothing above reads it.
    # None (no request, no cache file) unless HARDCOVER_EVIDENCE is on and a token is set
    audit.hardcover = hardcover.evidence_for(audit.match.product)
    return audit


_TIER_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
#: at equal confidence the tags' match must beat the folder's score by this much to replace it
EMBEDDED_SCORE_EDGE = 0.10


def _embedded_is_better(embedded: matching.Match, folder: matching.Match) -> bool:
    """True when the embedded-tag match should replace the folder-name match.

    A higher tier always wins (MEDIUM/HIGH over a LOW folder match, anything over NONE); at the
    same tier the embedded score must be clearly higher. Otherwise the folder match stands.
    """
    rank_e = _TIER_RANK.get(embedded.confidence, 0)
    rank_f = _TIER_RANK.get(folder.confidence, 0)
    if rank_e != rank_f:
        return rank_e > rank_f
    return embedded.score >= folder.score + EMBEDDED_SCORE_EDGE


def _match_embedded(unit: BookUnit, local_hours: float) -> matching.Match | None:
    """INGEST ONLY, after the folder name matched nothing or only at LOW: retry with the files' own tags.

    A torrent name is often junk while a single m4b carries correct title/artist tags. Returns
    the retry's match when it found something (any tier - the caller decides with
    _embedded_is_better whether it replaces the folder's, and the usual grading and auto-convert
    rules still apply), else None so the folder's match stands. The unit itself is not changed.
    """
    author, title = ingest.embedded_names(unit)
    if not (author or title):
        return None
    # the folder name's swapped reading belongs to the folder name, not to the tags
    retry_unit = dataclasses.replace(
        unit, author_guess=author or unit.author_guess, title_guess=title or unit.title_guess,
        alt_author_guess="", alt_title_guess="",
    )
    if (norm(retry_unit.author_guess), norm(retry_unit.title_guess)) == (
        norm(unit.author_guess),
        norm(unit.title_guess),
    ):
        return None  # the tags say what the folder said: the same search would find nothing again
    retry = matching.match_unit(retry_unit, local_hours=local_hours)
    return retry if retry.confidence != "NONE" else None


def _chapter_plan(unit: BookUnit, asin: str, audit: Audit) -> encode.ChapterPlan:
    """The encoder's chapter plan from the audit's probe: one ffprobe per file, shared by every format.

    AB-MONO-REENC: the probed channel counts and rates ride along, so a re-encode writes min(source,
    cap) rather than the caps. With no durations (nothing measured) plan_chapters probes itself."""
    probed = bool(audit.durations)
    return encode.plan_chapters(
        unit,
        asin,
        durations=audit.durations or None,
        codecs=set(audit.codecs) if probed else None,
        channels=set(audit.channels) if probed else None,
        sample_rates=set(audit.sample_rates) if probed else None,
    )


def _single_aac_mp4(unit: BookUnit, codecs: set[str]) -> bool:
    """One AAC file already in an MP4 container: the only source a remux (``-c:a copy``) can take."""
    return (
        len(unit.files) == 1 and Path(unit.files[0]).suffix.lower() in (".m4b", ".m4a") and codecs == {"aac"}
    )


def _within_cap(figures: set[str], cap: int) -> bool:
    """Every probed figure is a whole number at or below ``cap``. An empty set (ffprobe gave no
    figure) or an unreadable one is *not* within the cap: deliberately conservative, an unprobed
    shape is re-encoded rather than copied on trust."""
    if not figures:
        return False
    try:
        return all(0 < int(figure) <= cap for figure in figures)
    except ValueError:
        return False


def _shape_within_caps(channels: set[str], sample_rates: set[str]) -> bool:
    """AB-MONO-CAP: the probed audio is at or below AUDIO_CHANNELS and at or below SAMPLE_RATE.

    Both settings are caps, not targets. ffmpeg ignores ``-ac``/``-ar`` beside ``-c:a copy`` (exit 0,
    no warning), so a copy keeps the source's shape whatever is configured - which is right while
    the source is within both caps (a mono book is never upmixed, a 22050 Hz book never has its rate
    raised) and wrong only when it exceeds one, which is what a re-encode is for."""
    return _within_cap(channels, CONFIG.audio_channels) and _within_cap(sample_rates, CONFIG.sample_rate)


def _shape_text(channels: set[str], sample_rates: set[str]) -> str:
    return f"{'/'.join(sorted(channels)) or '?'} ch / {'/'.join(sorted(sample_rates)) or '?'} Hz"


def choose_action(
    unit: BookUnit,
    confidence: str,
    runtime_matches: bool,
    codecs: set[str],
    *,
    channels: set[str] | None = None,
    sample_rates: set[str] | None = None,
) -> str:
    """What to do with this unit. Pure function so the rules can be tested directly.

    A single AAC file that is already an MP4 container is remuxed, not re-encoded: chapters, tags
    and cover are rewritten while every audio sample is copied untouched - but only when its probed
    ``channels`` are at or below AUDIO_CHANNELS and its ``sample_rates`` at or below SAMPLE_RATE
    (AB-MONO-CAP: the settings are caps). A copy cannot change either, so a source above a cap is
    re-encoded; a source within both is copied and keeps its own shape. An unprobed figure counts
    as above the cap.

    ``channels``/``sample_rates`` left at None means the caller has no probe and the shape is not
    checked: only the offline match bench (no audio in its corpus) calls it that way. audit_unit
    always passes both.
    """
    already_mp4 = _single_aac_mp4(unit, codecs)
    if already_mp4 and channels is not None and sample_rates is not None:
        already_mp4 = _shape_within_caps(channels, sample_rates)
    if confidence not in ("HIGH", "MEDIUM"):
        return "review"
    if not runtime_matches:
        return "hold"
    if already_mp4:
        return "remux"
    return "merge+convert" if unit.multi_part else "convert"


def work_dir_for(unit: BookUnit) -> Path:
    """The book's scratch directory under WORK_DIR: the same name for the same book in every
    process. (``hash()`` of a str is salted per process, so a restart used to pick a new name and
    strand the old directory where nothing would ever find or delete it.)"""
    return CONFIG.work_dir / hashlib.sha1(unit.state_key.encode("utf-8")).hexdigest()[:8]


def _missing_formats(formats: list[str], paths: dict, reencode: bool) -> list[str]:
    """CORE-04: the requested formats still to make - every one for a re-encode, else those not on disk."""
    return list(formats) if reencode else [fmt for fmt in formats if not paths[fmt].exists()]


def missing_beside(primary: Path) -> list[str]:
    """The requested formats with no file beside ``primary`` (same folder and name, its own extension)."""
    formats = encode.formats_for(effective_val("output_format"))
    return _missing_formats(formats, {fmt: primary.with_suffix(f".{fmt}") for fmt in formats}, False)


def _refuse_uncontained(destinations: list[Path]) -> None:
    """CORE-04b: every file a conversion would write must land in a book folder inside OUTPUT_ROOT.

    Each destination is judged where the write really goes - its folder resolved, the name itself
    not followed (a placement renames over a link, it never writes through one) - so a saved anchor
    that is a link into the root cannot carry its siblings outside it. A book is never filed in the
    root itself either: the layout always puts it at least one folder down.
    """
    root = Path(CONFIG.output_root).resolve()
    for path in destinations:
        folder = path.parent.resolve()
        if folder == root or root not in folder.parents:
            raise ValueError(f"refused: {path.name} would be written outside a book folder in the output "
                             f"root ({path.parent}) - nothing was written")


def convert_unit(audit: Audit, log=print, cancelled=None, heartbeat=None, reencode: bool = False,
                 at: Path | None = None):
    """Encode, verify and file one book. Returns the EncodeResult (or None in a dry run).

    "Already published" means every requested format is on disk (CORE-04): when only some are,
    just the missing ones are encoded and placed beside the ones already there, which are left
    untouched. ``at`` is a published file of this book to file them beside (a book kept where an
    older layout put it) instead of the current layout's path, so it never gets a second copy.

    ``cancelled`` (optional, no arguments) is asked again once the encoder is free: True means
    the book was removed from Audiobind while it waited, and nothing is encoded. ``heartbeat``
    (optional) is called, throttled, from the encode's progress so one long encode never makes
    the healthcheck read the daemon as stalled. ``reencode`` (the manual Convert anyway) encodes
    even when the output already exists; the old files are renamed aside to ``.bak-<timestamp>``
    and the verified result takes their place (restored if the publish fails - see _publish_all).
    """
    unit, match = audit.unit, audit.match
    if not match.product:
        raise ValueError("cannot convert without a catalog match")

    book = catalog.book(match.asin)
    audit.audnex = book
    # the operator's corrections are written into the catalog payload before anything reads it,
    # so tags, metadata.json and the filing layout all agree with what the panel shows
    book = overrides_mod.apply(book, audit.overrides)
    series = (book.get("seriesPrimary") or {}).get("name", "")
    sequence = str((book.get("seriesPrimary") or {}).get("position", "") or "")
    authors = [a.get("name") for a in (book.get("authors") or []) if a.get("name")] or match.product.authors
    title = book.get("title") or match.product.title

    formats = encode.formats_for(effective_val("output_format"))
    # the first format is the book's main file: the unit's output
    if at is None:
        paths = {fmt: library.m4b_path(series, authors, title, sequence, ext=fmt) for fmt in formats}
    else:
        paths = {fmt: at.with_suffix(f".{fmt}") for fmt in formats}
        # CORE-04b: m4b_path checks each path it builds; a saved anchor's siblings are checked
        # here, every one, before anything is encoded, backed up or placed
        _refuse_uncontained([*paths.values(), at.parent / "cover.jpg", at.parent / "metadata.json"])
    out_path = paths[formats[0]]
    audit.out_path = str(out_path)
    todo = _missing_formats(formats, paths, reencode)
    if not todo:
        audit.already_published = True
        log(f"  already published: {out_path}")
        return None
    if CONTROL.dry_run:
        for fmt in todo:
            log(f"  [dry-run] would write {paths[fmt]}")
        return None  # nothing was written, so it is not "converted"

    work_dir = work_dir_for(unit)
    with CONVERT_LOCK:
        # checked again under the lock: a manual action and the daemon can both reach
        # this point for the same book, and only the first may encode it (unless asked to re-encode)
        todo = _missing_formats(formats, paths, reencode)
        if not todo:
            audit.already_published = True
            log(f"  already published (while waiting for the encoder): {out_path}")
            return None
        if todo != formats:
            log(f"  {', '.join(f for f in formats if f not in todo).upper()} already published; "
                f"adding {', '.join(todo).upper()}")
        if cancelled is not None and cancelled():
            log("  removed from Audiobind while waiting for the encoder - not converted")
            return None
        # CORE-01c: PCM a hard-killed join left under any book's work directory (it can be tens of GB)
        for stale in encode.reclaim_stale_parts():
            log(f"  reclaimed stranded PCM parts: {stale}")
        # a crashed or restarted run leaves its work directory behind under this same name:
        # start clean, so none of its partial files can be taken for this encode's output
        shutil.rmtree(work_dir, ignore_errors=True)
        CONTROL.begin_unit(unit.state_key, title)
        CONTROL.set_heartbeat(heartbeat)
        try:
            # one chapter plan (one ffprobe per file) shared by the encoder and metadata.json
            plan = _chapter_plan(unit, match.asin, audit)
            results = {}
            for fmt in todo:
                log(f"  encoding {fmt.upper()}")
                # nothing is published until every format has encoded and verified: a failed
                # second encode fails the unit and leaves the library untouched, never an
                # m4b without the mp3 the owner asked for
                results[fmt] = encode.encode(
                    unit,
                    match,
                    book,
                    work_dir / fmt,
                    paths[fmt],
                    log=log,
                    copy_audio=(audit.action == "remux" and fmt == "m4b"),
                    plan=plan,
                    fmt=fmt,
                    publish=False,
                    sequence=sequence,
                )
            duration = results.get(formats[0], results[todo[0]]).duration
            # CORE-03: metadata.json is written inside the publish, so a failed sidecar rolls
            # back the audio and cover with it, and the previous metadata comes back too
            placed, backups = _publish_all(todo, results, paths, log=log,
                                           sidecar=lambda: _write_sidecar(out_path, book, match, plan.chapters,
                                                                          duration, log=log))
            audit.placed = [path.name for path in placed]
            audit.moved_aside = {path.name: backup.name for path, backup in backups.items()}
            for fmt in todo:
                res = results[fmt]
                log(f"  published {paths[fmt]} ({res.size / 1e6:.0f} MB, {res.chapters} chapters)")
            if formats[0] in results:
                return results[formats[0]]
            # the main file was already there and is still the unit's output
            return dataclasses.replace(results[todo[0]], path=out_path, size=out_path.stat().st_size,
                                       cover=None)
        finally:
            # a stopped encode leaves only the work directory, which is deleted here, so the
            # library never sees a partial file and the book is simply retried next scan
            shutil.rmtree(work_dir, ignore_errors=True)
            CONTROL.set_heartbeat(None)
            CONTROL.end_unit()


def _write_sidecar(out_path: Path, book: dict, match, chapters: list[dict], duration: float, log=print) -> bool:
    """SEC-03: metadata.json beside the published book - only inside OUTPUT_ROOT and never
    through a symlink. A refused sidecar is logged and skipped; the conversion stands.
    True when it was written (CORE-04a: it is then one of the files Audiobind wrote)."""
    sidecar = out_path.parent / "metadata.json"
    root = Path(CONFIG.output_root).resolve()
    if root not in sidecar.parent.resolve().parents:
        log(f"  metadata.json skipped: {sidecar.parent} is outside the output root")
        return False
    if sidecar.is_symlink():
        log(f"  metadata.json skipped: {sidecar} is a symlink")
        return False
    encode.write_abs_metadata(sidecar, book, match, chapters, duration)
    return True


def _backup_path(path: Path, stamp: str) -> Path:
    """A free ``<path>.bak-<stamp>[-n]`` name beside ``path`` (same filesystem, so a rename)."""
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


def _publish_all(formats: list[str], results: dict, paths: dict, log=print,
                 sidecar=None) -> tuple[list[Path], dict[Path, Path]]:
    """Publish every verified format, the m4b (archival copy) first - all of them or none.

    If a later format fails to publish (a cross-filesystem copy that runs out of space), the
    files this call already placed are removed again, with a cover.jpg and any folders it
    created, so the library never keeps an m4b without the mp3 the owner asked for - which the
    next scan would otherwise take as "already published" and never finish. Files that were
    there before are never removed.

    An output that already exists (a Convert anyway, or a *different* source whose catalog
    metadata resolves to the same path) is first renamed aside to ``<path>.bak-<timestamp>`` -
    every format, plus the cover.jpg that belongs to that edition. A failed publish renames
    them back, so the previous edition is left exactly as it was; a successful one keeps them
    (logged) so the replaced book can still be recovered by hand.

    ``sidecar`` (optional, no arguments) writes metadata.json once every format is placed, as
    part of the same unit (CORE-03): an existing regular metadata.json is first copied to its own
    ``.bak-<timestamp>`` (a copy, not a rename - the writer may decline to write it), and a failed
    publish - the sidecar write included - puts it back with the audio and cover. The copy is kept
    beside a replaced edition's backups and removed when no audio was replaced.

    Returns (every path this call placed, in order - audio, cover.jpg, a metadata.json the
    sidecar wrote; the backups kept, original -> backup) for the row's record of what Audiobind
    wrote (CORE-04a).
    """
    folder = paths[formats[0]].parent
    new_dirs = []
    probe_dir = folder
    root = Path(CONFIG.output_root)
    while not probe_dir.exists() and probe_dir not in (root, probe_dir.parent):
        new_dirs.append(probe_dir)  # deepest first
        probe_dir = probe_dir.parent
    cover = folder / "cover.jpg"
    meta = folder / "metadata.json"
    # CORE-02b: lexists - a dangling symlink is there, and not this call's to remove
    preexisting = {path for path in [*paths.values(), cover, meta] if os.path.lexists(path)}
    to_back_up = [paths[fmt] for fmt in formats if paths[fmt] in preexisting]
    if cover in preexisting and any(results[fmt].cover is not None for fmt in formats):
        # publishing overwrites cover.jpg - also when only a missing format is added beside a kept
        # file (CORE-03b), so the existing art is always moved aside first and a rollback restores it
        to_back_up.append(cover)
    replacing = bool(to_back_up)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backups: dict[Path, Path] = {}
    placed: list[Path] = []  # CORE-02b: only what publish_result actually put in place
    try:
        for path in to_back_up:
            backup = _backup_path(path, stamp)
            os.rename(path, backup)
            backups[path] = backup
        if sidecar is not None and meta.is_file() and not meta.is_symlink():
            backup = _backup_path(meta, stamp)
            shutil.copy2(meta, backup)  # content and mode (SEC-03b) both come back on restore
            backups[meta] = backup
        for fmt in formats:
            # CORE-02: publish_result places the audio, then the cover, then stats it, so it
            # can fail with the audio already visible. CORE-02b: it records each path the moment
            # it is in place - a path whose placement failed (or that another writer made after
            # the snapshot above) is never on the list, so the rollback never unlinks it.
            encode.publish_result(results[fmt], paths[fmt], placed=placed)
        if sidecar is not None and sidecar():
            placed.append(meta)  # CORE-04a: written by this publish (the last step - nothing to undo)
    except BaseException:
        for path in dict.fromkeys(placed):
            if path not in preexisting:
                try:
                    path.unlink()
                except OSError:
                    pass
        for path, backup in backups.items():
            try:
                os.replace(backup, path)  # over whatever this run placed there
            except OSError as exc:
                log(f"  could not restore {path} from {backup}: {exc}")
        for directory in new_dirs:
            try:
                directory.rmdir()  # only ever an empty folder this call made
            except OSError:
                break
        raise
    if not replacing and meta in backups:
        # no edition was replaced, so there is nothing for the old metadata to pair with
        with contextlib.suppress(OSError):
            backups.pop(meta).unlink()
    for path, backup in backups.items():
        log(f"  replaced {path.name}; the previous file is kept at {backup}")
    return list(dict.fromkeys(placed)), backups


def recorded_output(row) -> dict:
    """The ``output`` block of a row's detail JSON ({} when there is none)."""
    try:
        detail = json.loads(row.detail or "{}")
    except (TypeError, ValueError):
        return {}
    output = detail.get("output") if isinstance(detail, dict) else None
    return output if isinstance(output, dict) else {}


def wrote_output(row) -> bool:
    """True only for a row whose ``output`` is a book Audiobind itself encoded and published.

    A "ready" row (already published) records a file that was there before the scan, so it is
    never Audiobind's to remove. A converted row whose detail names a different output path
    (the row was edited, or moved by hand) does not prove ownership of ``output`` either.
    Which files in that folder are Audiobind's is ``wrote_files``.
    """
    if row.status != "converted" or not row.output:
        return False
    path = recorded_output(row).get("path")
    if path:
        try:
            return Path(str(path)).resolve() == Path(row.output).resolve()
        except (OSError, RuntimeError):
            return False
    return True


def file_identity(path: Path) -> list[int] | None:
    """CORE-04d: [size, mtime_ns] of ``path`` (a link itself, never followed); None when absent."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _is_identity(value) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value))


def wrote_files(row) -> dict[str, list[int]] | None:
    """CORE-04a/04d: the files Audiobind wrote in the output's folder - {name relative to it:
    [size, mtime_ns] when it was written}.

    {} for a row whose output is not Audiobind's at all; None for a converted row whose record is
    missing or unreadable (no ``wrote`` list, or one without the ``wrote_id`` identities) - unknown
    provenance, which proves no more than ``legacy_owned`` allows (CORE-04c).
    """
    if not wrote_output(row):
        return {}
    output = recorded_output(row)
    names, ids = output.get("wrote"), output.get("wrote_id")
    if isinstance(names, list) and all(isinstance(name, str) for name in names) and isinstance(ids, dict):
        return {name: ids[name] for name in names if _is_identity(ids.get(name))}
    return None


def folder_files(folder: Path) -> list[str]:
    """Every file (and link - never followed) under ``folder``, as sorted '/'-separated names
    relative to it."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        links = [d for d in dirnames if (here / d).is_symlink()]
        for name in [*filenames, *links]:
            found.append((here / name).relative_to(folder).as_posix())
    return sorted(found)


#: the companions a publish writes beside the audio
COMPANIONS = ("cover.jpg", "metadata.json")
#: why a file in a folder with unknown provenance was left (CORE-04c)
UNATTRIBUTED = ("Audiobind has no record of the files it wrote for this book, so this file "
                "could not be attributed to it")
NOT_WRITTEN = "Audiobind did not write this file"
#: why a recorded name was left: the file there is no longer the one Audiobind wrote (CORE-04d)
CHANGED = "this file changed since Audiobind wrote it (its size or modification time differs)"


def legacy_owned(present: list[str], output_name: str) -> set[str]:
    """CORE-04c: what a converted row without a usable list proves Audiobind wrote, of the
    ``present`` names in its folder: its recorded output file, and cover.jpg / metadata.json
    only when the folder holds nothing else - unknown provenance is not ownership."""
    owned = {output_name} & set(present)
    if set(present) <= {output_name, *COMPANIONS}:
        owned |= set(COMPANIONS) & set(present)
    return owned


def attribute(folder: Path, present: list[str], recorded: dict | None,
              output_name: str) -> tuple[list[str], dict]:
    """(names Audiobind can prove it wrote, {name: why it was left}) over ``present`` in ``folder``.

    A recorded name proves nothing on its own (CORE-04d): it is Audiobind's only while the file
    there still has the size and mtime recorded when Audiobind wrote it."""
    own, left = [], {}
    if recorded is None:
        owned = legacy_owned(present, output_name)
    for name in present:
        if recorded is None:
            ours, why = name in owned, UNATTRIBUTED
        elif name not in recorded:
            ours, why = False, NOT_WRITTEN
        else:
            ours, why = file_identity(folder / name) == list(recorded[name]), CHANGED
        if ours:
            own.append(name)
        else:
            left[name] = why
    return own, left


def _owned_before(row) -> tuple[Path, dict[str, list[int]]] | None:
    """(folder, {name: identity}) Audiobind provably owns in this row's output folder right now,
    before a publish (CORE-04d: only names whose file is still the one it wrote)."""
    if row is None or not wrote_output(row):
        return None
    folder = Path(row.output).parent
    recorded = wrote_files(row)
    if recorded is None:
        # unknown provenance (CORE-04c): only what legacy_owned can prove
        output_name = Path(row.output).name
        try:
            in_root = folder.resolve() == Path(CONFIG.output_root).resolve()
        except (OSError, RuntimeError):
            in_root = True
        present = [output_name] if in_root else folder_files(folder)
        names = legacy_owned(present, output_name)
    else:
        names = attribute(folder, [n for n in recorded if os.path.lexists(folder / n)], recorded, "")[0]
    owned = {name: file_identity(folder / name) for name in names}
    return folder, {name: ident for name, ident in owned.items() if ident is not None}


def _merged_wrote(owned: tuple[Path, dict[str, list[int]]] | None, folder: Path,
                  audit: Audit) -> dict[str, list[int]]:
    """What the row records as written after a publish into ``folder`` - {name: [size, mtime_ns]}
    as each file is now (CORE-04d): what it placed, plus - in the same folder - what Audiobind
    owned before and the backups of those files, each only while it is still that same file."""
    ids = {}
    for name in audit.placed:
        ident = file_identity(folder / name)
        if ident is not None:
            ids[name] = ident
    if owned is not None:
        try:
            same = owned[0].resolve() == folder.resolve()
        except (OSError, RuntimeError):
            same = False
        if same:
            for name, before in owned[1].items():
                if name not in ids and file_identity(folder / name) == before:
                    ids[name] = before
            for name, backup in audit.moved_aside.items():
                # a backup is a rename (or a copy2) of the file it replaced: the same size and mtime
                if name in owned[1] and file_identity(folder / backup) == owned[1][name]:
                    ids[backup] = owned[1][name]
    return ids


def _recorded_series(state: State, audit: Audit) -> dict:
    """Q-B12: detail.match's series, sequence and series_source - the catalogue side only.

    Audnex ``seriesPrimary`` is what the tag writer uses, so it is recorded whenever the record is
    at hand without a request: the one convert_unit fetched, else the catalog cache, else what an
    earlier record of this same ASIN carried (so a re-check never flips the shelf back). Only when
    none of those has a series is the Audible search value kept, marked ``"catalog"``, with
    ``audnex_checked`` saying whether Audnex was asked and lists none. The overrides are not baked
    in here: they can change after this record, and overrides.series_of applies them on read.
    """
    product = audit.match.product
    asin = audit.match.asin
    found = audit.audnex if audit.audnex is not None else catalog.cached_book(asin)
    primary = (found or {}).get("seriesPrimary") or {}
    if isinstance(primary, dict) and str(primary.get("name") or "").strip():
        return {
            "series": str(primary["name"]).strip(),
            "sequence": str(primary.get("position") or "").strip(),
            "series_source": overrides_mod.SERIES_AUDNEX,
        }
    if found is None and asin:
        prev = state.get(audit.unit.state_key)
        try:
            before = (json.loads(prev.detail or "{}").get("match") or {}) if prev is not None else {}
        except (ValueError, TypeError, AttributeError):
            before = {}
        if (
            isinstance(before, dict)
            and before.get("asin") == asin
            and before.get("series_source") == overrides_mod.SERIES_AUDNEX
            and before.get("series")
        ):
            return {
                "series": str(before["series"]),
                "sequence": str(before.get("sequence") or ""),
                "series_source": overrides_mod.SERIES_AUDNEX,
            }
    return {
        "series": product.series if product else "",
        "sequence": product.sequence if product else "",
        "series_source": overrides_mod.SERIES_CATALOG,
        # True: Audnex was asked and lists no series, so the file carries none unless overridden
        "audnex_checked": bool(isinstance(found, dict) and found.get("asin")),
    }


def _recorded_hint_conflict(audit: Audit) -> dict:
    """Keep the catalog evidence already fetched for the hint, without another request."""
    asin = audit.unit.asin_hint
    pick = audit.match.product
    if not asin or not pick or asin == pick.asin:
        return {}
    book = catalog.cached_book(asin)
    if not isinstance(book, dict) or book.get("asin") != asin:
        return {}
    title = book.get("title")
    authors = book.get("authors")
    if not isinstance(title, str) or not title.strip() or not isinstance(authors, list) or not authors:
        return {}
    lead = authors[0].get("name") if isinstance(authors[0], dict) else None
    if not isinstance(lead, str) or not lead.strip() or not pick.authors or not pick.authors[0].strip():
        return {}
    hint = catalog.Product(asin=asin, title=title, authors=[lead])
    return {} if matching._same_work(hint, pick) else {"asin": asin, "title": title}


def record(
    state: State,
    audit: Audit,
    status: str,
    output: str = "",
    note: str = "",
    result=None,
    reopen: bool = False,
    unmeasured: bool = False,
) -> None:
    """Record one audit outcome. ``reopen`` marks a manual re-check that still ended
    in a decision-needed state, so the web panel re-opens with the reason. ``unmeasured`` marks
    a book held by the B2 refusal (C3)."""
    custom = overrides_mod.normalise(audit.overrides)
    detail_data = (
        {
            # a manual re-check (Retry now / Use this ASIN) that still needs a decision
            # tells the status page to re-open this book's panel with the reason
            "reopen": bool(reopen),
            "action": audit.action,
            "hours": audit.hours,
            "match": {
                "asin": audit.match.asin,
                "title": audit.match.product.title if audit.match.product else "",
                "authors": audit.match.product.authors if audit.match.product else [],
                "narrators": audit.match.product.narrators if audit.match.product else [],
                "hours": audit.match.product.hours if audit.match.product else 0,
                "confidence": audit.confidence,
                # kept so the status page can show cover art without a second catalog call
                "image": audit.match.product.image if audit.match.product else "",
                # a user-pinned ASIN that had no catalog data; nothing was matched
                "asin_pin_failed": audit.match.asin_pin_failed,
                "asin_hint_conflict": _recorded_hint_conflict(audit),
                # that pin, once it was dead too many checks in a row and stopped blocking a search
                "asin_pin_abandoned": audit.match.pin_abandoned,
                # the evidence behind the pick, shown in the "why this match" panel
                "score": audit.match.score,
                "margin": audit.match.margin,
                "title_sim": audit.match.title_sim,
                "author_sim": audit.match.author_sim,
                "author_known": audit.match.author_known,
                "pinned": audit.match.asin_override,
                "language": audit.match.product.language if audit.match.product else "",
                "language_penalized": audit.match.language_penalized,
                "runtime_delta_pct": audit.match.runtime_delta_pct,
                # the pick is a summary / review / zero-runtime stub (graded LOW), and whether
                # Audnex backed it when another candidate competed for the margin
                "knockoff": audit.match.knockoff,
                "corroborated": audit.match.corroborated,
                # "a Dramatized Adaptation" etc. when the pick is an adaptation the folder never
                # asked for (capped at MEDIUM); "" otherwise
                "unasked_adaptation": audit.match.unasked_adaptation,
                # the adaptation the folder asked for when the pick is a plain edition (capped at
                # MEDIUM too); "" otherwise
                "missing_adaptation": audit.match.missing_adaptation,
                "edition_conflict": audit.match.edition_conflict,
                # what the pick contradicts in the folder's own words (another series volume);
                # graded LOW. "" otherwise
                "veto": audit.match.veto,
                "query_title": audit.match.query_title,
                "query_author": audit.match.query_author,
                # "folder" or "embedded": which names the search above was run with
                "name_source": audit.name_source,
                # Q-B12: the series the file is tagged with (Audnex seriesPrimary), or the Audible
                # search value marked as a fallback - read through overrides.series_of by every view
                **_recorded_series(state, audit),
            },
            "overrides": overrides_mod.normalise(audit.overrides),
            "source": {
                "parts": len(audit.unit.parts),
                "files": audit.unit.n_files,
                "mb": audit.src_mb,
                "hours": audit.hours,
                "codecs": sorted(audit.codecs),
                "channels": sorted(audit.channels),
            },
            "output": (
                {
                    "path": str(result.path),
                    "mb": round(result.size / 1e6, 1),
                    "hours": round(result.duration / 3600, 2),
                    "chapters": result.chapters,
                    "chapters_from": result.chapters_source,
                }
                if result
                else None
            ),
        }
    )
    if result and audit.wrote is not None:
        # CORE-04a: exactly which files in the folder Audiobind wrote - Delete removes only these,
        # and (CORE-04d) only while each still has the size and mtime recorded here
        detail_data["output"]["wrote"] = sorted(audit.wrote)
        detail_data["output"]["wrote_id"] = {name: audit.wrote[name] for name in sorted(audit.wrote)}
    if unmeasured:
        # C3: held because B2 refused to verify an unmeasurable book - the next ordinary scan checks
        # it again (prefetch.already_decided), so a replaced file is picked up on its own
        detail_data["unmeasured"] = True
    if audit.hardcover is not None:
        # only while Hardcover evidence is on: with it off the stored detail is byte-identical
        detail_data["hardcover"] = audit.hardcover
    detail = json.dumps(detail_data)
    state.save(
        UnitState(
            # the state key: rel_path for a library book, "ingest:<rel_path>" for an ingest one
            path=audit.unit.state_key,
            status=status,
            confidence=audit.confidence,
            asin=audit.match.asin,
            title=(
                custom.get("title")
                or (audit.match.product.title if audit.match.product else audit.unit.title_guess)
            ),
            author=(
                custom["authors"][0]
                if custom.get("authors")
                else (
                    audit.match.product.authors[0]
                    if audit.match.product and audit.match.product.authors
                    else audit.unit.author_guess
                )
            ),
            hours=audit.hours,
            files=audit.unit.n_files,
            output=output,
            note=note or "; ".join(audit.notes),
            src_mb=audit.src_mb,
            out_mb=round(result.size / 1e6, 1) if result else 0.0,
            chapters=result.chapters if result else 0,
            # runners-up from the catalog search (other editions, near misses) are kept
            # so the detail panel can offer one-click "use this edition" ASIN pins
            alternates=json.dumps(audit.match.alternates),
            detail=detail,
        )
    )


def kept_conversion(state: State, unit: BookUnit) -> str:
    """The output recorded on this book's row when that file is still on disk, else "".

    A re-check (Re-check all, Retry, a forced scan) only knows where the CURRENT layout files a
    book; a book published under an older layout would look missing there and be encoded again.
    Any status counts, not only ``converted``: a ``ready`` row records a file that was already
    published, and a converted book re-checked into review or held keeps its output path.
    """
    prev = state.get(unit.state_key)
    if prev is None or not prev.output:
        return ""
    try:
        return prev.output if Path(prev.output).is_file() else ""
    except OSError:
        return ""


#: C3: the line a book held by the B2 refusal leads its note with
UNMEASURED_HELD = "Not converted: one file could not be measured, so the converted book could not be checked."


def unmeasured_held_note(reason: str) -> str:
    """C3: the held note for a book B2 refused - this line, then the refusal's own reason."""
    return f"{UNMEASURED_HELD} {reason}"[:300]


def process_unit(
    state: State,
    unit: BookUnit,
    log=print,
    force: bool = False,
    manual: bool = False,
    reencode: bool = False,
) -> Audit:
    """Audit, then convert when the confidence tier and runtime check allow it.

    ``force`` is the operator saying "I looked at this, convert it anyway": the tier, the runtime
    hold and the damaged-source hold are bypassed, but the output is still verified before it is
    published. ``manual`` marks a one-off re-check triggered from the web UI (Retry now /
    Use this ASIN): if it still ends in a decision-needed state, the detail is flagged so the
    panel re-opens with the reason instead of the change going unnoticed.

    ``reencode`` is only ever set by the manual Convert anyway: it lets a book that is already
    converted (its previous output still on disk) be encoded again. Nothing else sets it - not a
    forced scan (Re-check all), not Retry - so those leave a converted book where it is.

    A book removed from Audiobind (a dismissed ingest key) is never audited, recorded or
    converted: checked on entry, again before converting and again before every record.
    """

    def dismissed() -> bool:
        return state.is_dismissed(unit.state_key)

    def rec(status: str, **kw) -> None:
        if dismissed():
            # deleted while this re-check ran: writing the row would bring the book back
            log("  removed from Audiobind while being checked - nothing recorded")
            return
        if not kw.get("output"):
            # a re-check that ends without converting must not forget a published file: the row
            # would disagree with the disk, and the next re-check would encode a duplicate
            kept_output = kept_conversion(state, unit)
            if kept_output:
                kw["output"] = kept_output
        record(state, audit, status, reopen=manual and status in ("review", "held", "failed"), **kw)

    def convert() -> None:
        """The conversion shared by the forced and the automatic branch."""
        if dismissed():
            log("  removed from Audiobind while being checked - not converted")
            return
        kept = "" if reencode else kept_conversion(state, unit)
        if kept and not missing_beside(Path(kept)):
            # no re-encode, no duplicate, no orphaned folder (CORE-04: only when every requested
            # format is there - a missing one is made below, beside the kept file)
            prev = state.get(unit.state_key)
            if prev is not None and prev.status == "converted":
                # the row is left exactly as it was
                log(f"  already converted at {kept} - run Match Audiobookshelf layout to move it")
            else:
                # a ready row, or a converted book an earlier re-check put in review/held: it
                # would convert now, and its file is already published - say so, as
                # nothing_written does when the file sits at the current layout's path
                log(f"  already published at {kept} - not encoded again (Convert anyway re-encodes)")
                rec("ready", output=kept, note="already published - the output file exists")
            return
        try:
            owned = _owned_before(state.get(unit.state_key))  # read before the publish changes the folder
            result = convert_unit(audit, log=log, cancelled=dismissed, heartbeat=state.heartbeat,
                                  reencode=reencode, at=Path(kept) if kept else None)
            if result is None:  # dry run, already published, or removed while it waited
                nothing_written()
            else:
                audit.wrote = _merged_wrote(owned, Path(result.path).parent, audit)
                rec("converted", output=str(result.path), result=result)
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            rec("interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except encode.Unmeasured as exc:
            # C3: Audiobind declined to verify this book (B2) - nothing broke, so it is held, not
            # failed, and the next ordinary scan checks it again
            log(f"  held: {exc}")
            rec("held", note=unmeasured_held_note(str(exc)), unmeasured=True)
        except Exception as exc:  # noqa: BLE001 - one book must not stop a scan
            log(f"  FAILED: {exc}")
            # B4: the row keeps the short note; the log keeps where it happened (never the DB)
            log(traceback.format_exc().rstrip())
            rec("failed", note=str(exc)[:300])

    def nothing_written() -> None:
        """convert_unit returned None: the book was already filed, or this is a dry run.

        Shared by the forced and the automatic branch, so a Convert anyway in dry run
        says "dry run" rather than claiming the output already exists.
        """
        if audit.already_published:
            prev = state.get(unit.state_key)
            if prev and prev.output:
                # a re-check of an already-converted book must not downgrade it
                log("  already published - row unchanged")
            else:
                # the output path is kept so the next scan can skip this book while it exists
                rec("ready", output=audit.out_path, note="already published - the output file exists")
        else:
            rec("ready", note="dry run - nothing written")

    if dismissed():
        log("  removed from Audiobind - skipped")
        return Audit(unit=unit, action="dismissed")
    started = time.time()
    unsafe = unsafe_path_reason(unit)
    if unsafe:
        # held before ffprobe/ffmpeg ever see the names - not even Convert anyway passes this
        audit = Audit(unit=unit, action="hold")
        log(f"  held: {unsafe}")
        rec("held", note=unsafe)
        state.heartbeat()
        return audit
    asin_override = state.get_asin_override(unit.state_key)
    audit = audit_unit(
        unit, asin_override, pin_failures=state.get_pin_failures(unit.state_key) if asin_override else 0
    )
    if asin_override:
        # counted before anything is recorded, so a crash later in this check still counts it
        if audit.match.asin_pin_failed or audit.match.pin_abandoned:
            state.record_pin_failure(unit.state_key)
        elif audit.match.asin_override:
            state.reset_pin_failures(unit.state_key)  # live again: "in a row" starts over
    audit.overrides = state.get_overrides(unit.state_key)
    if audit.overrides:
        audit.notes.append("operator metadata overrides applied")
    log(
        f"  {audit.confidence:6} {audit.hours:>6.2f}h {unit.n_files:>3}f  "
        f"{audit.match.product.title if audit.match.product else '-'}"
    )

    if not force and audit.unreadable and audit.action in ("merge+convert", "convert", "remux"):
        log(f"  skipping: {audit.unreadable} unreadable source file(s) - fix the source first")
        rec("review", note=f"{audit.unreadable} unreadable source file(s); not converted")
    elif force and audit.match.product:
        log("  forced by operator - converting regardless of tier/hold")
        convert()
    elif (audit.action in ("merge+convert", "convert", "remux") and audit.confidence in effective_auto_tiers()
          and not audit.match.pin_abandoned):
        # a match made after giving up on a dead pin is never converted unseen: the operator
        # asked for one edition, and this search may have found another
        convert()
    elif audit.confidence == "NONE" and audit.match.rejected:
        log(f"  held: {audit.match.rejected}")
        rec("held", note=audit.match.rejected)
    elif audit.action == "hold":
        log("  held: runtime disagrees with the catalog (wrong edition?)")
        rec("held")
    else:
        rec("review")
    log(f"  done in {time.time() - started:.0f}s")
    state.heartbeat()  # keep "last scan" honest while a long pass is still running
    return audit

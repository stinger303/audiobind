"""One unit in, one finished book out: audit and convert."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import shutil
import stat
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, encode, hardcover, ingest, library, matching
from . import overrides as overrides_mod
from .config import CONFIG, effective_auto_tiers, effective_val
from .control import CONTROL, Stopped
from .probe import ProbeTimeout, probe
from .state import SOURCE_INGEST, WRITER_BUSY, State, UnitState, WriterBusy
from .units import AUDIO_EXT, BookUnit, find_unit, looks_like_collection, owned_paths, unsafe_path_reason
from .util import loads_dict, norm

#: the daemon and the manual "Retry / Convert anyway" buttons must not encode two books at once
CONVERT_LOCK = threading.Lock()

#: Audit.name_source values, recorded in the detail's match block
NAME_SOURCE_FOLDER = "folder"
NAME_SOURCE_EMBEDDED = "embedded"


class SourceContainmentError(ValueError):
    """A source path points outside the configured library."""


def source_target(root: Path, target: Path) -> Path:
    """Resolve parent components inside the root and preserve the leaf itself."""
    if root not in target.parents or ".." in target.relative_to(root).parts:
        raise SourceContainmentError(f"{target} escapes library root")
    parent = root
    for name in target.parent.relative_to(root).parts:
        parent = (parent / name).resolve()
        if not parent.is_relative_to(root):
            raise SourceContainmentError(f"{target} escapes library root")
    return parent / target.name


def find_source_alias(key: str) -> BookUnit | None:
    """Re-check an existing library row whose parent became an internal symlink."""
    configured = Path(CONFIG.library_root).absolute()
    root = configured.resolve()
    name, marker, part_key = key.partition("#")
    raw = root / name
    try:
        actual = source_target(root, raw)
    except (OSError, ValueError, RuntimeError):
        return None
    if actual == raw or actual.is_symlink():
        return None
    actual_key = actual.relative_to(root).as_posix() + marker + part_key
    unit = find_unit(root, CONFIG.ignore_globs, actual_key)
    if unit is None:
        return None

    def alias(path: str) -> str:
        relative = Path(path).relative_to(actual.parent)
        return str(configured / raw.parent.relative_to(root) / relative)

    parts = [dataclasses.replace(part, files=[alias(path) for path in part.files]) for part in unit.parts]
    return dataclasses.replace(unit, path=alias(unit.path), parent=alias(unit.parent),
                               parts=parts, rel_path=key)


def source_entry_identity(info: os.stat_result) -> list | dict:
    if stat.S_ISREG(info.st_mode):
        return [info.st_size, info.st_mtime_ns]
    kind = "link" if stat.S_ISLNK(info.st_mode) else "irregular"
    return {"kind": kind, "id": [info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]}


def source_entries(root: Path, targets: list[Path]) -> dict:
    """Inventory delete targets without following symlinks or accepting a partial walk."""
    entries = {}

    def visit(path):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            entries[name] = None
            for child in path.iterdir():
                visit(child)
        else:
            entries[name] = source_entry_identity(info)

    for target in targets:
        visit(source_target(root, target))
    return entries


def source_ownership(unit: BookUnit) -> dict:
    if unit.source == SOURCE_INGEST:
        return {}
    try:
        configured_root = Path(CONFIG.library_root).absolute()
        root = configured_root.resolve()

        def relative(path):
            return Path(path).absolute().relative_to(configured_root)

        targets = [root / relative(p) for p in owned_paths(unit)]
        if root in targets:
            targets = [root / relative(unit.parent) / part.name for part in unit.parts]
        entries = source_entries(root, targets)
        audio = {name for name, identity in entries.items()
                 if isinstance(identity, list) and Path(name).suffix.lower() in AUDIO_EXT}
        unit_audio = {source_target(root, root / relative(f)).relative_to(root).as_posix()
                      for f in unit.files}
        if audio != unit_audio:
            return {"blocked": "the source folder also holds audio belonging to other books"}
        if audio:
            return {"targets": [p.relative_to(root).as_posix() for p in targets], "entries": entries}
    except (OSError, ValueError, RuntimeError):
        pass
    return {}


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
    output_collision: str = ""
    #: which names the catalog match was searched with: "folder" (the folder/file name, as
    #: always) or "embedded" (an ingest unit's own audio tags, after the folder found nothing)
    name_source: str = NAME_SOURCE_FOLDER
    #: Hardcover's second opinion (hardcover.evidence_for) for the detail panel; None while it is off.
    #: Never read by the grade or the action
    hardcover: dict | None = None
    #: CORE-04d: names and identities kept as detail.output.wrote / wrote_id.
    wrote: dict | None = None
    source_ownership: dict = field(default_factory=dict)
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
    audit = Audit(unit=unit, source_ownership=source_ownership(unit))
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
    if audit.match.product and audit.match.hint_conflict:
        audit.notes.append(f"{audit.match.hint_conflict} - graded LOW, not converted automatically")
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
    if audit.source_ownership and audit.source_ownership != source_ownership(unit):
        audit.source_ownership = {}
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


def attempt_work_dir() -> Path:
    """A private scratch path for one attempt, independent of the unit's previous runs."""
    return CONFIG.work_dir / f"attempt-{uuid.uuid4().hex}"


def missing_beside(primary: Path) -> list[str]:
    """The requested formats with no file beside ``primary`` (same folder and name, its own extension)."""
    formats = encode.formats_for(effective_val("output_format"))
    return [fmt for fmt in formats if not primary.with_suffix(f".{fmt}").exists()]


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
                 at: Path | None = None, *, state: State):
    """Encode, verify and file one book. Returns the EncodeResult (or None in a dry run).

    "Already published" means every requested format is on disk and verified for this edition.
    Verified formats can be kept while missing formats are added. ``at`` anchors the requested
    formats beside a previously published file, including under an older layout.

    ``cancelled`` (optional, no arguments) is asked again once the encoder is free: True means
    the book was removed from Audiobind while it waited, and nothing is encoded. ``heartbeat``
    (optional) is called, throttled, from the encode's progress so one long encode never makes
    the healthcheck read the daemon as stalled. ``reencode`` (the manual Convert anyway) encodes
    even when the output already exists; the old files are renamed aside to ``.bak-<timestamp>``
    and the verified result takes their place through the publication journal.
    """
    with state.unit_writer(audit.unit.state_key):
        return _convert_unit(audit, log, cancelled, heartbeat, reencode, at, state)


def _convert_unit(audit, log, cancelled, heartbeat, reencode, at, state):
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
    with state.output_writer(out_path.parent), CONVERT_LOCK:
        with state.publication_lock():
            _recover_publications(state)
            existing = {str(path): _publication_identity(path) for path in paths.values()}
            audit.output_collision = _output_collision(state, audit, paths.values(), existing)
        if audit.output_collision:
            return None
        if CONTROL.deleting(unit.state_key):
            return None
        todo = formats if reencode else [fmt for fmt in formats if existing[str(paths[fmt])] is None]
        if not todo:
            audit.already_published = True
            log(f"  already published: {out_path}")
            return None
        if CONTROL.dry_run:
            for fmt in todo:
                log(f"  [dry-run] would write {paths[fmt]}")
            return None
        work_dir = attempt_work_dir()
        if todo != formats:
            log(f"  {', '.join(f for f in formats if f not in todo).upper()} already published; "
                f"adding {', '.join(todo).upper()}")
        if cancelled is not None and cancelled():
            log("  removed from Audiobind while waiting for the encoder - not converted")
            return None
        work_dir.mkdir(parents=True)
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
            if cancelled is not None and cancelled():
                return None
            with state.publication_lock():
                _recover_publications(state)
                return _publish_recorded(state, audit, todo, results, paths, book,
                                         plan.chapters, duration, log)
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
    while os.path.lexists(candidate):
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


class PublicationRecoveryError(RuntimeError):
    """The pending attempt must be reconciled before a scan can continue."""


def _publication_identity(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [stat.S_IFMT(info.st_mode), info.st_ino, info.st_size, info.st_mtime_ns]


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _settle_publication(state: State, attempt: int, plan: dict, outcome: UnitState | None) -> bool:
    before, expected, backups = plan["before"], plan["expected"], plan["backups"]
    stage = Path(plan["stage"])
    _refuse_uncontained([stage, *(Path(p) for p in before), *(Path(p) for p in backups.values())])
    complete = outcome is not None and all(
        _publication_identity(Path(path)) == identity for path, identity in expected.items()
    )
    if outcome is not None and not complete:
        for name in plan["writes"]:
            path = Path(name)
            backup = Path(backups[name]) if name in backups else None
            current = _publication_identity(path)
            if backup is not None and _publication_identity(backup) == before[name]:
                if current is not None and current not in (before[name], expected[name]):
                    # Preserve an external replacement before restoring the previous book.
                    os.rename(path, _backup_path(path, f"conflict-{attempt}"))
                os.replace(backup, path)
            elif before[name] is None and current == expected[name]:
                path.unlink()
            elif before[name] is not None and current != before[name]:
                raise PublicationRecoveryError(f"Cannot restore {path}: its publication backup is missing")
        previous = state.get(outcome.path)
        if (previous and previous.status == "converted" and previous.output in before
                and _publication_identity(Path(previous.output)) != before[previous.output]):
            outcome = dataclasses.replace(previous, status="review", output="",
                                          note="Previous output changed during interrupted publication")
        else:
            outcome = None
    if stage.exists():
        shutil.rmtree(stage)
    if stage.parent.is_dir():
        _sync_directory(stage.parent)
    if not complete:
        for name in plan["new_dirs"]:
            directory = Path(name)
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno != errno.ENOTEMPTY:
                    raise
                break
            _sync_directory(directory.parent)
    state.finish_publication(attempt, outcome)
    return complete


def _recover_publications(state: State, current: int | None = None) -> bool:
    completed = False
    try:
        for attempt, plan, outcome in state.pending_publications():
            if _settle_publication(state, attempt, plan, outcome) and attempt == current:
                completed = True
    except Exception as exc:
        raise PublicationRecoveryError(f"Cannot reconcile pending publication: {exc}") from exc
    return completed


def recover_publications(state: State) -> None:
    with state.publication_lock():
        _recover_publications(state)


def _publish_recorded(state: State, audit: Audit, formats: list[str], results: dict, paths: dict,
                      book: dict, chapters: list[dict], duration: float, log):
    primary = next(iter(paths.values()))
    folder = primary.parent.resolve()
    destinations = {fmt: folder / path.name for fmt, path in paths.items()}
    stage = folder / f".audiobind-publish-{uuid.uuid4().hex}"
    cover, meta = folder / "cover.jpg", folder / "metadata.json"
    writes = [destinations[fmt] for fmt in formats]
    if any(result.cover is not None for result in results.values()):
        writes.append(cover)
    if not meta.is_symlink():
        writes.append(meta)
    else:
        log(f"  metadata.json skipped: {meta} is a symlink")
    _refuse_uncontained([*destinations.values(), cover, meta])
    before = {str(path): _publication_identity(path) for path in {*destinations.values(), cover, meta}}
    # Recheck after encoding, before creating an intent or touching a destination. Include
    # companions only when this publication would write them.
    audit.output_collision = _output_collision(state, audit, [*destinations.values(), *writes], before)
    if audit.output_collision:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backups = {str(path): str(_backup_path(path, stamp)) for path in writes if before[str(path)] is not None}
    new_dirs = []
    parent = folder
    root = Path(CONFIG.output_root).resolve()
    while not parent.exists():
        new_dirs.append(str(parent))
        parent = parent.parent
    plan = {"stage": str(stage), "before": before, "backups": backups,
            "writes": [str(path) for path in writes], "expected": {},
            "new_dirs": [name for name in new_dirs if root in Path(name).parents]}
    owned = _owned_before(state.get(audit.unit.state_key), audit.match.asin)
    attempt = state.begin_publication(audit.unit.state_key, plan)
    try:
        stage.mkdir(parents=True)
        for name in reversed(new_dirs):
            _sync_directory(Path(name).parent)
        for fmt in formats:
            encode.publish_result(results[fmt], stage / destinations[fmt].name)
        if meta in writes:
            _write_sidecar(stage / primary.name, book, audit.match, chapters, duration, log=log)
            if before[str(meta)] is not None:
                (stage / meta.name).chmod(stat.S_IMODE(meta.stat().st_mode))
        expected = before.copy()
        for path in writes:
            staged = stage / path.name
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            expected[str(path)] = _publication_identity(staged)
        _sync_directory(stage)
        _sync_directory(folder)
        for name, identity in before.items():
            if _publication_identity(Path(name)) != identity:
                audit.output_collision = (
                    f"Output collision: {Path(name).name} changed while preparing this book."
                )
                _recover_publications(state)
                return None

        audit.wrote = {path.name: expected[str(path)][-2:] for path in writes}
        if owned is not None and owned[0].resolve() == folder:
            for name, identity in owned[1].items():
                audit.wrote.setdefault(name, identity)
                backup = backups.get(str(folder / name))
                if backup is not None:
                    audit.wrote[Path(backup).name] = identity
        first = next(iter(paths))
        result = dataclasses.replace(results.get(first, results[formats[0]]), path=primary,
                                     size=expected[str(destinations[first])][-2], cover=None)
        outcome = _record_row(state, audit, "converted", output=str(primary), result=result)
        plan["expected"] = expected
        state.seal_publication(attempt, plan, outcome)
        for name, backup in backups.items():
            if _publication_identity(Path(name)) != before[name] or os.path.lexists(backup):
                audit.output_collision = f"Output collision: {Path(name).name} changed during publication."
                _recover_publications(state)
                return None
            os.rename(name, backup)
        _sync_directory(folder)
        for path in writes:
            if os.path.lexists(path):
                audit.output_collision = f"Output collision: {path.name} appeared during publication."
                _recover_publications(state)
                return None
            encode._place(stage / path.name, path, move=True)
        _sync_directory(stage)
        _sync_directory(folder)
    except PublicationRecoveryError:
        raise
    except Exception:
        if not _recover_publications(state, current=attempt):
            raise
    else:
        if not _recover_publications(state, current=attempt):
            raise OSError(f"Output changed during publication in {folder}")
    for path in writes:
        log(f"  published {path}")
    for name, backup in backups.items():
        log(f"  replaced {Path(name).name}; the previous file is kept at {backup}")
    return result


def recorded_output(row) -> dict:
    """The ``output`` block of a row's detail JSON ({} when there is none)."""
    try:
        detail = json.loads(row.detail or "{}")
    except (TypeError, ValueError):
        return {}
    output = detail.get("output") if isinstance(detail, dict) else None
    return output if isinstance(output, dict) else {}


def _published_edition(row: UnitState) -> tuple[dict, str]:
    """Read the edition captured by publication, including identity-bearing older rows."""
    output = recorded_output(row)
    path = output.get("path")
    if not row.output or not isinstance(path, str) or Path(path).absolute() != Path(row.output).absolute():
        return {}, ""
    if "asin" in output:
        return output, output["asin"] if isinstance(output["asin"], str) else ""
    match = loads_dict(row.detail).get("match")
    if row.status == "converted" and isinstance(match, dict) and match.get("asin") == row.asin:
        return output, row.asin
    return {}, ""


def _output_collision(state: State, audit: Audit, paths, snapshot: dict) -> str:
    row = state.get(audit.unit.state_key)
    output, asin = _published_edition(row) if row else ({}, "")
    ids = _manifest_files(output) or {}
    for path in dict.fromkeys(paths):
        identity = snapshot[str(path)]
        if identity is None:
            continue
        if (asin and asin == audit.match.asin
                and Path(output["path"]).parent.resolve() == path.parent.resolve()
                and identity[0] == stat.S_IFREG and ids.get(path.name) == identity[-2:]):
            continue
        return (f"Output collision: {path.name} already exists, but Audiobind cannot verify "
                f"that it belongs to this book and edition ({audit.match.asin}). "
                "Review the existing file before trying again.")
    return ""


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
    provenance that requires separate confirmation before Delete can remove those files.
    """
    if not wrote_output(row):
        return {}
    return _manifest_files(recorded_output(row))


def _manifest_files(output: dict) -> dict[str, list[int]] | None:
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
    """Legacy filename candidates for the recorded output and its possible companions."""
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
    for name in present:
        if recorded is None:
            ours, why = False, UNATTRIBUTED
        elif name not in recorded:
            ours, why = False, NOT_WRITTEN
        else:
            ours, why = file_identity(folder / name) == list(recorded[name]), CHANGED
        if ours:
            own.append(name)
        else:
            left[name] = why
    return own, left


def _owned_before(row, asin: str) -> tuple[Path, dict[str, list[int]]] | None:
    """(folder, {name: identity}) Audiobind provably owns in this row's output folder right now,
    before a publish (CORE-04d: only names whose file is still the one it wrote)."""
    if row is None:
        return None
    output, edition = _published_edition(row)
    if not edition or edition != asin:
        return None
    folder = Path(row.output).parent
    recorded = _manifest_files(output)
    if recorded is None:
        return None
    names = attribute(folder, [n for n in recorded if os.path.lexists(folder / n)], recorded, "")[0]
    owned = {name: file_identity(folder / name) for name in names}
    return folder, {name: ident for name, ident in owned.items() if ident is not None}


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
    state.save(_record_row(state, audit, status, output, note, result, reopen, unmeasured))


def _record_row(
    state: State,
    audit: Audit,
    status: str,
    output: str = "",
    note: str = "",
    result=None,
    reopen: bool = False,
    unmeasured: bool = False,
) -> UnitState:
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
                "ownership": audit.source_ownership,
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
    previous = state.get(audit.unit.state_key) if output and not result else None
    if previous and previous.output == output:
        published, edition = _published_edition(previous)
        if edition:
            detail_data["output"] = {**published, "asin": edition}
        elif wrote_output(previous):
            detail_data["output"] = recorded_output(previous)
    if result and audit.wrote is not None:
        # CORE-04a: exactly which files in the folder Audiobind wrote - Delete removes only these,
        # and (CORE-04d) only while each still has the size and mtime recorded here
        detail_data["output"]["asin"] = audit.match.asin
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
    return UnitState(
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
        out_mb=round(result.size / 1e6, 1) if result else (previous.out_mb if previous else 0.0),
        chapters=result.chapters if result else (previous.chapters if previous else 0),
        # runners-up from the catalog search (other editions, near misses) are kept
        # so the detail panel can offer one-click "use this edition" ASIN pins
        alternates=json.dumps(audit.match.alternates),
        detail=detail,
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
    if not CONTROL.begin_audit(unit.state_key, manual=manual):
        log(f"  {WRITER_BUSY} - skipped")
        return Audit(unit=unit, action="busy")
    try:
        with state.unit_writer(unit.state_key):
            return _process_unit(state, unit, log, force, manual, reencode)
    except WriterBusy as exc:
        log(f"  {exc} - skipped")
        return Audit(unit=unit, action="busy")
    finally:
        CONTROL.end_audit(unit.state_key)


def _process_unit(state: State, unit: BookUnit, log, force: bool, manual: bool, reencode: bool) -> Audit:
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
        if kept:
            # Probe the saved layout before entering the conversion failure handler.
            # An I/O error here must leave the previous row untouched.
            missing_beside(Path(kept))
        try:
            result = convert_unit(audit, log=log, cancelled=dismissed, heartbeat=state.heartbeat,
                                  reencode=reencode, at=Path(kept) if kept else None, state=state)
            if result is None:  # dry run, already published, or removed while it waited
                nothing_written()
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            rec("interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except encode.Unmeasured as exc:
            # C3: Audiobind declined to verify this book (B2) - nothing broke, so it is held, not
            # failed, and the next ordinary scan checks it again
            log(f"  held: {exc}")
            rec("held", note=unmeasured_held_note(str(exc)), unmeasured=True)
        except (PublicationRecoveryError, WriterBusy):
            raise
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
        if audit.output_collision:
            log(f"  {audit.output_collision}")
            rec("review", note=audit.output_collision)
        elif audit.already_published:
            prev = state.get(unit.state_key)
            if prev and prev.status == "converted":
                if manual and audit.unit.source != SOURCE_INGEST and audit.source_ownership:
                    rec("converted", output=prev.output, note="re-checked; existing conversion kept")
                else:
                    log(f"  already converted at {prev.output} - run Match Audiobookshelf layout to move it")
            else:
                rec("ready", output=audit.out_path, note="already published - verified for this edition")
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

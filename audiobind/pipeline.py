"""One unit in, one finished book out: audit and convert."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, encode, ingest, library, matching
from . import overrides as overrides_mod
from .config import CONFIG, effective_auto_tiers, effective_val
from .control import CONTROL, Stopped
from .probe import probe
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

    @property
    def runtime_matches(self) -> bool:
        if not self.match.product:
            return True
        return matching.runtime_ok(self.hours, self.match.product.hours, effective_val("max_runtime_delta"))


def audit_unit(unit: BookUnit, asin_override: str = "") -> Audit:
    """Read-only assessment: durations, codecs, catalog match, what we would do."""
    audit = Audit(unit=unit)
    total = 0.0
    for path in unit.files:
        info = probe(path)
        audit.durations[path] = info.duration
        total += info.duration
        if info.codec:
            audit.codecs.add(info.codec)
        if info.channels:
            audit.channels.add(str(info.channels))
        if not info.ok:
            audit.unreadable += 1
    audit.hours = round(total / 3600, 2)
    audit.src_mb = round(sum(os.path.getsize(f) for f in unit.files if os.path.exists(f)) / 1e6, 1)

    audit.match = matching.match_unit(unit, asin_override, local_hours=audit.hours)
    if unit.source == SOURCE_INGEST and not asin_override and audit.match.confidence in ("NONE", "LOW"):
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
    if audit.confidence == "NONE" and audit.match.rejected:
        audit.notes.append(audit.match.rejected)

    avg_hours = audit.hours / len(unit.files) if unit.files else 0
    if looks_like_collection(unit, avg_hours) and audit.confidence in ("LOW", "NONE"):
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

    audit.action = choose_action(unit, audit.confidence, audit.runtime_matches, audit.codecs)
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


def choose_action(unit: BookUnit, confidence: str, runtime_matches: bool, codecs: set[str]) -> str:
    """What to do with this unit. Pure function so the rules can be tested directly.

    A single AAC file that is already an MP4 container is remuxed, not re-encoded: chapters, tags
    and cover are rewritten while every audio sample is copied untouched.
    """
    already_mp4 = (
        len(unit.files) == 1 and Path(unit.files[0]).suffix.lower() in (".m4b", ".m4a") and codecs == {"aac"}
    )
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


def convert_unit(audit: Audit, log=print, cancelled=None, heartbeat=None):
    """Encode, verify and file one book. Returns the EncodeResult (or None in a dry run).

    ``cancelled`` (optional, no arguments) is asked again once the encoder is free: True means
    the book was removed from Audiobind while it waited, and nothing is encoded. ``heartbeat``
    (optional) is called, throttled, from the encode's progress so one long encode never makes
    the healthcheck read the daemon as stalled.
    """
    unit, match = audit.unit, audit.match
    if not match.product:
        raise ValueError("cannot convert without a catalog match")

    book = catalog.book(match.asin)
    # the operator's corrections are written into the catalog payload before anything reads it,
    # so tags, metadata.json and the filing layout all agree with what the panel shows
    book = overrides_mod.apply(book, audit.overrides)
    series = (book.get("seriesPrimary") or {}).get("name", "")
    sequence = str((book.get("seriesPrimary") or {}).get("position", "") or "")
    authors = [a.get("name") for a in (book.get("authors") or []) if a.get("name")] or match.product.authors
    title = book.get("title") or match.product.title

    formats = encode.formats_for(effective_val("output_format"))
    # the first format is the book's main file: the unit's output and the "already published" test
    paths = {fmt: library.m4b_path(series, authors, title, sequence, ext=fmt) for fmt in formats}
    out_path = paths[formats[0]]
    audit.out_path = str(out_path)
    if out_path.exists():
        audit.already_published = True
        log(f"  already published: {out_path}")
        return None
    if CONTROL.dry_run:
        for path in paths.values():
            log(f"  [dry-run] would write {path}")
        return None  # nothing was written, so it is not "converted"

    work_dir = work_dir_for(unit)
    with CONVERT_LOCK:
        # checked again under the lock: a manual action and the daemon can both reach
        # this point for the same book, and only the first may encode it
        if out_path.exists():
            audit.already_published = True
            log(f"  already published (while waiting for the encoder): {out_path}")
            return None
        if cancelled is not None and cancelled():
            log("  removed from Audiobind while waiting for the encoder - not converted")
            return None
        # a crashed or restarted run leaves its work directory behind under this same name:
        # start clean, so none of its partial files can be taken for this encode's output
        shutil.rmtree(work_dir, ignore_errors=True)
        CONTROL.begin_unit(unit.state_key, title)
        CONTROL.set_heartbeat(heartbeat)
        try:
            # one chapter plan (one ffprobe per file) shared by the encoder and metadata.json
            plan = encode.plan_chapters(unit, match.asin, durations=audit.durations or None)
            results = {}
            for fmt in formats:
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
            _publish_all(formats, results, paths)
            result = results[formats[0]]
            encode.write_abs_metadata(
                out_path.parent / "metadata.json", book, match, plan.chapters, result.duration
            )
            for fmt in formats:
                res = results[fmt]
                log(f"  published {paths[fmt]} ({res.size / 1e6:.0f} MB, {res.chapters} chapters)")
            return result
        finally:
            # a stopped encode leaves only the work directory, which is deleted here, so the
            # library never sees a partial file and the book is simply retried next scan
            shutil.rmtree(work_dir, ignore_errors=True)
            CONTROL.set_heartbeat(None)
            CONTROL.end_unit()


def _publish_all(formats: list[str], results: dict, paths: dict) -> None:
    """Publish every verified format, the m4b (archival copy) first - all of them or none.

    If a later format fails to publish (a cross-filesystem copy that runs out of space), the
    files this call already placed are removed again, with a cover.jpg and any folders it
    created, so the library never keeps an m4b without the mp3 the owner asked for - which the
    next scan would otherwise take as "already published" and never finish. Files that were
    there before are never removed.
    """
    folder = paths[formats[0]].parent
    new_dirs = []
    probe_dir = folder
    root = Path(CONFIG.output_root)
    while not probe_dir.exists() and probe_dir not in (root, probe_dir.parent):
        new_dirs.append(probe_dir)  # deepest first
        probe_dir = probe_dir.parent
    cover = folder / "cover.jpg"
    preexisting = {path for path in [*paths.values(), cover] if path.exists()}
    placed = []
    try:
        for fmt in formats:
            encode.publish_result(results[fmt], paths[fmt])
            placed.append(paths[fmt])
    except BaseException:
        for path in [*placed, cover]:
            if path not in preexisting:
                try:
                    path.unlink()
                except OSError:
                    pass
        for directory in new_dirs:
            try:
                directory.rmdir()  # only ever an empty folder this call made
            except OSError:
                break
        raise


def record(
    state: State,
    audit: Audit,
    status: str,
    output: str = "",
    note: str = "",
    result=None,
    reopen: bool = False,
) -> None:
    """Record one audit outcome. ``reopen`` marks a manual re-check that still ended
    in a decision-needed state, so the web panel re-opens with the reason."""
    custom = overrides_mod.normalise(audit.overrides)
    detail = json.dumps(
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
                "query_title": audit.match.query_title,
                "query_author": audit.match.query_author,
                # "folder" or "embedded": which names the search above was run with
                "name_source": audit.name_source,
                # what the catalog says this book's series is, for the series shelf
                "series": audit.match.product.series if audit.match.product else "",
                "sequence": audit.match.product.sequence if audit.match.product else "",
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
        if kept:
            # no re-encode, no duplicate, no orphaned folder
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
            result = convert_unit(audit, log=log, cancelled=dismissed, heartbeat=state.heartbeat)
            if result is None:  # dry run, already published, or removed while it waited
                nothing_written()
            else:
                rec("converted", output=str(result.path), result=result)
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            rec("interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except Exception as exc:  # noqa: BLE001 - one book must not stop a scan
            log(f"  FAILED: {exc}")
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
    audit = audit_unit(unit, state.get_asin_override(unit.state_key))
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
    elif audit.action in ("merge+convert", "convert", "remux") and audit.confidence in effective_auto_tiers():
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

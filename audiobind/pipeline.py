"""One unit in, one finished book out: audit and convert."""

from __future__ import annotations

import dataclasses
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
    if unit.source == SOURCE_INGEST and not asin_override and audit.match.confidence == "NONE":
        embedded = _match_embedded(unit, audit.hours)
        if embedded is not None:
            audit.match = embedded
            audit.name_source = NAME_SOURCE_EMBEDDED
            audit.notes.append("matched on the audio file's embedded tags")
    audit.confidence = audit.match.confidence

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


def _match_embedded(unit: BookUnit, local_hours: float) -> matching.Match | None:
    """INGEST ONLY, after the folder name matched nothing: retry with the files' own tags.

    A torrent name is often junk while a single m4b carries correct title/artist tags. Returns
    the retry's match when it found something (any tier - the usual grading and auto-convert
    rules still apply), else None so the folder's NONE stands. The unit itself is not changed.
    """
    author, title = ingest.embedded_names(unit)
    if not (author or title):
        return None
    retry_unit = dataclasses.replace(
        unit, author_guess=author or unit.author_guess, title_guess=title or unit.title_guess
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


def convert_unit(audit: Audit, log=print):
    """Encode, verify and file one book. Returns the EncodeResult (or None in a dry run)."""
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

    out_path = library.m4b_path(series, authors, title, sequence)
    audit.out_path = str(out_path)
    if out_path.exists():
        audit.already_published = True
        log(f"  already published: {out_path}")
        return None
    if CONTROL.dry_run:
        log(f"  [dry-run] would write {out_path}")
        return None  # nothing was written, so it is not "converted"

    work_dir = CONFIG.work_dir / f"{abs(hash(unit.state_key)) % 10**8:08x}"
    with CONVERT_LOCK:
        # checked again under the lock: a manual action and the daemon can both reach
        # this point for the same book, and only the first may encode it
        if out_path.exists():
            audit.already_published = True
            log(f"  already published (while waiting for the encoder): {out_path}")
            return None
        CONTROL.begin_unit(unit.state_key)
        try:
            # one chapter plan (one ffprobe per file) shared by the encoder and metadata.json
            plan = encode.plan_chapters(unit, match.asin, durations=audit.durations or None)
            result = encode.encode(
                unit, match, book, work_dir, out_path, log=log, copy_audio=(audit.action == "remux"), plan=plan
            )
            encode.write_abs_metadata(
                out_path.parent / "metadata.json", book, match, plan.chapters, result.duration
            )
            log(f"  published {out_path} ({result.size / 1e6:.0f} MB, {result.chapters} chapters)")
            return result
        finally:
            # a stopped encode leaves only the work directory, which is deleted here, so the
            # library never sees a partial file and the book is simply retried next scan
            shutil.rmtree(work_dir, ignore_errors=True)
            CONTROL.end_unit()


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


def process_unit(state: State, unit: BookUnit, log=print, force: bool = False, manual: bool = False) -> Audit:
    """Audit, then convert when the confidence tier and runtime check allow it.

    ``force`` is the operator saying "I looked at this, convert it anyway": the tier, the runtime
    hold and the damaged-source hold are bypassed, but the output is still verified before it is
    published. ``manual`` marks a one-off re-check triggered from the web UI (Retry now /
    Use this ASIN): if it still ends in a decision-needed state, the detail is flagged so the
    panel re-opens with the reason instead of the change going unnoticed.
    """

    def rec(status: str, **kw) -> None:
        record(state, audit, status, reopen=manual and status in ("review", "held", "failed"), **kw)

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
        try:
            result = convert_unit(audit, log=log)
            if result is None:  # dry run, or already published
                nothing_written()
            else:
                rec("converted", output=str(result.path), result=result)
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            rec("interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except Exception as exc:  # noqa: BLE001
            log(f"  FAILED: {exc}")
            rec("failed", note=str(exc)[:300])
    elif audit.action in ("merge+convert", "convert", "remux") and audit.confidence in effective_auto_tiers():
        try:
            result = convert_unit(audit, log=log)
            if result is None:  # dry run, or already published
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
    elif audit.action == "hold":
        log("  held: runtime disagrees with the catalog (wrong edition?)")
        rec("held")
    else:
        rec("review")
    log(f"  done in {time.time() - started:.0f}s")
    state.heartbeat()  # keep "last scan" honest while a long pass is still running
    return audit

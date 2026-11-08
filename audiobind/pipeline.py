"""One unit in, one finished book out: audit and convert."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, encode, library, matching
from . import overrides as overrides_mod
from .config import CONFIG
from .control import CONTROL, Stopped
from .probe import probe
from .state import State, UnitState
from .units import BookUnit, looks_like_collection

#: the daemon and the manual "Retry / Convert anyway" buttons must not encode two books at once
CONVERT_LOCK = threading.Lock()


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

    @property
    def runtime_matches(self) -> bool:
        if not self.match.product:
            return True
        return matching.runtime_ok(self.hours, self.match.product.hours, CONFIG.max_runtime_delta)


def audit_unit(unit: BookUnit, asin_override: str = "") -> Audit:
    """Read-only assessment: durations, codecs, catalog match, what we would do."""
    audit = Audit(unit=unit)
    total = 0.0
    for path in unit.files:
        info = probe(path)
        total += info.duration
        if info.codec:
            audit.codecs.add(info.codec)
        if info.channels:
            audit.channels.add(str(info.channels))
        if not info.ok:
            audit.unreadable += 1
    audit.hours = round(total / 3600, 2)
    audit.src_mb = round(sum(os.path.getsize(f) for f in unit.files if os.path.exists(f)) / 1e6, 1)

    audit.match = matching.match_unit(unit, asin_override)
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
    if not unit.author_guess and audit.match.product:
        audit.notes.append("author taken from catalog")
    if audit.match.product and not audit.runtime_matches:
        audit.notes.append(
            f"runtime {audit.hours}h vs catalog {audit.match.product.hours}h")
    elif unit.multi_part and audit.match.product:
        audit.notes.append("parts verified complete")

    audit.action = choose_action(unit, audit.confidence, audit.runtime_matches, audit.codecs)
    return audit


def choose_action(unit: BookUnit, confidence: str, runtime_matches: bool,
                  codecs: set[str]) -> str:
    """What to do with this unit. Pure function so the rules can be tested directly.

    A single AAC file that is already an MP4 container is remuxed, not re-encoded: chapters, tags
    and cover are rewritten while every audio sample is copied untouched.
    """
    already_mp4 = (len(unit.files) == 1
                   and Path(unit.files[0]).suffix.lower() in (".m4b", ".m4a")
                   and codecs == {"aac"})
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
    if out_path.exists():
        log(f"  already published: {out_path}")
        return None
    if CONTROL.dry_run:
        log(f"  [dry-run] would write {out_path}")
        return None                     # nothing was written, so it is not "converted"

    work_dir = CONFIG.work_dir / f"{abs(hash(unit.rel_path)) % 10**8:08x}"
    CONVERT_LOCK.acquire()
    CONTROL.begin_unit(unit.rel_path)
    try:
        duration = sum(probe(f).duration for f in unit.files)
        chapters, _source = encode.build_chapters(catalog.chapters(match.asin), duration, unit)
        result = encode.encode(unit, match, book, work_dir, out_path, log=log,
                               copy_audio=(audit.action == "remux"))
        encode.write_abs_metadata(out_path.parent / "metadata.json", book, match, chapters,
                                  result.duration)
        log(f"  published {out_path} ({result.size / 1e6:.0f} MB, {result.chapters} chapters)")
        return result
    finally:
        # a stopped encode leaves only the work directory, which is deleted here, so the
        # library never sees a partial file and the book is simply retried next scan
        shutil.rmtree(work_dir, ignore_errors=True)
        CONTROL.end_unit()
        CONVERT_LOCK.release()


def record(state: State, audit: Audit, status: str, output: str = "", note: str = "",
           result=None) -> None:
    custom = overrides_mod.normalise(audit.overrides)
    detail = json.dumps({
        "action": audit.action,
        "hours": audit.hours,
        "match": {
            "asin": audit.match.asin,
            "title": audit.match.product.title if audit.match.product else "",
            "authors": audit.match.product.authors if audit.match.product else [],
            "hours": audit.match.product.hours if audit.match.product else 0,
            "confidence": audit.confidence,
            # kept so the status page can show cover art without a second catalog call
            "image": audit.match.product.image if audit.match.product else "",
            # a user-pinned ASIN that had no catalog data; the search result was used
            "asin_pin_failed": audit.match.asin_pin_failed,
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
        "output": ({
            "path": str(result.path),
            "mb": round(result.size / 1e6, 1),
            "hours": round(result.duration / 3600, 2),
            "chapters": result.chapters,
            "chapters_from": result.chapters_source,
        } if result else None),
    })
    state.save(UnitState(
        path=audit.unit.rel_path,
        status=status,
        confidence=audit.confidence,
        asin=audit.match.asin,
        title=(custom.get("title")
               or (audit.match.product.title if audit.match.product else audit.unit.title_guess)),
        author=(custom["authors"][0] if custom.get("authors")
                else (audit.match.product.authors[0]
                      if audit.match.product and audit.match.product.authors
                      else audit.unit.author_guess)),
        hours=audit.hours,
        files=audit.unit.n_files,
        output=output,
        note=note or "; ".join(audit.notes),
        src_mb=audit.src_mb,
        out_mb=round(result.size / 1e6, 1) if result else 0.0,
        chapters=result.chapters if result else 0,
        detail=detail,
    ))


def process_unit(state: State, unit: BookUnit, log=print, force: bool = False) -> Audit:
    """Audit, then convert when the confidence tier and runtime check allow it.

    ``force`` is the operator saying "I looked at this, convert it anyway": the tier, the runtime
    hold and the damaged-source hold are bypassed, but the output is still verified before it is
    published.
    """
    started = time.time()
    audit = audit_unit(unit, state.get_asin_override(unit.rel_path))
    audit.overrides = state.get_overrides(unit.rel_path)
    if audit.overrides:
        audit.notes.append("operator metadata overrides applied")
    log(f"  {audit.confidence:6} {audit.hours:>6.2f}h {unit.n_files:>3}f  "
        f"{audit.match.product.title if audit.match.product else '-'}")

    if not force and audit.unreadable and audit.action in ("merge+convert", "convert", "remux"):
        log(f"  skipping: {audit.unreadable} unreadable source file(s) - fix the source first")
        record(state, audit, "review",
               note=f"{audit.unreadable} unreadable source file(s); not converted")
    elif force and audit.match.product:
        log("  forced by operator - converting regardless of tier/hold")
        try:
            result = convert_unit(audit, log=log)
            if result is None:
                record(state, audit, "ready", note="already published")
            else:
                record(state, audit, "converted", str(result.path), result=result)
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            record(state, audit, "interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except Exception as exc:                        # noqa: BLE001
            log(f"  FAILED: {exc}")
            record(state, audit, "failed", note=str(exc)[:300])
    elif (audit.action in ("merge+convert", "convert", "remux")
          and audit.confidence in CONFIG.auto_tiers):
        try:
            result = convert_unit(audit, log=log)
            if result is None:                       # dry run, or already published
                record(state, audit, "ready", note="dry run - nothing written")
            else:
                record(state, audit, "converted", str(result.path), result=result)
        except Stopped:
            log("  stopped by operator - partial file discarded, book will be retried")
            record(state, audit, "interrupted", note="stopped by operator - retried next scan")
            CONTROL.pause()
        except Exception as exc:                        # noqa: BLE001 - one book must not stop a scan
            log(f"  FAILED: {exc}")
            record(state, audit, "failed", note=str(exc)[:300])
    elif audit.action == "hold":
        log("  held: runtime disagrees with the catalog (wrong edition?)")
        record(state, audit, "held")
    else:
        record(state, audit, "review")
    log(f"  done in {time.time() - started:.0f}s")
    state.heartbeat()          # keep "last scan" honest while a long pass is still running
    return audit

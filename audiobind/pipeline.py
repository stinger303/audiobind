"""One unit in, one finished book out: audit and convert."""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog, encode, library, matching
from .config import CONFIG
from .probe import probe
from .state import State, UnitState
from .units import BookUnit, looks_like_collection


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

    @property
    def runtime_matches(self) -> bool:
        if not self.match.product:
            return True
        return matching.runtime_ok(self.hours, self.match.product.hours, CONFIG.max_runtime_delta)


def audit_unit(unit: BookUnit) -> Audit:
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

    audit.match = matching.match_unit(unit)
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

    if audit.confidence not in ("HIGH", "MEDIUM"):
        audit.action = "review"
    elif not audit.runtime_matches:
        audit.action = "hold"
    else:
        audit.action = "merge+convert" if unit.multi_part else "convert"
    return audit


def convert_unit(audit: Audit, log=print) -> Path:
    """Encode, verify and file one book. Returns the published m4b path."""
    unit, match = audit.unit, audit.match
    if not match.product:
        raise ValueError("cannot convert without a catalog match")

    book = catalog.book(match.asin)
    series = (book.get("seriesPrimary") or {}).get("name", "")
    sequence = str((book.get("seriesPrimary") or {}).get("position", "") or "")
    authors = [a.get("name") for a in (book.get("authors") or []) if a.get("name")] or match.product.authors
    title = book.get("title") or match.product.title

    out_path = library.m4b_path(series, authors, title, sequence)
    if out_path.exists():
        log(f"  already published: {out_path}")
        return out_path
    if CONFIG.dry_run:
        log(f"  [dry-run] would write {out_path}")
        return None                     # nothing was written, so it is not "converted"

    work_dir = CONFIG.work_dir / f"{abs(hash(unit.rel_path)) % 10**8:08x}"
    try:
        duration = sum(probe(f).duration for f in unit.files)
        chapters, _source = encode.build_chapters(catalog.chapters(match.asin), duration, unit)
        result = encode.encode(unit, match, book, work_dir, out_path, log=log)
        encode.write_abs_metadata(out_path.parent / "metadata.json", book, match, chapters,
                                  result.duration)
        log(f"  published {out_path} ({result.size / 1e6:.0f} MB, {result.chapters} chapters)")
        return out_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def record(state: State, audit: Audit, status: str, output: str = "", note: str = "") -> None:
    state.save(UnitState(
        path=audit.unit.rel_path,
        status=status,
        confidence=audit.confidence,
        asin=audit.match.asin,
        title=(audit.match.product.title if audit.match.product else audit.unit.title_guess),
        author=(audit.match.product.authors[0] if audit.match.product and audit.match.product.authors
                else audit.unit.author_guess),
        hours=audit.hours,
        files=audit.unit.n_files,
        output=output,
        note=note or "; ".join(audit.notes),
    ))


def process_unit(state: State, unit: BookUnit, log=print) -> Audit:
    """Audit, then convert when the confidence tier and runtime check allow it."""
    started = time.time()
    audit = audit_unit(unit)
    log(f"  {audit.confidence:6} {audit.hours:>6.2f}h {unit.n_files:>3}f  "
        f"{audit.match.product.title if audit.match.product else '-'}")

    if audit.action in ("merge+convert", "convert") and audit.confidence in CONFIG.auto_tiers:
        try:
            output = convert_unit(audit, log=log)
            if output is None:                       # dry run
                record(state, audit, "ready", note="dry run - nothing written")
            else:
                record(state, audit, "converted", str(output))
        except Exception as exc:                        # noqa: BLE001 - one book must not stop a scan
            log(f"  FAILED: {exc}")
            record(state, audit, "failed", note=str(exc)[:300])
    elif audit.action == "hold":
        log("  held: runtime disagrees with the catalog (wrong edition?)")
        record(state, audit, "held")
    else:
        record(state, audit, "review")
    log(f"  done in {time.time() - started:.0f}s")
    return audit

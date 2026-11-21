"""Settings > Library: move already-converted books into the Audiobookshelf folder layout.

New conversions are filed by ``library.m4b_path``. Books converted before that layout existed
keep their old folders until the operator runs this - an explicit, previewed action that a scan
never triggers. Every row's target is computed from the metadata stored with it, the same rules
a fresh conversion would use. Nothing is ever overwritten: a book whose target already exists,
whose folder also holds another book's audio, or whose file sits outside OUTPUT_ROOT is skipped
with the reason. Every move is written to an undo journal in STATE_DIR as it happens.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import library
from . import overrides as overrides_mod
from .config import CONFIG
from .units import AUDIO_EXT, foreign_audio
from .util import loads_dict as _loads

log = logging.getLogger("audiobind")


@dataclass
class Move:
    state_key: str
    src: Path  # the row's output file as recorded
    dst: Path  # where the layout puts it
    folder: bool  # True: the book folder moves; False: only file names change in place

    def public(self) -> dict:
        return {"from": str(self.src), "to": str(self.dst), "state_key": self.state_key}


#: metadata.json's series entry: "Name #3", "Name #", or just "Name"
_SERIES_ENTRY = re.compile(r"^(?P<name>.*?)\s*#\s*(?P<seq>[^#]*)$")


def book_metadata(row) -> dict:
    """series / sequence / authors / title from the book's own ``metadata.json``, where present.

    The conversion writes it from the same catalog payload (operator corrections applied) it
    files the book with, so it is the closest record of where a fresh conversion would put the
    book. Only the keys found (and readable) are returned; anything else falls back to the row.
    """
    try:
        raw = (Path(row.output).parent / "metadata.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict = {}
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        found["title"] = title.strip()
    authors = data.get("authors")
    if isinstance(authors, list):
        names = [a.strip() for a in authors if isinstance(a, str) and a.strip()]
        if names:
            found["authors"] = names
    series = data.get("series")
    if isinstance(series, list):
        # [] is an explicit "no series"; the first entry is the primary series
        first = next((x.strip() for x in series if isinstance(x, str) and x.strip()), "")
        entry = _SERIES_ENTRY.match(first) if first else None
        found["series"] = (entry.group("name") if entry else first).strip()
        found["sequence"] = (entry.group("seq") if entry else "").strip()
    return found


def target_for(row) -> Path:
    """Where this converted row's output belongs. Raises ValueError if it would leave OUTPUT_ROOT.

    Reads the book's ``metadata.json`` first (see ``book_metadata``), so a migration files the
    book where a fresh conversion would; any field it lacks comes from the catalog match in
    ``detail.match``, with the operator corrections applied at conversion time
    (``detail.overrides``) on top.
    """
    detail = _loads(row.detail)
    match = detail.get("match") if isinstance(detail.get("match"), dict) else {}
    custom = overrides_mod.normalise(detail.get("overrides"))
    meta = book_metadata(row)
    authors = meta.get("authors") or custom.get("authors") or [str(a) for a in (match.get("authors") or []) if a]
    if "series" in meta:
        series, sequence = meta["series"], meta["sequence"]
    else:
        series = custom.get("series") or str(match.get("series") or "")
        sequence = str(custom.get("series_index") or match.get("sequence") or "")
    title = meta.get("title") or custom.get("title") or row.title or str(match.get("title") or "")
    ext = Path(row.output).suffix.lstrip(".").lower() or "m4b"
    return library.m4b_path(series, authors, title, sequence, ext=ext)


def _siblings(src: Path) -> list[Path]:
    """The book's own published files beside ``src``: the same stem in every audio format
    (an M4B + MP3 book is two files), ``src`` first."""
    found = [src]
    try:
        with os.scandir(src.parent) as entries:
            for entry in entries:
                path = Path(entry.path)
                if (
                    path != src
                    and entry.is_file(follow_symlinks=False)
                    and path.stem == src.stem
                    and path.suffix.lower() in AUDIO_EXT
                ):
                    found.append(path)
    except OSError:
        pass
    return found


def _inside(path: Path, root: Path) -> bool:
    return root in path.parents


def plan(state) -> dict:
    """The moves this run would make, and every book it would leave alone (with why).

    Reads only: nothing on disk or in the database changes.
    """
    root = Path(CONFIG.output_root).resolve()
    rows = state.all("converted")
    moves: list[Move] = []
    skipped: list[dict] = []
    unchanged = 0
    claimed: set[Path] = set()

    # which rows (of any status) point into which folder: a folder two rows share cannot move
    # for one of them without leaving the other pointing at nothing
    folders: dict[Path, int] = {}
    for _path, output in state.outputs():
        try:
            folder = Path(output).resolve().parent
        except (OSError, RuntimeError):
            continue
        folders[folder] = folders.get(folder, 0) + 1
    holders = {parent for folder in folders for parent in folder.parents}  # folders with a book below them

    def skip(row, reason: str) -> None:
        skipped.append({"from": row.output or row.path, "reason": reason, "state_key": row.path})

    for row in sorted(rows, key=lambda r: r.path):
        if not row.output:
            skip(row, "no published file is recorded for this book")
            continue
        src = Path(row.output)
        try:
            resolved = src.resolve()
        except (OSError, RuntimeError):
            skip(row, "the published file's path cannot be read")
            continue
        if not _inside(resolved, root):
            skip(row, "the published file is not inside OUTPUT_ROOT")
            continue
        if src.is_symlink() or not src.is_file():
            skip(row, "the published file is missing")
            continue
        try:
            dst = target_for(row)
        except ValueError:
            skip(row, "the new path would leave OUTPUT_ROOT")
            continue
        src_dir, dst_dir = resolved.parent, dst.parent.resolve()
        dst = dst_dir / dst.name
        if not _inside(dst_dir, root):
            # m4b_path checked the path as built; resolving it can still leave the root when a
            # folder on the way (an author folder, say) is a symlink to somewhere else
            skip(row, "the new path would leave OUTPUT_ROOT")
            continue
        if dst_dir == src_dir and dst.name == resolved.name:
            unchanged += 1
            continue
        if dst in claimed or dst_dir in claimed:
            skip(row, "another book in this run moves to the same place")
            continue
        shared = folders.get(src_dir, 0) > 1 or src_dir in holders
        if dst_dir == src_dir:
            # renaming a file another row records would leave that row pointing at nothing
            if shared:
                skip(row, "another book in Audiobind is filed in this folder")
                continue
            # only the file name changes: rename the book's files in place
            clash = [p for p in _siblings(resolved) if (dst_dir / (dst.stem + p.suffix)).exists()]
            if clash:
                skip(row, f"{dst_dir / (dst.stem + clash[0].suffix)} already exists")
                continue
            moves.append(Move(row.path, src, dst, folder=False))
            claimed.add(dst)
            continue
        if src_dir == root or src.parent.is_symlink():
            skip(row, "the book is not in a folder of its own")
            continue
        if os.path.lexists(dst_dir):
            skip(row, f"{dst_dir} already exists")
            continue
        if _inside(dst_dir, src_dir) or _inside(src_dir, dst_dir):
            skip(row, "the new folder would sit inside the old one")
            continue
        if shared:
            skip(row, "another book in Audiobind is filed in this folder")
            continue
        foreign = foreign_audio(SimpleNamespace(files=[str(p) for p in _siblings(resolved)]), [str(src_dir)])
        if foreign:
            skip(row, f"the folder also holds {len(foreign)} audio file(s) that belong to other books")
            continue
        moves.append(Move(row.path, src, dst, folder=True))
        claimed.update({dst, dst_dir})
    return {"moves": moves, "skipped": skipped, "unchanged": unchanged}


def _move_dir(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):  # checked again right before the move: never overwrite
        raise FileExistsError(errno.EEXIST, "target already exists", str(dst))
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def _rename_files(folder: Path, old: Path, new_stem: str, on_rename=None) -> list[tuple[str, str]]:
    """Rename the book's files inside ``folder`` from ``old``'s stem to ``new_stem``.

    ``on_rename(src, dst)`` is called after EACH rename, so the undo journal holds every file
    that moved even when a later rename fails.
    """
    # every clash is checked before anything is renamed, and the main file goes last, so a
    # failure part-way leaves the row's recorded file where it was
    pairs = [(p, folder / (new_stem + p.suffix)) for p in reversed(_siblings(folder / old.name))]
    pairs = [(a, b) for a, b in pairs if a != b]
    for _path, target in pairs:
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "target already exists", str(target))
    done: list[tuple[str, str]] = []
    for path, target in pairs:
        os.rename(path, target)
        done.append((str(path), str(target)))
        if on_rename is not None:
            on_rename(str(path), str(target))
    return done


def _prune_empty(start: Path, root: Path) -> None:
    """Remove folders the move left empty, up to (never including) OUTPUT_ROOT."""
    current = start
    while current != root and _inside(current, root):
        try:
            current.rmdir()  # only succeeds on an empty folder
        except OSError:
            return
        current = current.parent


def journal_path() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(CONFIG.state_dir) / f"abs-layout-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = Path(CONFIG.state_dir) / f"abs-layout-{stamp}-{n}.json"
    return path


def _write_journal(path: Path, entries: list[dict]) -> None:
    body = {"version": 1, "output_root": str(CONFIG.output_root), "moves": entries}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def apply(state) -> dict:
    """Make the planned moves. Carries on past a failure; every move lands in the journal."""
    result = plan(state)
    root = Path(CONFIG.output_root).resolve()
    journal = journal_path()
    entries: list[dict] = []
    moved: list[dict] = []
    failed: list[dict] = []
    if result["moves"]:
        _write_journal(journal, entries)  # written first: an empty journal proves the run started
    for move in result["moves"]:
        src = move.src.resolve()
        current = src  # where the book's main file is right now, recorded even after a half-done move

        def journal_rename(entry: dict):
            def note(a: str, b: str) -> None:
                entry.setdefault("files", []).append({"from": a, "to": b})
                _write_journal(journal, entries)

            return note

        try:
            if move.folder:
                old_dir, new_dir = src.parent, move.dst.parent
                _move_dir(old_dir, new_dir)
                current = new_dir / src.name
                entry = {"state_key": move.state_key, "kind": "folder", "from": str(old_dir), "to": str(new_dir),
                         "at": time.time()}
                entries.append(entry)
                _write_journal(journal, entries)
                _prune_empty(old_dir.parent, root)
                if src.name != move.dst.name:
                    _rename_files(new_dir, src, move.dst.stem, on_rename=journal_rename(entry))
            else:
                entry = {"state_key": move.state_key, "kind": "file", "at": time.time(), "files": []}
                entries.append(entry)
                _rename_files(src.parent, src, move.dst.stem, on_rename=journal_rename(entry))
            current = move.dst
        except OSError as exc:
            log.warning("Audiobookshelf layout: could not move %s: %s", move.src, exc)
            failed.append({"from": str(move.src), "to": str(move.dst), "state_key": move.state_key,
                           "error": exc.strerror or str(exc)})
        if current != src:
            _record_output(state, move.state_key, current, old=src)
        if current == move.dst:
            log.info("Audiobookshelf layout: %s -> %s", move.src, move.dst)
            moved.append(move.public())
    return {
        "moved": moved,
        "failed": failed,
        "skipped": result["skipped"],
        "unchanged": result["unchanged"],
        "journal": str(journal) if result["moves"] else "",
    }


def _record_output(state, key: str, output: Path, old: Path | None = None) -> None:
    """Point the row (and the output path kept in its detail) at the file's new place.

    ``old`` is where the main file was: when the move renamed the book's files to a new stem, the
    names in the detail's ``wrote`` list (CORE-04a) follow them - each one that is now on disk
    under its new name, so a half-done rename is recorded as it is.
    """
    row = state.get(key)
    detail = None
    if row is not None:
        data = _loads(row.detail)
        if isinstance(data.get("output"), dict) and data["output"].get("path"):
            data["output"]["path"] = str(output)
            wrote = data["output"].get("wrote")
            if old is not None and old.stem != output.stem and isinstance(wrote, list):
                data["output"]["wrote"] = sorted({_renamed(name, old.stem, output) for name in wrote})
            detail = json.dumps(data)
    state.set_output(key, str(output), detail)


def _renamed(name, old_stem: str, output: Path):
    """``name`` (from a row's wrote list) as _rename_files left it beside ``output``."""
    if not isinstance(name, str) or "/" in name:
        return name
    path = Path(name)
    if path.stem != old_stem or path.suffix.lower() not in AUDIO_EXT:
        return name
    new = output.stem + path.suffix
    folder = output.parent
    return new if os.path.lexists(folder / new) and not os.path.lexists(folder / name) else name

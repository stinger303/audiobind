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
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import library, pipeline
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
    source: Path  # resolved source at preview time
    files: tuple[tuple[str, str], ...]  # old and new audio basenames, primary last

    def public(self) -> dict:
        return {"from": str(self.src), "to": str(self.dst), "state_key": self.state_key}

    def companion_moves(self) -> list[dict]:
        return [
            {"from": str(self.src.parent / old), "to": str(self.dst.parent / new)}
            for old, new in self.files if old != self.src.name and (self.folder or old != new)
        ]


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
        # Q-B12: the same resolution the panel and the series shelf use
        series, sequence, _source = overrides_mod.series_of(match, custom)
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
    return [src, *sorted(found[1:])]


def _audio_pairs(siblings: list[Path], target: Path) -> tuple[tuple[str, str], ...]:
    pairs = [(path.name, target.stem + path.suffix) for path in reversed(siblings[1:])]
    pairs.append((siblings[0].name, target.name))
    return tuple(pairs)


def _inside(path: Path, root: Path) -> bool:
    return root in path.parents


def plan(state) -> dict:
    """The moves this run would make, and every book it would leave alone (with why).

    Reads only: nothing on disk or in the database changes.
    """
    root = Path(CONFIG.output_root).resolve()
    revision, rows, outputs = state.layout_snapshot()
    moves: list[Move] = []
    skipped: list[dict] = []
    unchanged = 0
    claimed: set[Path] = set()

    # which rows (of any status) point into which folder: a folder two rows share cannot move
    # for one of them without leaving the other pointing at nothing
    folders: dict[Path, int] = {}
    for _path, output in outputs:
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
            pairs = _audio_pairs(_siblings(resolved), dst)
            clash = next(
                (dst_dir / new for old, new in pairs if old != new and os.path.lexists(dst_dir / new)), None,
            )
            if clash is not None:
                skip(row, f"{clash} already exists")
                continue
            moves.append(Move(row.path, src, dst, False, resolved, pairs))
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
        siblings = _siblings(resolved)
        foreign = foreign_audio(SimpleNamespace(files=[str(p) for p in siblings]), [str(src_dir)])
        if foreign:
            skip(row, f"the folder also holds {len(foreign)} audio file(s) that belong to other books")
            continue
        moves.append(Move(row.path, src, dst, True, resolved, _audio_pairs(siblings, dst)))
        claimed.update({dst, dst_dir})
    identity = {
        "revision": revision, "root": str(root), "outputs": sorted(outputs),
        "moves": [(move.public(), str(move.source), move.folder, move.files) for move in moves],
    }
    plan_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"moves": moves, "skipped": skipped, "unchanged": unchanged, "plan_id": plan_id}


def _prune_empty(start: Path, root: Path) -> None:
    """Remove folders the move left empty, up to (never including) OUTPUT_ROOT."""
    current = start
    while current != root and _inside(current, root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def journal_path(directory: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = directory / f"abs-layout-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"abs-layout-{stamp}-{n}.json"
    return path


def _write_journal(path: Path, entries: list[dict]) -> None:
    body = {"version": 2, "output_root": str(Path(CONFIG.output_root).resolve()), "moves": entries}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(body, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    pipeline._sync_directory(path.parent)


def _close_journal(path: Path) -> Path:
    journal = journal_path(path.parent)
    os.replace(path, journal)
    pipeline._sync_directory(path.parent)
    return journal


def _directory_id(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino] if stat.S_ISDIR(info.st_mode) else None


def _intent(move: Move) -> dict:
    src, dst = move.source, move.dst
    folder = dst.parent
    files = [
        {"from": str(folder / old_name), "to": str(folder / new_name),
         "identity": pipeline.file_identity(src.parent / old_name)}
        for old_name, new_name in move.files if old_name != new_name
    ]
    identity = pipeline.file_identity(src)
    if identity is None:
        raise FileNotFoundError(f"Layout source disappeared: {src}")
    entry = {
        "state_key": move.state_key, "kind": "folder" if move.folder else "file",
        "status": "open", "at": time.time(),
        "from": str(src.parent if move.folder else src), "to": str(folder if move.folder else dst),
        "source_output": str(src), "target_output": str(dst),
        "identity": identity, "files": files,
    }
    if move.folder:
        entry["folder_id"] = _directory_id(src.parent)
        entry["stage"] = str(folder.with_name(f".audiobind-layout-{uuid.uuid4().hex}"))
    return entry


def _rename(source: Path, target: Path) -> None:
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, "target already exists", str(target))
    os.rename(source, target)


def _rename_files(entry: dict, folder: Path) -> None:
    for pair in entry["files"]:
        source = folder / Path(pair["from"]).name
        target = folder / Path(pair["to"]).name
        if pipeline.file_identity(source) != pair["identity"]:
            raise OSError(f"Layout source changed: {source}")
        _rename(source, target)


def _copy_sync(source, dest):
    shutil.copy2(source, dest)
    with open(dest, "rb") as handle:
        os.fsync(handle.fileno())


def _move_dir(entry: dict, journal: Path, entries: list[dict]) -> None:
    src, dst, stage = (Path(entry[name]) for name in ("from", "to", "stage"))
    root = Path(CONFIG.output_root).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    for parent in dst.parents:
        pipeline._sync_directory(parent)
        if parent == root:
            break
    try:
        _rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    else:
        _rename_files(entry, dst)
        return

    shutil.copytree(src, stage, symlinks=True, copy_function=_copy_sync)
    _rename_files(entry, stage)
    for folder, _, _ in os.walk(stage, topdown=False):
        pipeline._sync_directory(Path(folder))
    entry["copy_id"] = _directory_id(stage)
    _write_journal(journal, entries)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "target already exists", str(dst))
    os.replace(stage, dst)


def _check_entry_paths(entry: dict, root: Path) -> None:
    names = [entry[key] for key in ("from", "to", "source_output", "target_output")]
    if "stage" in entry:
        names.append(entry["stage"])
    for pair in entry["files"]:
        names.extend((pair["from"], pair["to"]))
    for name in names:
        path = Path(name)
        if not _inside(path, root) or path.parent.resolve() != path.parent or path.is_symlink():
            raise OSError(f"Unsafe layout journal path: {path}")


def _rollback_files(entry: dict) -> None:
    for pair in reversed(entry["files"]):
        source, target = Path(pair["from"]), Path(pair["to"])
        if pipeline.file_identity(source) == pair["identity"]:
            continue
        if not os.path.lexists(source) and pipeline.file_identity(target) == pair["identity"]:
            _rename(target, source)
        else:
            raise OSError(f"Cannot restore layout file {source}; check {target}")


def _settle(state, entry: dict, root: Path) -> bool:
    """Finish a complete move or restore the original names. Safe to repeat after interruption."""
    _check_entry_paths(entry, root)
    src, dst = (Path(entry[name]) for name in ("source_output", "target_output"))
    folder_move = entry["kind"] == "folder"
    target_id = _directory_id(dst.parent) if folder_move else None
    copied = target_id is not None and target_id == entry.get("copy_id")
    moved = target_id is not None and target_id == entry.get("folder_id")
    complete = (not folder_move or copied or moved) and (
        pipeline.file_identity(dst) == entry["identity"]
        and all(pipeline.file_identity(Path(pair["to"])) == pair["identity"]
                and not os.path.lexists(pair["from"]) for pair in entry["files"])
    )
    if complete:
        pipeline._sync_directory(dst.parent)
        if folder_move:
            pipeline._sync_directory(dst.parent.parent)
            if os.path.lexists(src.parent):
                if not copied or _directory_id(src.parent) != entry["folder_id"]:
                    raise OSError(f"Layout source was replaced: {src.parent}")
                shutil.rmtree(src.parent)
            if src.parent.parent.is_dir():
                pipeline._sync_directory(src.parent.parent)
        _record_output(state, entry["state_key"], dst, old=src)
    else:
        if copied:
            raise OSError(f"Published layout copy changed: {dst.parent}")
        if not folder_move or moved:
            _rollback_files(entry)
            pipeline._sync_directory(dst.parent)
        if moved:
            _rename(dst.parent, src.parent)
            pipeline._sync_directory(dst.parent.parent)
            pipeline._sync_directory(src.parent.parent)
        if pipeline.file_identity(src) != entry["identity"]:
            raise OSError(f"Cannot restore layout source: {src}")

    if folder_move:
        stage = Path(entry["stage"])
        if stage.is_dir():
            shutil.rmtree(stage)
            pipeline._sync_directory(stage.parent)
    entry["status"] = "committed" if complete else "rolled_back"
    return complete


def _recover_locked(state) -> None:
    root = Path(CONFIG.output_root).resolve()
    journal = state.layout_intent
    try:
        body = json.loads(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise OSError(f"Cannot read the layout journal: {journal}") from exc
    if body["version"] != 2 or Path(body["output_root"]) != root:
        raise OSError(f"Layout journal does not match this output root: {journal}")
    for entry in body["moves"]:
        if entry["status"] == "open":
            _settle(state, entry, root)
            _write_journal(journal, body["moves"])
        if entry["status"] == "committed" and entry["kind"] == "folder":
            _prune_empty(Path(entry["from"]).parent, root)
    _close_journal(journal)

def recover(state) -> None:
    """Settle interrupted layout moves before discovery or another media write."""
    if state.layout_intent.exists():
        with state.layout_writer():
            _recover_locked(state)


class LayoutPlanChanged(Exception):
    """The current plan differs from the one the operator previewed."""


def apply(state, plan_id: str) -> dict:
    """Make only the previewed moves under the exclusive layout claim."""
    with state.layout_writer():
        pipeline.recover_publications(state)
        _recover_locked(state)
        result = plan(state)
        if result["plan_id"] != plan_id:
            raise LayoutPlanChanged("The library changed. Run a fresh preview before applying.")
        return _apply(state, result)


def _apply(state, result: dict) -> dict:
    root = Path(CONFIG.output_root).resolve()
    active = state.layout_intent
    entries: list[dict] = []
    moved: list[dict] = []
    failed: list[dict] = []
    for move in result["moves"]:
        entry = _intent(move)
        _check_entry_paths(entry, root)
        entries.append(entry)
        _write_journal(active, entries)
        error = None
        try:
            if move.folder:
                _move_dir(entry, active, entries)
            else:
                _rename_files(entry, move.dst.parent)
        except OSError as exc:
            error = exc
        complete = _settle(state, entry, root)
        _write_journal(active, entries)
        if complete:
            if move.folder:
                _prune_empty(Path(entry["from"]).parent, root)
            log.info("Audiobookshelf layout: %s -> %s", move.src, move.dst)
            moved.append(move.public())
        else:
            log.warning("Audiobookshelf layout: could not move %s: %s", move.src, error)
            message = (error.strerror or str(error)) if error else "incomplete move"
            failed.append({**move.public(), "error": message})
    return {
        "moved": moved, "failed": failed, "skipped": result["skipped"],
        "unchanged": result["unchanged"], "journal": str(_close_journal(active)) if entries else "",
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
                ids = data["output"].get("wrote_id")
                if isinstance(ids, dict):  # CORE-04d: a rename keeps the size and mtime
                    data["output"]["wrote_id"] = {_renamed(name, old.stem, output): ident
                                                  for name, ident in ids.items()}
            detail = json.dumps(data)
    if not state.set_output(key, str(output), detail):
        raise OSError(f"Cannot record layout move for missing unit {key}")


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

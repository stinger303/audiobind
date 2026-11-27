"""Read-only reconciliation of library paths and recorded publications."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CONFIG
from .ingest import configured_dir, rel_of
from .state import INGEST_KEY_PREFIX, State, UnitState
from .units import _skip, audio_files, library_path_key


@dataclass(frozen=True, order=True)
class PathEntry:
    path: str


@dataclass(frozen=True, order=True)
class OutputEntry:
    path: str
    output: str


def published_units(state: State) -> list[UnitState]:
    """Publication is recorded by output, including ready and rechecked rows.

    A dry-run ready row has no output. A converted row should always have one.
    """
    return sorted(
        (row for row in state.all(blobs=False) if row.output or row.status == "converted"),
        key=lambda row: row.path,
    )


def _source_exists(root: Path, key: str) -> bool:
    # Prefer literal paths, which may themselves contain '#'. Grouped keys append
    # one or more suffixes to the directory (or to the name used for the root).
    root_key = library_path_key(root, root)
    while True:
        if (root / key).exists() or (key == root_key and audio_files(str(root))):
            return True
        key, marker, _ = key.rpartition("#")
        if not marker:
            return False


def missing_sources(state: State) -> list[PathEntry]:
    found = []
    for key, _ in state.path_statuses():
        if key.startswith(INGEST_KEY_PREFIX):
            ingest_root = configured_dir()
            if not ingest_root:
                continue
            root, relative = Path(ingest_root), rel_of(key)
        else:
            root, relative = Path(CONFIG.library_root), key
        if not _source_exists(root, relative):
            found.append(PathEntry(key))
    return sorted(found)


def missing_outputs(state: State) -> list[OutputEntry]:
    return [
        OutputEntry(row.path, row.output)
        for row in published_units(state)
        if not row.output or not (Path(CONFIG.output_root) / row.output).is_file()
    ]


def orphans(state: State) -> list[PathEntry]:
    root = Path(CONFIG.library_root)
    keys = {key for key, _ in state.path_statuses()}
    found = []
    for folder, dirs, _ in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not _skip(name, CONFIG.ignore_globs))
        if Path(folder) != root:
            key = library_path_key(folder, root)
            if key not in keys:
                found.append(PathEntry(key))
    return sorted(found)


def report(state: State) -> dict:
    return {
        "missing_sources": [asdict(entry) for entry in missing_sources(state)],
        "missing_outputs": [asdict(entry) for entry in missing_outputs(state)],
        "orphans": [asdict(entry) for entry in orphans(state)],
        "truncated": False,
    }

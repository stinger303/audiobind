"""SQLite manifest: what has been seen, matched and converted. Survives restarts."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    path        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    confidence  TEXT,
    asin        TEXT,
    title       TEXT,
    author      TEXT,
    hours       REAL,
    files       INTEGER,
    output      TEXT,
    note        TEXT,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL,
    finished_at REAL,
    seen        INTEGER,
    converted   INTEGER,
    held        INTEGER,
    failed      INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class UnitState:
    path: str
    status: str
    confidence: str = ""
    asin: str = ""
    title: str = ""
    author: str = ""
    hours: float = 0.0
    files: int = 0
    output: str = ""
    note: str = ""
    updated_at: float = 0.0


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "audiobind.db"
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # ---------------------------------------------------------------- units
    def get(self, path: str) -> UnitState | None:
        row = self.connection.execute("SELECT * FROM units WHERE path = ?", (path,)).fetchone()
        return UnitState(**{k: row[k] for k in row.keys() if k in UnitState.__annotations__}) if row else None

    def save(self, state: UnitState) -> None:
        self.connection.execute(
            """INSERT INTO units (path, status, confidence, asin, title, author, hours, files,
                                  output, note, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 status=excluded.status, confidence=excluded.confidence, asin=excluded.asin,
                 title=excluded.title, author=excluded.author, hours=excluded.hours,
                 files=excluded.files, output=excluded.output, note=excluded.note,
                 updated_at=excluded.updated_at""",
            (state.path, state.status, state.confidence, state.asin, state.title,
             state.author, state.hours, state.files, state.output, state.note, time.time()),
        )
        self.connection.commit()

    def all(self, status: str | None = None) -> list[UnitState]:
        query = "SELECT * FROM units" + (" WHERE status = ?" if status else "") + " ORDER BY updated_at DESC"
        rows = self.connection.execute(query, (status,) if status else ()).fetchall()
        return [UnitState(**{k: r[k] for k in r.keys() if k in UnitState.__annotations__}) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT status, COUNT(*) n FROM units GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---------------------------------------------------------------- runs
    def start_run(self) -> int:
        cursor = self.connection.execute("INSERT INTO runs (started_at) VALUES (?)", (time.time(),))
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, seen: int, converted: int, held: int, failed: int) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at=?, seen=?, converted=?, held=?, failed=? WHERE id=?",
            (time.time(), seen, converted, held, failed, run_id))
        self.connection.commit()

    def last_run(self) -> dict | None:
        row = self.connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        self.connection.commit()

    def get_meta(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def heartbeat(self) -> None:
        self.set_meta("heartbeat", time.time())

    def heartbeat_age(self) -> float:
        beat = self.get_meta("heartbeat")
        return float("inf") if not beat else time.time() - beat

    def close(self) -> None:
        self.connection.close()

"""SQLite manifest: what has been seen, matched and converted. Survives restarts."""
from __future__ import annotations

import json
import sqlite3
import threading
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
    updated_at  REAL,
    src_mb      REAL DEFAULT 0,
    out_mb      REAL DEFAULT 0,
    chapters    INTEGER DEFAULT 0,
    detail      TEXT DEFAULT '',
    asin_override TEXT DEFAULT '',
    overrides   TEXT DEFAULT ''
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
    src_mb: float = 0.0
    out_mb: float = 0.0
    chapters: int = 0
    detail: str = ""
    overrides: str = ""


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "audiobind.db"
        # autocommit + a lock: several threads share this connection, and SQLite cannot nest
        # transactions on one of them ("cannot start a transaction within a transaction")
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30,
                                          isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database already exists."""
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(units)")}
        for name, ddl in (("src_mb", "REAL DEFAULT 0"), ("out_mb", "REAL DEFAULT 0"),
                          ("chapters", "INTEGER DEFAULT 0"), ("detail", "TEXT DEFAULT ''"),
                          ("asin_override", "TEXT DEFAULT ''"),
                          ("overrides", "TEXT DEFAULT ''")):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE units ADD COLUMN {name} {ddl}")

    # ---------------------------------------------------------------- units
    def get(self, path: str) -> UnitState | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM units WHERE path = ?", (path,)).fetchone()
        return UnitState(**{k: row[k] for k in row.keys() if k in UnitState.__annotations__}) if row else None

    def save(self, state: UnitState) -> None:
        with self._lock:
            self._save_locked(state)

    def _save_locked(self, state: UnitState) -> None:
        self.connection.execute(
            """INSERT INTO units (path, status, confidence, asin, title, author, hours, files,
                                  output, note, updated_at, src_mb, out_mb, chapters, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 status=excluded.status, confidence=excluded.confidence, asin=excluded.asin,
                 title=excluded.title, author=excluded.author, hours=excluded.hours,
                 files=excluded.files, output=excluded.output, note=excluded.note,
                 updated_at=excluded.updated_at, src_mb=excluded.src_mb, out_mb=excluded.out_mb,
                 chapters=excluded.chapters, detail=excluded.detail""",
            (state.path, state.status, state.confidence, state.asin, state.title,
             state.author, state.hours, state.files, state.output, state.note, time.time(),
             state.src_mb, state.out_mb, state.chapters, state.detail),
        )
        self.connection.commit()

    def all(self, status: str | None = None) -> list[UnitState]:
        query = "SELECT * FROM units" + (" WHERE status = ?" if status else "") + " ORDER BY updated_at DESC"
        with self._lock:
            rows = self.connection.execute(query, (status,) if status else ()).fetchall()
        return [UnitState(**{k: r[k] for k in r.keys() if k in UnitState.__annotations__}) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) n FROM units GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def detail_for_asin(self, asin: str) -> str:
        """The newest detail blob recorded for an ASIN (the cover proxy reads the image URL)."""
        if not asin:
            return ""
        with self._lock:
            row = self.connection.execute(
                "SELECT detail FROM units WHERE asin = ? AND detail != '' "
                "ORDER BY updated_at DESC LIMIT 1", (asin,)).fetchone()
        return (row["detail"] if row else "") or ""

    # ---------------------------------------------------------------- runs
    def start_run(self) -> int:
        with self._lock:
            cursor = self.connection.execute("INSERT INTO runs (started_at) VALUES (?)",
                                             (time.time(),))
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, seen: int, converted: int, held: int, failed: int) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET finished_at=?, seen=?, converted=?, held=?, failed=? WHERE id=?",
                (time.time(), seen, converted, held, failed, run_id))

    def last_run(self) -> dict | None:
        with self._lock:                 # a background prefetch may be reading at the same time
            row = self.connection.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_asin_override(self, path: str, asin: str) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, asin_override, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET asin_override = excluded.asin_override",
                (path, "review", asin, time.time()))

    def get_asin_override(self, path: str) -> str:
        with self._lock:
            row = self.connection.execute("SELECT asin_override FROM units WHERE path = ?",
                                          (path,)).fetchone()
        return (row["asin_override"] if row and row["asin_override"] else "")

    # ------------------------------------------------- metadata overrides
    def set_overrides(self, path: str, data: dict) -> None:
        """Operator-entered metadata corrections (title/author/series/...)."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, overrides, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET overrides = excluded.overrides",
                (path, "review", json.dumps(data), time.time()))

    def get_overrides(self, path: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT overrides FROM units WHERE path = ?",
                                          (path,)).fetchone()
        if not row or not row["overrides"]:
            return {}
        try:
            data = json.loads(row["overrides"])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def heartbeat(self) -> None:
        self.set_meta("heartbeat", time.time())

    def heartbeat_age(self) -> float:
        beat = self.get_meta("heartbeat")
        return float("inf") if not beat else time.time() - beat

    def close(self) -> None:
        self.connection.close()

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
    overrides   TEXT DEFAULT '',
    alternates  TEXT DEFAULT ''
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
CREATE TABLE IF NOT EXISTS series_cache (
    key         TEXT PRIMARY KEY,
    fetched_at  REAL,
    ok          INTEGER,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS series_asins (
    asin        TEXT PRIMARY KEY,
    series_key  TEXT,
    image       TEXT
);
CREATE TABLE IF NOT EXISTS owned_marks (
    asin        TEXT PRIMARY KEY,
    marked_at   REAL
);
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
    # no ``overrides`` field on purpose: operator metadata corrections are owned by
    # set_overrides()/get_overrides(). save() never writes that column, so a field here
    # would only be a stale copy that a re-save could be mistaken for
    alternates: str = ""


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "audiobind.db"
        # autocommit + a lock: several threads share this connection, and SQLite cannot nest
        # transactions on one of them ("cannot start a transaction within a transaction")
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database already exists."""
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(units)")}
        for name, ddl in (
            ("src_mb", "REAL DEFAULT 0"),
            ("out_mb", "REAL DEFAULT 0"),
            ("chapters", "INTEGER DEFAULT 0"),
            ("detail", "TEXT DEFAULT ''"),
            ("asin_override", "TEXT DEFAULT ''"),
            ("overrides", "TEXT DEFAULT ''"),
            ("alternates", "TEXT DEFAULT ''"),
        ):
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
                                  output, note, updated_at, src_mb, out_mb, chapters, detail,
                                  alternates)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 status=excluded.status, confidence=excluded.confidence, asin=excluded.asin,
                 title=excluded.title, author=excluded.author, hours=excluded.hours,
                 files=excluded.files, output=excluded.output, note=excluded.note,
                 updated_at=excluded.updated_at, src_mb=excluded.src_mb, out_mb=excluded.out_mb,
                 chapters=excluded.chapters, detail=excluded.detail,
                 alternates=excluded.alternates""",
            (
                state.path,
                state.status,
                state.confidence,
                state.asin,
                state.title,
                state.author,
                state.hours,
                state.files,
                state.output,
                state.note,
                time.time(),
                state.src_mb,
                state.out_mb,
                state.chapters,
                state.detail,
                state.alternates,
            ),
        )
        self.connection.commit()

    def all(self, status: str | None = None) -> list[UnitState]:
        query = "SELECT * FROM units" + (" WHERE status = ?" if status else "") + " ORDER BY updated_at DESC"
        with self._lock:
            rows = self.connection.execute(query, (status,) if status else ()).fetchall()
        return [UnitState(**{k: r[k] for k in r.keys() if k in UnitState.__annotations__}) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute("SELECT status, COUNT(*) n FROM units GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def rev(self) -> str:
        """A cheap 'did anything change' signature: newest row timestamp + row count.

        Unlike counts(), this moves even when a re-check ends in the same status, so
        the web page can notice a manual Retry / ASIN re-check that changed nothing
        but the detail blob."""
        with self._lock:
            row = self.connection.execute("SELECT MAX(updated_at), COUNT(*) FROM units").fetchone()
        return f"{row[0] or 0.0:.6f}|{row[1]}"

    def clear_reopen(self, path: str) -> None:
        """The status page has shown this book's re-check banner - stop re-opening it.

        Read and write happen under one lock hold: a record() from a manual re-check that
        lands in between must not be overwritten with the stale detail read here.
        """
        with self._lock:
            row = self.connection.execute("SELECT detail FROM units WHERE path=?", (path,)).fetchone()
            if not row or not row["detail"]:
                return
            try:
                d = json.loads(row["detail"])
            except ValueError:
                return
            if not isinstance(d, dict) or not d.get("reopen"):
                return
            d["reopen"] = False
            self.connection.execute("UPDATE units SET detail=? WHERE path=?", (json.dumps(d), path))

    def has_asin(self, asin: str) -> bool:
        """True when some book in the library is matched or pinned to this ASIN."""
        if not asin:
            return False
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM units WHERE asin = ? OR asin_override = ? LIMIT 1", (asin, asin)
            ).fetchone()
        return row is not None

    def detail_for_asin(self, asin: str) -> str:
        """The newest detail blob recorded for an ASIN (the cover proxy reads the image URL)."""
        if not asin:
            return ""
        with self._lock:
            row = self.connection.execute(
                "SELECT detail FROM units WHERE asin = ? AND detail != '' ORDER BY updated_at DESC LIMIT 1",
                (asin,),
            ).fetchone()
        return (row["detail"] if row else "") or ""

    # ---------------------------------------------------------------- runs
    def start_run(self) -> int:
        with self._lock:
            cursor = self.connection.execute("INSERT INTO runs (started_at) VALUES (?)", (time.time(),))
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, seen: int, converted: int, held: int, failed: int) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET finished_at=?, seen=?, converted=?, held=?, failed=? WHERE id=?",
                (time.time(), seen, converted, held, failed, run_id),
            )

    def last_run(self) -> dict | None:
        with self._lock:  # a background prefetch may be reading at the same time
            row = self.connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_asin_override(self, path: str, asin: str) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, asin_override, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET asin_override = excluded.asin_override",
                (path, "review", asin, time.time()),
            )

    def get_asin_override(self, path: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT asin_override FROM units WHERE path = ?", (path,)
            ).fetchone()
        return row["asin_override"] if row and row["asin_override"] else ""

    # ------------------------------------------------- metadata overrides
    def set_overrides(self, path: str, data: dict) -> None:
        """Operator-entered metadata corrections (title/author/series/...)."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, overrides, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET overrides = excluded.overrides",
                (path, "review", json.dumps(data), time.time()),
            )

    def get_overrides(self, path: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT overrides FROM units WHERE path = ?", (path,)).fetchone()
        if not row or not row["overrides"]:
            return {}
        try:
            data = json.loads(row["overrides"])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------- alternate editions
    def set_alternates(self, path: str, data: list[dict]) -> None:
        """Other Audible candidates seen while matching this unit (top few editions)."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, alternates, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET alternates = excluded.alternates",
                (path, "review", json.dumps(data or []), time.time()),
            )

    def get_alternates(self, path: str) -> list[dict]:
        with self._lock:
            row = self.connection.execute("SELECT alternates FROM units WHERE path = ?", (path,)).fetchone()
        if not row or not row["alternates"]:
            return []
        try:
            data = json.loads(row["alternates"])
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    # ------------------------------------------------- series shelf
    def series_rows(self) -> list[dict]:
        """Every unit with the columns the series shelf reads, overrides included, in one query."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT path, status, asin, asin_override, title, author, output, detail, overrides "
                "FROM units ORDER BY path"
            ).fetchall()
        return [{k: (r[k] or "") for k in r.keys()} for r in rows]

    def series_cache_all(self) -> dict[str, dict]:
        """{key: {"fetched_at", "ok", "payload"}} - the last Audible series list per series."""
        with self._lock:
            rows = self.connection.execute("SELECT key, fetched_at, ok, payload FROM series_cache").fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except ValueError:
                payload = {}
            out[r["key"]] = {
                "fetched_at": float(r["fetched_at"] or 0),
                "ok": bool(r["ok"]),
                "payload": payload if isinstance(payload, dict) else {},
            }
        return out

    def series_cache_put(self, key: str, ok: bool, payload: dict, asins: list[tuple[str, str]]) -> None:
        """Store one series list, and replace the ASINs it allows the cover proxy to fetch."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO series_cache (key, fetched_at, ok, payload) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, ok=excluded.ok, "
                "payload=excluded.payload",
                (key, time.time(), 1 if ok else 0, json.dumps(payload or {})),
            )
            if ok:
                # a failed refresh keeps the previous allowlist: the covers already shown stay valid
                self.connection.execute("DELETE FROM series_asins WHERE series_key = ?", (key,))
                self.connection.executemany(
                    "INSERT INTO series_asins (asin, series_key, image) VALUES (?,?,?) "
                    "ON CONFLICT(asin) DO UPDATE SET series_key=excluded.series_key, image=excluded.image",
                    [(asin, key, image or "") for asin, image in asins if asin],
                )
            self._bump_series_rev_locked()

    def series_image(self, asin: str) -> str | None:
        """The catalog image URL for an ASIN in a fetched series list; None when it is in none."""
        if not asin:
            return None
        with self._lock:
            row = self.connection.execute("SELECT image FROM series_asins WHERE asin = ?", (asin,)).fetchone()
        return (row["image"] or "") if row else None

    def set_owned_mark(self, asin: str, on: bool = True) -> None:
        """"I have this": the book is yours without a rip, so the shelf stops counting it missing."""
        with self._lock:
            if on:
                self.connection.execute(
                    "INSERT INTO owned_marks (asin, marked_at) VALUES (?,?) "
                    "ON CONFLICT(asin) DO UPDATE SET marked_at=excluded.marked_at",
                    (asin, time.time()),
                )
            else:
                self.connection.execute("DELETE FROM owned_marks WHERE asin = ?", (asin,))
            self._bump_series_rev_locked()

    def owned_marks(self) -> set[str]:
        with self._lock:
            rows = self.connection.execute("SELECT asin FROM owned_marks").fetchall()
        return {r["asin"] for r in rows}

    def _bump_series_rev_locked(self) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES ('series_rev', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        )

    def series_rev(self) -> int:
        """Moves on every series-list store and every "I have this" change (part of /api/state rev)."""
        with self._lock:
            row = self.connection.execute("SELECT value FROM meta WHERE key = 'series_rev'").fetchone()
        try:
            return int(json.loads(row["value"])) if row else 0
        except (ValueError, TypeError):
            return 0

    def delete(self, path: str) -> bool:
        """Remove a unit row from the database. Returns True if a row was deleted."""
        with self._lock:
            cursor = self.connection.execute("DELETE FROM units WHERE path = ?", (path,))
            self.connection.commit()
            return cursor.rowcount > 0

    def heartbeat(self) -> None:
        self.set_meta("heartbeat", time.time())

    def heartbeat_age(self) -> float:
        beat = self.get_meta("heartbeat")
        return float("inf") if not beat else time.time() - beat

    def close(self) -> None:
        self.connection.close()

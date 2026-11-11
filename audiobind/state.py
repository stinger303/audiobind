"""SQLite manifest: what has been seen, matched and converted. Survives restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
    alternates  TEXT DEFAULT '',
    source      TEXT DEFAULT 'library'
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
CREATE TABLE IF NOT EXISTS field_locks (
    path        TEXT NOT NULL,
    field       TEXT NOT NULL,
    locked_at   REAL,
    PRIMARY KEY (path, field)
);
"""

#: metadata fields a human edit locks: the editor's fields plus the ASIN set with Edit ASIN.
#: A locked field is never overwritten by a rescan, re-check or re-rip; unlocking hands it
#: back to Audible's catalog.
LOCKABLE_FIELDS = ("title", "authors", "narrator", "series", "series_index", "asin")

#: where a unit's source files live. A library row's ``path`` is relative to LIBRARY_ROOT
#: (unchanged since 0.1); an ingest row's is ``ingest:`` + its path relative to INGEST_DIR.
#: The prefix keeps the two namespaces apart in the one primary key: the same folder name
#: in the library and in the torrent folder are two different rows, never one.
SOURCE_LIBRARY = "library"
SOURCE_INGEST = "ingest"
INGEST_KEY_PREFIX = "ingest:"


def key_source(path: str) -> str:
    """The source a state key belongs to - read from the key itself, so it can never drift."""
    return SOURCE_INGEST if str(path).startswith(INGEST_KEY_PREFIX) else SOURCE_LIBRARY


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
    #: "library" or "ingest". Informational on the way in: save() always stores the source
    #: the key says (key_source), so a row can never claim a source its path does not have
    source: str = SOURCE_LIBRARY


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
        self._migrate_locks()
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
            # every row that predates ingest came from the library, which is what the default says
            ("source", "TEXT DEFAULT 'library'"),
        ):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE units ADD COLUMN {name} {ddl}")

    def _migrate_locks(self) -> None:
        """Once per database: every correction typed before field locks existed was a human
        edit, so it starts out locked - exactly as if it had been saved today."""
        if self.connection.execute("SELECT 1 FROM meta WHERE key = 'field_locks_v1'").fetchone():
            return
        now = time.time()
        rows = self.connection.execute(
            "SELECT path, overrides, asin_override FROM units "
            "WHERE (overrides IS NOT NULL AND overrides != '') "
            "   OR (asin_override IS NOT NULL AND asin_override != '')"
        ).fetchall()
        for row in rows:
            fields = set()
            try:
                data = json.loads(row["overrides"] or "{}")
            except ValueError:
                data = {}
            if isinstance(data, dict):
                fields |= {f for f in LOCKABLE_FIELDS if f != "asin" and data.get(f)}
            if row["asin_override"]:
                fields.add("asin")
            self.connection.executemany(
                "INSERT OR IGNORE INTO field_locks (path, field, locked_at) VALUES (?,?,?)",
                [(row["path"], f, now) for f in sorted(fields)],
            )
        self.connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('field_locks_v1', 'true')")

    # ---------------------------------------------------------------- units
    def get(self, path: str) -> UnitState | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM units WHERE path = ?", (path,)).fetchone()
        return UnitState(**{k: row[k] for k in row.keys() if k in UnitState.__annotations__}) if row else None

    def save(self, state: UnitState) -> None:
        with self._lock:
            self._save_locked(state)

    def _save_locked(self, state: UnitState) -> None:
        state = self._honour_locks_locked(state)
        self.connection.execute(
            """INSERT INTO units (path, status, confidence, asin, title, author, hours, files,
                                  output, note, updated_at, src_mb, out_mb, chapters, detail,
                                  alternates, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 status=excluded.status, confidence=excluded.confidence, asin=excluded.asin,
                 title=excluded.title, author=excluded.author, hours=excluded.hours,
                 files=excluded.files, output=excluded.output, note=excluded.note,
                 updated_at=excluded.updated_at, src_mb=excluded.src_mb, out_mb=excluded.out_mb,
                 chapters=excluded.chapters, detail=excluded.detail,
                 alternates=excluded.alternates, source=excluded.source""",
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
                key_source(state.path),
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
                "INSERT INTO units (path, status, asin_override, updated_at, source) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET asin_override = excluded.asin_override",
                (path, "review", asin, time.time(), key_source(path)),
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
                "INSERT INTO units (path, status, overrides, updated_at, source) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET overrides = excluded.overrides",
                (path, "review", json.dumps(data), time.time(), key_source(path)),
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
                "INSERT INTO units (path, status, alternates, updated_at, source) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET alternates = excluded.alternates",
                (path, "review", json.dumps(data or []), time.time(), key_source(path)),
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

    # ------------------------------------------------- field locks
    @contextmanager
    def _txn(self):
        """All-or-nothing for a multi-statement write; the caller holds self._lock, so no
        other statement can interleave (and no transaction is ever nested)."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self.connection.execute("COMMIT")

    def locked_fields(self, path: str) -> set[str]:
        with self._lock:
            return self._locked_fields_locked(path)

    def _locked_fields_locked(self, path: str) -> set[str]:
        rows = self.connection.execute("SELECT field FROM field_locks WHERE path = ?", (path,)).fetchall()
        return {r["field"] for r in rows}

    def all_locks(self) -> dict[str, set[str]]:
        """{path: {field, ...}} for every book with at least one locked field (one query)."""
        with self._lock:
            rows = self.connection.execute("SELECT path, field FROM field_locks").fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["path"], set()).add(r["field"])
        return out

    def all_asin_overrides(self) -> dict[str, str]:
        """{path: asin} for every book whose ASIN was set by hand (one query)."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT path, asin_override FROM units "
                "WHERE asin_override IS NOT NULL AND asin_override != ''"
            ).fetchall()
        return {r["path"]: r["asin_override"] for r in rows}

    def lock_fields(self, path: str, fields) -> None:
        """A human edited these fields: lock them, and show the locked title/author at once."""
        wanted = [f for f in fields if f in LOCKABLE_FIELDS]
        if not wanted:
            return
        now = time.time()
        with self._lock, self._txn():
            self.connection.executemany(
                "INSERT OR IGNORE INTO field_locks (path, field, locked_at) VALUES (?,?,?)",
                [(path, f, now) for f in wanted],
            )
            row = self.connection.execute("SELECT * FROM units WHERE path = ?", (path,)).fetchone()
            if row is not None:
                current = UnitState(**{k: row[k] for k in row.keys() if k in UnitState.__annotations__})
                fixed = self._honour_locks_locked(current)
                if (fixed.title, fixed.author) != (current.title, current.author):
                    self.connection.execute(
                        "UPDATE units SET title = ?, author = ? WHERE path = ?",
                        (fixed.title, fixed.author, path),
                    )

    def unlock_field(self, path: str, field: str) -> bool:
        """One click unlocks: the field goes back to following Audible's catalog.

        The stored correction for that field is dropped (otherwise it would still win at
        conversion time), and the table's title/author fall back to the last catalog match.
        Unlocking the ASIN clears the hand-set ASIN, so the next re-check matches on its own.
        Returns False when the field was not locked.
        """
        if field not in LOCKABLE_FIELDS:
            return False
        with self._lock, self._txn():
            cursor = self.connection.execute(
                "DELETE FROM field_locks WHERE path = ? AND field = ?", (path, field)
            )
            if cursor.rowcount == 0:
                return False
            if field == "asin":
                self.connection.execute("UPDATE units SET asin_override = '' WHERE path = ?", (path,))
                return True
            row = self.connection.execute(
                "SELECT overrides, detail FROM units WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return True
            try:
                data = json.loads(row["overrides"] or "{}")
            except ValueError:
                data = {}
            data = data if isinstance(data, dict) else {}
            data.pop(field, None)
            self.connection.execute(
                "UPDATE units SET overrides = ? WHERE path = ?", (json.dumps(data) if data else "", path)
            )
            if field in ("title", "authors"):
                try:
                    match = (json.loads(row["detail"] or "{}") or {}).get("match") or {}
                except (ValueError, AttributeError):
                    match = {}
                if field == "title" and match.get("title"):
                    self.connection.execute(
                        "UPDATE units SET title = ? WHERE path = ?", (str(match["title"]), path)
                    )
                if field == "authors" and match.get("authors"):
                    self.connection.execute(
                        "UPDATE units SET author = ? WHERE path = ?", (str(match["authors"][0]), path)
                    )
            return True

    def _honour_locks_locked(self, state: UnitState) -> UnitState:
        """A locked title/author is never overwritten: whatever a rescan, re-check or re-rip
        recorded, the human value is what lands in the row. (The ASIN lock needs no guard here -
        save() never writes asin_override - and the other fields live in the overrides column,
        which save() never writes either.)"""
        locked = self._locked_fields_locked(state.path) & {"title", "authors"}
        if not locked:
            return state
        row = self.connection.execute("SELECT overrides FROM units WHERE path = ?", (state.path,)).fetchone()
        try:
            data = json.loads((row["overrides"] if row else "") or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            return state
        title, author = state.title, state.author
        if "title" in locked and data.get("title"):
            title = str(data["title"])
        if "authors" in locked and isinstance(data.get("authors"), list) and data["authors"]:
            author = str(data["authors"][0])
        if (title, author) == (state.title, state.author):
            return state
        return replace(state, title=title, author=author)

    def delete(self, path: str) -> bool:
        """Remove a unit row from the database. Returns True if a row was deleted."""
        with self._lock:
            self.connection.execute("DELETE FROM field_locks WHERE path = ?", (path,))
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

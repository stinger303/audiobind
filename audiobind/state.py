"""SQLite manifest: what has been seen, matched and converted. Survives restarts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .util import loads_dict

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
    source      TEXT DEFAULT 'library',
    pin_failures INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publications (
    attempt INTEGER PRIMARY KEY AUTOINCREMENT,
    unit    TEXT NOT NULL UNIQUE,
    plan    TEXT NOT NULL,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS quarantined_publications (
    attempt INTEGER PRIMARY KEY,
    unit TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL,
    outcome TEXT,
    reason TEXT NOT NULL,
    previous_note TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS credentials (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    algo          TEXT NOT NULL,
    iterations    INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash   TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wishlist (
    asin        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    authors     TEXT NOT NULL DEFAULT '[]',
    added_at    REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'wanted',
    origin      TEXT NOT NULL DEFAULT '',
    series      TEXT NOT NULL DEFAULT '',
    sequence    TEXT NOT NULL DEFAULT '',
    hours       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracker_handoffs (
    asin TEXT PRIMARY KEY,
    added_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_seen (
    asin        TEXT PRIMARY KEY,
    image       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    seen_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS discover_cache (
    key         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dismissed (
    state_key    TEXT PRIMARY KEY,
    dismissed_at REAL
);
"""

#: a book a Discover page showed is remembered this long (its cover stays fetchable and a
#: "Get this" click can use the catalog's metadata); wishlist rows are never forgotten
CATALOG_SEEN_TTL_SECONDS = 90 * 24 * 3600

#: a wishlist row's life. Phase 2 only ever writes "wanted"; the Phase 3 watcher moves a row
#: on from there, and only along WISHLIST_TRANSITIONS:
#:
#:   wanted ──found on the tracker, torrent handed to qBittorrent──▶ downloading ──▶ downloaded ──▶ in_library
#:     │ └──three searches in a row found nothing──▶ not_found ──Retry──▶ wanted
#:     └────download / qBittorrent failed──────────▶ error ──────Retry──▶ wanted
#:
#: "in_library" is reached from any state, but only by a library match (a converted book with
#: the same ASIN, or the same title and author) - never by the watcher itself.
WISHLIST_WANTED = "wanted"
WISHLIST_DOWNLOADING = "downloading"
WISHLIST_IN_LIBRARY = "in_library"
WISHLIST_NOT_FOUND = "not_found"
WISHLIST_ERROR = "error"
#: the torrent finished in qBittorrent; the row waits for the converted book (never searched again)
WISHLIST_DOWNLOADED = "downloaded"
WISHLIST_STATUSES = (
    WISHLIST_WANTED, WISHLIST_DOWNLOADING, WISHLIST_DOWNLOADED, WISHLIST_IN_LIBRARY, WISHLIST_NOT_FOUND,
    WISHLIST_ERROR,
)
WISHLIST_TRANSITIONS: dict[str, frozenset[str]] = {
    WISHLIST_WANTED: frozenset({WISHLIST_DOWNLOADING, WISHLIST_NOT_FOUND, WISHLIST_ERROR, WISHLIST_IN_LIBRARY}),
    WISHLIST_DOWNLOADING: frozenset({WISHLIST_DOWNLOADED, WISHLIST_IN_LIBRARY, WISHLIST_ERROR}),
    WISHLIST_DOWNLOADED: frozenset({WISHLIST_IN_LIBRARY}),
    WISHLIST_NOT_FOUND: frozenset({WISHLIST_WANTED, WISHLIST_IN_LIBRARY}),
    WISHLIST_ERROR: frozenset({WISHLIST_WANTED, WISHLIST_IN_LIBRARY}),
    WISHLIST_IN_LIBRARY: frozenset(),
}
#: the states a Retry click resets to "wanted"
WISHLIST_RETRYABLE = frozenset({WISHLIST_NOT_FOUND, WISHLIST_ERROR})
#: the states where a watcher search would actually run (or could): saving a new search title
#: resets only these to "wanted"
WISHLIST_SEARCHABLE = frozenset({WISHLIST_WANTED}) | WISHLIST_RETRYABLE
#: the states past searching: only an explicit Re-queue (wishlist_requeue) moves them back to "wanted"
WISHLIST_REQUEUEABLE = frozenset({WISHLIST_DOWNLOADING, WISHLIST_DOWNLOADED, WISHLIST_IN_LIBRARY})


def wishlist_transition_ok(current: str, new: str) -> bool:
    """True when a wishlist row may move from ``current`` to ``new``."""
    return new in WISHLIST_TRANSITIONS.get(current, frozenset())

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


_UNIT_FIELDS = frozenset(UnitState.__annotations__)


def _unit_from_row(row: sqlite3.Row) -> UnitState:
    """A units row as a UnitState; columns a UnitState has no field for (old or hand-made
    tables) are ignored. The one place a row becomes a UnitState."""
    return UnitState(**{k: row[k] for k in row.keys() if k in _UNIT_FIELDS})


@dataclass(frozen=True)
class Credentials:
    """The one account (single-user app: the table holds at most one row, id = 1).

    ``password_hash``/``salt`` are hex PBKDF2 output (see auth.hash_password) and are left
    out of repr() so a log line or traceback can never print them. ``version`` goes up on
    every change; sessions created under an older version stop working.
    """

    username: str
    password_hash: str = field(repr=False)
    salt: str = field(repr=False)
    algo: str = ""
    iterations: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 0


WRITER_BUSY = "another run is working on this book"


class WriterBusy(Exception):
    def __init__(self, message: str = WRITER_BUSY):
        super().__init__(message)


@contextmanager
def file_claim(path: Path, *, shared: bool = False, blocking: bool = False):
    """Lock a stable inode. The kernel releases the claim when the last descriptor closes."""
    with path.open("a") as handle:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(handle, operation if blocking else operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WriterBusy() from exc
        yield


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "audiobind.db"
        # autocommit + a lock: several threads share this connection, and SQLite cannot nest
        # transactions on one of them ("cannot start a transaction within a transaction")
        self._lock = threading.RLock()
        self._writer = threading.local()
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        # WAL: a reader and a writer no longer block each other inside SQLite, so each hold of
        # the RLock is shorter. The mode is stored in the file, so an existing rollback-journal
        # database converts once, here, and stays WAL. The RLock stays: it is what keeps the
        # threads sharing this one connection from interleaving transactions
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(SCHEMA)
        with self._lock, self._txn():
            self._migrate()
            self._migrate_locks()
        # after _migrate: asin_override may only just have been added. The cover cache and the
        # wishlist ask "is this ASIN in the library?" per ASIN; without these that is a full scan
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(units)")}
        for column in ("asin", "asin_override"):
            if column in columns:  # a very old or hand-made table may lack one: no index, no crash
                self.connection.execute(f"CREATE INDEX IF NOT EXISTS idx_units_{column} ON units({column})")
        # the 2-5 s status poll (/api/state) runs counts(), rev() and library_totals() on every
        # open tab; each was a full scan of the table, detail blobs included. These are covering
        # indexes for exactly those three queries (and status-filtered lists, newest-first lists)
        for name, columns_ddl, needs in (
            ("idx_units_updated_at", "updated_at", {"updated_at"}),
            ("idx_units_status_totals", "status, hours, out_mb", {"status", "hours", "out_mb"}),
        ):
            if needs <= columns:
                self.connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON units({columns_ddl})")
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
            # 0.21.0-dev11: checks in a row the hand-set ASIN had no catalog data (save() never writes it)
            ("pin_failures", "INTEGER DEFAULT 0"),
        ):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE units ADD COLUMN {name} {ddl}")
        # 0.18: the watcher's bookkeeping on each wishlist row
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(wishlist)")}
        for name, ddl in (
            ("note", "TEXT NOT NULL DEFAULT ''"),
            ("checked_at", "REAL NOT NULL DEFAULT 0"),
            ("updated_at", "REAL NOT NULL DEFAULT 0"),
            ("misses", "INTEGER NOT NULL DEFAULT 0"),
            ("tracker_tid", "TEXT NOT NULL DEFAULT ''"),
            ("torrent_hash", "TEXT NOT NULL DEFAULT ''"),
            # 0.18.0-dev12: an optional title the watcher searches instead of the catalog title
            ("search_title", "TEXT"),
            # 0.21.0-dev1: the freeleech credit ledger (additive; a credit is never refunded)
            ("wedge_spent", "INTEGER NOT NULL DEFAULT 0"),
            ("wedge_at", "REAL"),
        ):
            if name not in existing:
                self.connection.execute(f"ALTER TABLE wishlist ADD COLUMN {name} {ddl}")
                if name == "tracker_tid":
                    # 0.19.0-dev7: this column used to be named after the old site. Copy what it
                    # holds; the old column is left in place (harmless, and a downgrade still works).
                    for old in sorted(c for c in existing if c.endswith("_tid") and c != name):
                        self.connection.execute(f"UPDATE wishlist SET tracker_tid = {old} WHERE tracker_tid = ''")
        # STATECFG-01: the freeleech credit ledger in its own table. A wishlist row is deleted when
        # its book lands, and a spend counted from that row went with it - reopening the daily cap.
        # Created once, then seeded with the spends the wishlist rows still carry (additive: the
        # wishlist columns stay, and are still written, but are no longer what is counted)
        if not self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'credit_spends'"
        ).fetchone():
            self.connection.execute(
                "CREATE TABLE credit_spends (id INTEGER PRIMARY KEY, tracker_tid TEXT NOT NULL DEFAULT '', "
                "spent_at REAL NOT NULL)"
            )
            self.connection.execute(
                "INSERT INTO credit_spends (tracker_tid, spent_at) "
                "SELECT tracker_tid, wedge_at FROM wishlist WHERE wedge_spent > 0 AND wedge_at IS NOT NULL"
            )
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_credit_spends_at ON credit_spends(spent_at)")

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
    @property
    def layout_intent(self) -> Path:
        return self.path.parent / "abs-layout-active.json.open"

    def layout_writer(self, *, blocking: bool = False):
        return file_claim(self.path.parent / "media-layout.lock", blocking=blocking)

    @contextmanager
    def unit_writer(self, key: str):
        """Pair a shared layout claim with an exclusive book claim."""
        if getattr(self._writer, "unit", None) == key:
            yield
            return
        digest = hashlib.sha256(key.encode()).hexdigest()
        with (
            file_claim(self.path.parent / "media-layout.lock", shared=True),
            file_claim(self.path.parent / f"media-unit-{digest}.lock"),
        ):
            if self.layout_intent.exists():
                raise WriterBusy("an interrupted library layout move needs recovery")
            previous = getattr(self._writer, "unit", None)
            self._writer.unit = key
            try:
                yield
            finally:
                self._writer.unit = previous

    def output_writer(self, folder: Path):
        digest = hashlib.sha256(str(folder.resolve()).encode()).hexdigest()
        return file_claim(self.path.parent / f"media-output-{digest}.lock")

    def publication_lock(self, *, blocking: bool = True):
        """Serialize publication journal settlement across processes sharing this manifest."""
        return file_claim(self.path.parent / "publication.lock", blocking=blocking)

    @contextmanager
    def _durable_publication(self):
        with self._lock:
            self.connection.execute("PRAGMA synchronous=FULL")
            try:
                with self._txn():
                    yield
            finally:
                self.connection.execute("PRAGMA synchronous=NORMAL")

    def begin_publication(self, unit: str, plan: dict) -> int:
        with self._durable_publication():
            cursor = self.connection.execute(
                "INSERT INTO publications (unit, plan) VALUES (?, ?)", (unit, json.dumps(plan))
            )
            return cursor.lastrowid

    def seal_publication(self, attempt: int, plan: dict, outcome: UnitState) -> None:
        with self._durable_publication():
            self.connection.execute(
                "UPDATE publications SET plan = ?, outcome = ? WHERE attempt = ?",
                (json.dumps(plan), json.dumps(asdict(outcome)), attempt),
            )

    def pending_publications(self) -> list[tuple[int, dict, UnitState | None]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT attempt, plan, outcome FROM publications ORDER BY attempt"
            ).fetchall()
        return [(row["attempt"], json.loads(row["plan"]),
                 UnitState(**json.loads(row["outcome"])) if row["outcome"] else None) for row in rows]

    def quarantine_publication(self, attempt: int, reason: str) -> str:
        with self._durable_publication():
            intent = self.connection.execute(
                "SELECT * FROM publications WHERE attempt = ?", (attempt,)
            ).fetchone()
            unit = intent["unit"]
            row = self.get(unit)
            note = row.note if row else ""
            self.connection.execute(
                "INSERT INTO quarantined_publications VALUES (?, ?, ?, ?, ?, ?)",
                (attempt, unit, intent["plan"], intent["outcome"], reason, note),
            )
            if row is None:
                self._save_locked(UnitState(path=unit, status="review"))
            self.connection.execute("UPDATE units SET note = ? WHERE path = ?", (reason, unit))
            self.connection.execute("DELETE FROM publications WHERE attempt = ?", (attempt,))
            return unit

    def publication_quarantine(self, unit: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT reason FROM quarantined_publications WHERE unit = ?", (unit,)
            ).fetchone()
        return row["reason"] if row else ""

    def quarantined_publication_folders(self) -> list[Path]:
        with self._lock:
            rows = self.connection.execute("SELECT plan FROM quarantined_publications").fetchall()
        return [Path(json.loads(row["plan"])["stage"]).parent for row in rows]

    def dismiss_publication_quarantine(self, unit: str) -> bool:
        with self._durable_publication():
            row = self.connection.execute(
                "SELECT reason, previous_note FROM quarantined_publications WHERE unit = ?", (unit,)
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                "UPDATE units SET note = ? WHERE path = ? AND note = ?",
                (row["previous_note"], unit, row["reason"]),
            )
            self.connection.execute("DELETE FROM quarantined_publications WHERE unit = ?", (unit,))
            return True

    def finish_publication(self, attempt: int, outcome: UnitState | None) -> None:
        """Commit the row and retire the intent in the same durable SQLite transaction."""
        with self._durable_publication():
            if outcome is not None:
                self._save_locked(outcome)
            self.connection.execute("DELETE FROM publications WHERE attempt = ?", (attempt,))

    def get(self, path: str) -> UnitState | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM units WHERE path = ?", (path,)).fetchone()
        return _unit_from_row(row) if row else None

    def save(self, state: UnitState) -> None:
        with self._lock:
            self._save_locked(state)

    def _save_locked(self, state: UnitState) -> None:
        """Insert or update a row. An empty ``detail`` never replaces a stored one: a caller that
        builds a fresh UnitState without the blob must not wipe the match evidence, cover URL and
        reopen flag. Clearing the blob on purpose is clear_detail()."""
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
                 chapters=excluded.chapters,
                 detail=CASE WHEN excluded.detail IS NULL OR excluded.detail = ''
                             THEN units.detail ELSE excluded.detail END,
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

    def clear_detail(self, path: str) -> bool:
        """Drop a row's detail blob on purpose. save() will not do it: an empty ``detail`` there
        means "not supplied" and keeps what is stored. Returns True when a row had one."""
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE units SET detail = '' WHERE path = ? AND detail IS NOT NULL AND detail != ''",
                (path,),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def set_output(self, path: str, output: str, detail: str | None = None) -> bool:
        """Point a row at its moved output file (and optionally its rewritten detail JSON) without
        touching anything else - not updated_at, not the locks. Returns True when a row changed."""
        with self._durable_publication():
            if detail is None:
                cursor = self.connection.execute("UPDATE units SET output = ? WHERE path = ?", (output, path))
            else:
                cursor = self.connection.execute(
                    "UPDATE units SET output = ?, detail = ? WHERE path = ?", (output, detail, path)
                )
            return cursor.rowcount > 0

    #: every UnitState column except the three text blobs (detail, alternates; overrides is not a field)
    _LIGHT_COLUMNS = (
        "path, status, confidence, asin, title, author, hours, files, output, note, "
        "updated_at, src_mb, out_mb, chapters, source"
    )

    def all(self, status: str | None = None, blobs: bool = True) -> list[UnitState]:
        """Every row, newest first. ``blobs=False`` leaves ``detail`` and ``alternates`` empty:
        a caller that never reads them (the /api/units listing) skips the bulk of the bytes."""
        columns = "*" if blobs else self._LIGHT_COLUMNS
        query = f"SELECT {columns} FROM units" + (" WHERE status = ?" if status else "") + " ORDER BY updated_at DESC"
        with self._lock:
            rows = self.connection.execute(query, (status,) if status else ()).fetchall()
        return [_unit_from_row(r) for r in rows]

    def statuses_of(self, paths) -> dict[str, str]:
        """{path: status} for just these rows (primary-key lookups, not a table scan)."""
        paths = list(dict.fromkeys(paths))
        found: dict[str, str] = {}
        with self._lock:
            for i in range(0, len(paths), 500):
                chunk = paths[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for row in self.connection.execute(f"SELECT path, status FROM units WHERE path IN ({marks})", chunk):
                    found[row[0]] = row[1]
        return found

    def hydrate_unit_blobs(self, units: list[UnitState]) -> None:
        """Load table details and alternate editions for the selected rows in place."""
        by_path = {unit.path: unit for unit in units}
        paths = list(by_path)
        with self._lock:
            for i in range(0, len(paths), 500):
                chunk = paths[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = self.connection.execute(
                    f"SELECT path, detail, alternates FROM units WHERE path IN ({marks})", chunk
                )
                for path, detail, alternates in rows:
                    unit = by_path[path]
                    unit.detail = detail
                    unit.alternates = alternates

    def path_statuses(self) -> list[tuple[str, str]]:
        """(path, status) for every row - for callers that only tally or match by status and
        must not drag every row's ``detail`` blob out of SQLite (``all()`` does)."""
        with self._lock:
            rows = self.connection.execute("SELECT path, status FROM units").fetchall()
        return [(r[0], r[1]) for r in rows]

    def outputs(self) -> list[tuple[str, str]]:
        """(path, output) for every row that records a published file; no ``detail`` blob."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT path, output FROM units WHERE output IS NOT NULL AND output != ''"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def layout_snapshot(self) -> tuple[str, list[UnitState], list[tuple[str, str]]]:
        """Read the revision, candidates and output ownership from the same SQLite snapshot."""
        with self._lock:
            self.connection.execute("BEGIN")
            try:
                return self.rev(), self.all("converted"), self.outputs()
            finally:
                self.connection.rollback()

    def library_totals(self) -> tuple[int, int, float, float]:
        """(rows, converted rows, hours of converted books, output MB of converted books), summed
        in SQLite: the status page asks every 2-5 s per open tab, and all() would read every
        row's ``detail`` blob (and build a UnitState each) to add up three numbers."""
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*),"
                " COALESCE(SUM(status = 'converted'), 0),"
                " COALESCE(SUM(CASE WHEN status = 'converted' THEN COALESCE(hours, 0) END), 0),"
                " COALESCE(SUM(CASE WHEN status = 'converted' THEN COALESCE(out_mb, 0) END), 0)"
                " FROM units"
            ).fetchone()
        return int(row[0]), int(row[1]), float(row[2]), float(row[3])

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
        """The newest detail blob recorded for an ASIN (the cover proxy reads the image URL).

        Matched or pinned, like has_asin(): a book pinned with an ASIN override that was never
        matched keeps ``asin`` empty, and its blob must still be found."""
        if not asin:
            return ""
        with self._lock:
            row = self.connection.execute(
                "SELECT detail FROM units WHERE (asin = ? OR asin_override = ?) AND detail != '' "
                "ORDER BY updated_at DESC LIMIT 1",
                (asin, asin),
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

    def last_run(self, *, finished_only: bool = False) -> dict | None:
        where = "WHERE finished_at IS NOT NULL" if finished_only else ""
        with self._lock:  # a background prefetch may be reading at the same time
            row = self.connection.execute(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def set_meta_many(self, items: dict) -> None:
        """Several meta values in ONE transaction: all of them are stored or none is."""
        with self._lock:
            with self._txn():
                for key, value in items.items():
                    self.connection.execute(
                        "INSERT INTO meta (key, value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)),
                    )

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def get_meta_many(self, keys) -> dict:
        """{key: decoded value} for the keys that exist, in ONE query and one lock hold - the
        settings refresh that runs on every page and poll would otherwise be one per setting."""
        keys = list(dict.fromkeys(keys))  # SQLite's variable limit is far above the ~50 settings; chunk anyway
        found: dict = {}
        with self._lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for row in self.connection.execute(
                    f"SELECT key, value FROM meta WHERE key IN ({marks})", chunk
                ):
                    found[row["key"]] = row["value"]
        return {key: json.loads(value) for key, value in found.items()}

    def set_asin_override(self, path: str, asin: str) -> None:
        """The operator's ASIN pin. Setting one (even the same one again) starts its dead-pin
        count over: the operator has asked for it to be tried afresh."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, asin_override, updated_at, source, pin_failures) "
                "VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(path) DO UPDATE SET asin_override = excluded.asin_override, pin_failures = 0, "
                "updated_at = excluded.updated_at",
                (path, "review", asin, time.time(), key_source(path)),
            )

    def get_pin_failures(self, path: str) -> int:
        """Checks in a row on which this book's hand-set ASIN had no catalog data."""
        with self._lock:
            row = self.connection.execute("SELECT pin_failures FROM units WHERE path = ?", (path,)).fetchone()
        return int(row["pin_failures"] or 0) if row else 0

    def record_pin_failure(self, path: str) -> int:
        """One more check with no catalog data for the pin; returns the new count."""
        with self._lock:
            self.connection.execute(
                "UPDATE units SET pin_failures = COALESCE(pin_failures, 0) + 1 WHERE path = ?", (path,)
            )
        return self.get_pin_failures(path)

    def reset_pin_failures(self, path: str) -> None:
        with self._lock:
            self.connection.execute("UPDATE units SET pin_failures = 0 WHERE path = ?", (path,))

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
                # updated_at moves so rev() (the web poll) sees the edit in every open tab
                "ON CONFLICT(path) DO UPDATE SET overrides = excluded.overrides, updated_at = excluded.updated_at",
                (path, "review", json.dumps(data), time.time(), key_source(path)),
            )

    def get_overrides(self, path: str) -> dict:
        with self._lock:
            row = self.connection.execute("SELECT overrides FROM units WHERE path = ?", (path,)).fetchone()
        return loads_dict(row["overrides"]) if row else {}

    # ------------------------------------------------- alternate editions
    def set_alternates(self, path: str, data: list[dict]) -> None:
        """Other Audible candidates seen while matching this unit (top few editions)."""
        with self._lock:
            self.connection.execute(
                "INSERT INTO units (path, status, alternates, updated_at, source) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET alternates = excluded.alternates, updated_at = excluded.updated_at",
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

    def all_overrides(self) -> dict[str, dict]:
        """{path: overrides dict} for every book with a hand-made metadata correction (one query).

        Each value is what get_overrides() would return for that path; books without any are absent."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT path, overrides FROM units WHERE overrides IS NOT NULL AND overrides != ''"
            ).fetchall()
        found = {r["path"]: loads_dict(r["overrides"]) for r in rows}
        return {path: data for path, data in found.items() if data}

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
                current = _unit_from_row(row)
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
            # updated_at moves so rev() (the web poll) sees the unlock in every open tab: the
            # Unlock action returns without a re-audit, so nothing else would move it
            if field == "asin":
                self.connection.execute(
                    "UPDATE units SET asin_override = '', pin_failures = 0, updated_at = ? WHERE path = ?",
                    (time.time(), path),
                )
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
                "UPDATE units SET overrides = ?, updated_at = ? WHERE path = ?",
                (json.dumps(data) if data else "", time.time(), path),
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

    def delete(self, path: str, *, dismiss_ingest: bool = False) -> bool:
        """Remove a unit row and optionally dismiss its ingest key in one transaction."""
        with self._lock, self._txn():
            self.connection.execute("DELETE FROM field_locks WHERE path = ?", (path,))
            cursor = self.connection.execute("DELETE FROM units WHERE path = ?", (path,))
            if dismiss_ingest:
                self.dismiss(path)
            return cursor.rowcount > 0

    # ------------------------------------------------- dismissed ingest books
    # An ingest book removed from Audiobind while its torrent keeps seeding: the folder is still
    # in INGEST_DIR, so the scan skips its key instead of converting it again. A dismissal is
    # forgotten once the folder itself is gone (the daemon prunes it).
    def dismiss(self, key: str) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO dismissed (state_key, dismissed_at) VALUES (?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET dismissed_at=excluded.dismissed_at",
                (key, time.time()),
            )

    def is_dismissed(self, key: str) -> bool:
        with self._lock:
            row = self.connection.execute("SELECT 1 FROM dismissed WHERE state_key = ?", (key,)).fetchone()
        return row is not None

    def dismissed_keys(self) -> set[str]:
        with self._lock:
            rows = self.connection.execute("SELECT state_key FROM dismissed").fetchall()
        return {r["state_key"] for r in rows}

    def undismiss(self, key: str) -> bool:
        """Forget one dismissal. Returns True if there was one."""
        with self._lock:
            cursor = self.connection.execute("DELETE FROM dismissed WHERE state_key = ?", (key,))
            return cursor.rowcount > 0

    # ------------------------------------------------- the wishlist
    # Books you clicked "Get this" / "Want" on - every row is one click; nothing adds a row on
    # its own. The Phase 3 watcher (watcher.py) moves rows along WISHLIST_TRANSITIONS.
    _WISHLIST_INSERT = (
        "INSERT OR IGNORE INTO wishlist (asin, title, authors, added_at, status, origin, series, "
        "sequence, hours) VALUES (?,?,?,?,?,?,?,?,?)"
    )

    @staticmethod
    def _wishlist_params(item: dict, now: float) -> tuple:
        authors = [str(a) for a in (item.get("authors") or []) if str(a or "").strip()]
        try:
            hours = float(item.get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0
        return (
            str(item["asin"]),
            str(item.get("title") or ""),
            json.dumps(authors),
            now,
            WISHLIST_WANTED,
            str(item.get("origin") or ""),
            str(item.get("series") or ""),
            str(item.get("sequence") or ""),
            hours,
        )

    @staticmethod
    def _wishlist_row(row) -> dict:
        try:
            authors = json.loads(row["authors"] or "[]")
        except ValueError:
            authors = []
        return {
            "asin": row["asin"],
            "title": row["title"] or "",
            "authors": authors if isinstance(authors, list) else [],
            "added_at": float(row["added_at"] or 0),
            "status": row["status"] or WISHLIST_WANTED,
            "origin": row["origin"] or "",
            "series": row["series"] or "",
            "sequence": row["sequence"] or "",
            "hours": float(row["hours"] or 0),
            "note": row["note"] or "",
            "checked_at": float(row["checked_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "misses": int(row["misses"] or 0),
            "tracker_tid": row["tracker_tid"] or "",
            "torrent_hash": row["torrent_hash"] or "",
            "search_title": row["search_title"] or "",
        }

    def wishlist_add(self, item: dict) -> bool:
        """Add one book; True when it is new. Adding an ASIN that is already on the list
        changes nothing - not its added_at, not its status (idempotent)."""
        with self._lock:
            cursor = self.connection.execute(self._WISHLIST_INSERT, self._wishlist_params(item, time.time()))
        return cursor.rowcount > 0

    def wishlist_add_many(self, items: list[dict]) -> int:
        """Add several books all-or-nothing; returns how many were new."""
        now = time.time()
        added = 0
        with self._lock, self._txn():
            for item in items:
                cursor = self.connection.execute(self._WISHLIST_INSERT, self._wishlist_params(item, now))
                added += cursor.rowcount
        return added

    def wishlist_remove(self, asin: str) -> bool:
        with self._lock:
            cursor = self.connection.execute("DELETE FROM wishlist WHERE asin = ?", (asin,))
        return cursor.rowcount > 0

    def wishlist_get(self, asin: str) -> dict | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM wishlist WHERE asin = ?", (asin,)).fetchone()
        return self._wishlist_row(row) if row else None

    def wishlist_all(self) -> list[dict]:
        """Every row, newest first."""
        with self._lock:
            rows = self.connection.execute("SELECT * FROM wishlist ORDER BY added_at DESC, asin").fetchall()
        return [self._wishlist_row(r) for r in rows]

    def wishlist_asins(self) -> set[str]:
        with self._lock:
            rows = self.connection.execute("SELECT asin FROM wishlist").fetchall()
        return {r["asin"] for r in rows}

    def wishlist_count(self) -> int:
        with self._lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0])

    def wishlist_status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute("SELECT status, COUNT(*) n FROM wishlist GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def wishlist_due(self, limit: int) -> list[dict]:
        """The "wanted" rows the watcher searches next: never-checked rows first, oldest added
        first; then the least recently checked. Ordering by the last check is what keeps a
        handful of books the tracker does not have from starving every row behind them."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM wishlist WHERE status = ? "
                "AND asin NOT IN (SELECT asin FROM tracker_handoffs) "
                "ORDER BY checked_at ASC, added_at ASC, asin LIMIT ?",
                (WISHLIST_WANTED, max(0, int(limit))),
            ).fetchall()
        return [self._wishlist_row(r) for r in rows]

    def begin_tracker_handoff(self, asin: str, intent: dict) -> bool:
        with self._lock, self._txn():
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO tracker_handoffs (asin, added_at, payload) "
                "SELECT asin, added_at, ? FROM wishlist WHERE asin = ? AND status = ?",
                (json.dumps(intent), asin, WISHLIST_WANTED),
            )
            return cursor.rowcount == 1

    def tracker_handoffs(self) -> dict[str, dict]:
        with self._lock:
            rows = self.connection.execute("SELECT asin, payload FROM tracker_handoffs").fetchall()
        return {row["asin"]: json.loads(row["payload"]) for row in rows}

    def prepare_tracker_add(self, asin: str, intent: dict) -> None:
        with self._lock, self._txn():
            self.connection.execute(
                "UPDATE tracker_handoffs SET payload = ? WHERE asin = ?", (json.dumps(intent), asin)
            )

    def settle_tracker_handoff(self, asin: str, status: str | None, note: str = "", digest: str = "") -> None:
        with self._lock, self._txn():
            row = self.connection.execute("SELECT * FROM tracker_handoffs WHERE asin = ?", (asin,)).fetchone()
            if not row:
                return
            intent = json.loads(row["payload"])
            spent_at = intent.get("spent_at", 0)
            if spent_at:
                self.connection.execute(
                    "INSERT INTO credit_spends (tracker_tid, spent_at) VALUES (?, ?)",
                    (intent["tid"], spent_at),
                )
            if status:
                now = time.time()
                self.connection.execute(
                    "UPDATE wishlist SET status = ?, note = ?, tracker_tid = ?, torrent_hash = ?, "
                    "updated_at = ?, checked_at = ?, wedge_spent = ?, wedge_at = ? "
                    "WHERE asin = ? AND added_at = ? AND status = ?",
                    (status, note, intent["tid"], digest, now, now, bool(spent_at), spent_at,
                     asin, row["added_at"], WISHLIST_WANTED),
                )
            self.connection.execute("DELETE FROM tracker_handoffs WHERE asin = ?", (asin,))

    def wishlist_set_status(
        self,
        asin: str,
        new: str,
        *,
        expect: str | None = None,
        note: str | None = None,
        tracker_tid: str | None = None,
        torrent_hash: str | None = None,
        checked: bool = False,
    ) -> bool:
        """Move one row to ``new`` - only along WISHLIST_TRANSITIONS, and when ``expect`` is
        given only if the row is still in that state (a row removed or changed by a click
        while the watcher worked on it is left alone). True when the row moved."""
        if new not in WISHLIST_STATUSES:
            return False
        now = time.time()
        with self._lock, self._txn():
            row = self.connection.execute("SELECT status FROM wishlist WHERE asin = ?", (asin,)).fetchone()
            if row is None:
                return False
            current = row["status"] or WISHLIST_WANTED
            if expect is not None and current != expect:
                return False
            if not wishlist_transition_ok(current, new):
                return False
            sets = ["status = ?", "updated_at = ?"]
            params: list = [new, now]
            if new == WISHLIST_WANTED:
                sets.append("misses = 0")
            for column, value in (("note", note), ("tracker_tid", tracker_tid), ("torrent_hash", torrent_hash)):
                if value is not None:
                    sets.append(f"{column} = ?")
                    params.append(str(value))
            if checked:
                sets.append("checked_at = ?")
                params.append(now)
            self.connection.execute(f"UPDATE wishlist SET {', '.join(sets)} WHERE asin = ?", (*params, asin))
        return True

    def wedges_spent_since(self, when: float) -> int:
        """Freeleech credits the watcher has spent since ``when`` (epoch seconds), from the ledger
        (STATECFG-01: credit_spends, which outlives the wishlist rows)."""
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM credit_spends WHERE spent_at >= ?", (float(when),)
            ).fetchone()
        return int(row[0] or 0)

    def mark_wedge_spent(self, tid: str, at: float) -> None:
        """Record one credit spent on tracker torrent ``tid``: a ledger row, counted whatever happens
        to the wishlist row, plus the row's own marker (call after the row carries that tracker_tid)."""
        with self._lock, self._txn():
            self.connection.execute(
                "INSERT INTO credit_spends (tracker_tid, spent_at) VALUES (?, ?)", (str(tid), float(at))
            )
            self.connection.execute(
                "UPDATE wishlist SET wedge_spent = 1, wedge_at = ? WHERE tracker_tid = ?",
                (float(at), str(tid)),
            )

    def wishlist_snatch_hashes(self) -> list[str]:
        """Info hashes (lower case) of every torrent the watcher added."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT torrent_hash FROM wishlist WHERE torrent_hash IS NOT NULL AND torrent_hash != ''"
            ).fetchall()
        return [r[0].strip().lower() for r in rows if r[0].strip()]

    def wishlist_record_check(self, asin: str, note: str, miss: bool) -> int | None:
        """A search that changed nothing about a "wanted" row: store when it was checked and
        what was decided. ``miss`` counts a search that found nothing (any other outcome
        resets the count). Returns the consecutive-miss count, or None when the row is gone or
        no longer "wanted"."""
        now = time.time()
        with self._lock, self._txn():
            row = self.connection.execute("SELECT status, misses FROM wishlist WHERE asin = ?", (asin,)).fetchone()
            if row is None or (row["status"] or WISHLIST_WANTED) != WISHLIST_WANTED:
                return None
            misses = int(row["misses"] or 0) + 1 if miss else 0
            self.connection.execute(
                "UPDATE wishlist SET note = ?, checked_at = ?, updated_at = ?, misses = ? WHERE asin = ?",
                (str(note), now, now, misses, asin),
            )
        return misses

    def wishlist_set_search_title(self, asin: str, title: str) -> dict | None:
        """Set (or, when ``title`` is blank, clear) the title the watcher searches for. The new
        title is always saved; the display title is never touched.

        Only a row where a search would actually run - "wanted", "not_found" or "error"
        (WISHLIST_SEARCHABLE) - goes back to "wanted" with the note, misses and checked_at
        cleared, so the next cycle searches it at once. A "downloading", "downloaded" or
        "in_library" row keeps its status, note, misses and checked_at: the book is already past
        searching, and putting it back to "wanted" would make the watcher grab a second torrent
        (orphaning the first) or re-download a book the user owns. ``wishlist_requeue`` is the
        explicit way to search for such a row again. Returns the row, or None when it is gone."""
        title = " ".join(str(title or "").split())
        now = time.time()
        with self._lock, self._txn():
            row = self.connection.execute("SELECT status FROM wishlist WHERE asin = ?", (asin,)).fetchone()
            if row is None:
                return None
            if (row["status"] or WISHLIST_WANTED) in WISHLIST_SEARCHABLE:
                self.connection.execute(
                    "UPDATE wishlist SET search_title = ?, status = ?, note = '', misses = 0, checked_at = 0, "
                    "updated_at = ? WHERE asin = ?",
                    (title or None, WISHLIST_WANTED, now, asin),
                )
            else:
                self.connection.execute(
                    "UPDATE wishlist SET search_title = ?, updated_at = ? WHERE asin = ?",
                    (title or None, now, asin),
                )
        return self.wishlist_get(asin)

    def wishlist_requeue(self, asin: str) -> bool:
        """Re-queue: a "downloading", "downloaded" or "in_library" row goes back to "wanted" (and to
        the front of the watcher's queue), so the watcher searches for it again. This is a
        deliberate override of WISHLIST_TRANSITIONS, for the one explicit click that asks for it.
        The torrent already in qBittorrent is NOT removed or stopped, and the row's tracker_tid /
        torrent_hash stay until a new grab replaces them. True when it moved."""
        now = time.time()
        with self._lock, self._txn():
            row = self.connection.execute("SELECT status FROM wishlist WHERE asin = ?", (asin,)).fetchone()
            if row is None or row["status"] not in WISHLIST_REQUEUEABLE:
                return False
            self.connection.execute(
                "UPDATE wishlist SET status = ?, misses = 0, checked_at = 0, updated_at = ?, "
                "note = ? WHERE asin = ?",
                (WISHLIST_WANTED, now, f"re-queued ({time.strftime('%Y-%m-%d %H:%M', time.localtime(now))})", asin),
            )
        return True

    def wishlist_retry(self, asin: str) -> bool:
        """Retry: a "not_found" or "error" row goes back to "wanted" (and to the front of the
        watcher's queue). True when it moved."""
        now = time.time()
        with self._lock, self._txn():
            row = self.connection.execute("SELECT status FROM wishlist WHERE asin = ?", (asin,)).fetchone()
            if row is None or row["status"] not in WISHLIST_RETRYABLE:
                return False
            self.connection.execute(
                "UPDATE wishlist SET status = ?, misses = 0, checked_at = 0, updated_at = ?, "
                "note = ? WHERE asin = ?",
                (WISHLIST_WANTED, now, f"retry requested ({time.strftime('%Y-%m-%d %H:%M', time.localtime(now))})",
                 asin),
            )
        return True

    # ------------------------------------------------- books a Discover page showed
    def catalog_seen_put(self, products: list[dict]) -> None:
        """Remember catalog results (search, recommendations) by ASIN. This is what lets the
        cover proxy fetch their art - only for ASINs a page really showed - and what a "Get
        this" click takes its metadata from. Old rows age out unless they are wishlisted."""
        now = time.time()
        rows = [
            (str(p["asin"]), str(p.get("image") or ""), json.dumps(p), now)
            for p in products
            if p.get("asin")
        ]
        with self._lock, self._txn():
            self.connection.executemany(
                "INSERT INTO catalog_seen (asin, image, payload, seen_at) VALUES (?,?,?,?) "
                "ON CONFLICT(asin) DO UPDATE SET image=excluded.image, payload=excluded.payload, "
                "seen_at=excluded.seen_at",
                rows,
            )
            self.connection.execute(
                "DELETE FROM catalog_seen WHERE seen_at < ? AND asin NOT IN (SELECT asin FROM wishlist)",
                (now - CATALOG_SEEN_TTL_SECONDS,),
            )

    def catalog_seen(self, asin: str) -> dict | None:
        if not asin:
            return None
        with self._lock:
            row = self.connection.execute("SELECT payload FROM catalog_seen WHERE asin = ?", (asin,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def library_asins(self) -> set[str]:
        """Every ASIN a library book is matched or pinned to (two queries, one lock hold)."""
        found: set[str] = set()
        with self._lock:
            for sql in (
                "SELECT asin AS a FROM units WHERE asin != ''",
                "SELECT asin_override AS a FROM units WHERE asin_override != ''",
            ):
                found.update(r["a"] for r in self.connection.execute(sql).fetchall() if r["a"])
        return found

    def cover_keep_asins(self) -> set[str]:
        """Every ASIN whose cached cover must survive a prune: the library's (matched or pinned),
        and anything a Discover page, a series listing or the wishlist still refers to."""
        keep: set[str] = set()
        with self._lock:
            for sql in (
                "SELECT asin AS a FROM units WHERE asin != ''",
                "SELECT asin_override AS a FROM units WHERE asin_override != ''",
                "SELECT asin AS a FROM catalog_seen",
                "SELECT asin AS a FROM series_asins",
                "SELECT asin AS a FROM wishlist",
            ):
                keep.update(r["a"] for r in self.connection.execute(sql).fetchall() if r["a"])
        return keep

    def catalog_seen_image(self, asin: str) -> str | None:
        """The catalog image URL for an ASIN a Discover page showed; None when it showed none."""
        if not asin:
            return None
        with self._lock:
            row = self.connection.execute("SELECT image FROM catalog_seen WHERE asin = ?", (asin,)).fetchone()
        return (row["image"] or "") if row else None

    # ------------------------------------------------- stored Discover results (For you)
    def discover_cache_put(self, key: str, payload: dict) -> float:
        """Store one computed result with the time it was fetched; returns that time."""
        now = time.time()
        with self._lock:
            self.connection.execute(
                "INSERT INTO discover_cache (key, fetched_at, payload) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
                (key, now, json.dumps(payload or {})),
            )
        return now

    def discover_cache_get(self, key: str) -> dict | None:
        """{"fetched_at", "payload"} or None when nothing was stored yet."""
        with self._lock:
            row = self.connection.execute(
                "SELECT fetched_at, payload FROM discover_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        return {"fetched_at": float(row["fetched_at"] or 0), "payload": payload if isinstance(payload, dict) else {}}

    # ------------------------------------------------- the account
    def get_credentials(self) -> Credentials | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT username, password_hash, salt, algo, iterations, created_at, updated_at, version "
                "FROM credentials WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return Credentials(**{k: row[k] for k in row.keys()})

    def has_credentials(self) -> bool:
        with self._lock:
            return self.connection.execute("SELECT 1 FROM credentials WHERE id = 1").fetchone() is not None

    def credentials_version(self) -> int:
        """0 while there is no account."""
        with self._lock:
            row = self.connection.execute("SELECT version FROM credentials WHERE id = 1").fetchone()
        return int(row["version"]) if row else 0

    def create_credentials(self, username: str, hashed) -> int:
        """First setup: create THE account. Returns its version (1), or 0 when an account
        already exists - the single INSERT is what makes a racing second setup lose."""
        now = time.time()
        with self._lock:
            try:
                self.connection.execute(
                    "INSERT INTO credentials (id, username, password_hash, salt, algo, iterations, "
                    "created_at, updated_at, version) VALUES (1,?,?,?,?,?,?,?,1)",
                    (username, hashed.digest, hashed.salt, hashed.algo, int(hashed.iterations), now, now),
                )
            except sqlite3.IntegrityError:
                return 0
        return 1

    def set_credentials(self, username: str, hashed) -> int:
        """Create or replace the account (Settings with the legacy token, reset-password).
        Returns the new version, always above any version a session could hold."""
        now = time.time()
        with self._lock, self._txn():
            self.connection.execute(
                "INSERT INTO credentials (id, username, password_hash, salt, algo, iterations, "
                "created_at, updated_at, version) VALUES (1,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username, "
                "password_hash=excluded.password_hash, salt=excluded.salt, algo=excluded.algo, "
                "iterations=excluded.iterations, updated_at=excluded.updated_at, "
                "version=credentials.version + 1",
                (username, hashed.digest, hashed.salt, hashed.algo, int(hashed.iterations), now, now),
            )
            return int(self.connection.execute("SELECT version FROM credentials WHERE id = 1").fetchone()[0])

    def set_password(self, hashed) -> int:
        """A new password for the existing account. Returns the new version (0: no account)."""
        with self._lock, self._txn():
            cursor = self.connection.execute(
                "UPDATE credentials SET password_hash=?, salt=?, algo=?, iterations=?, updated_at=?, "
                "version = version + 1 WHERE id = 1",
                (hashed.digest, hashed.salt, hashed.algo, int(hashed.iterations), time.time()),
            )
            if cursor.rowcount == 0:
                return 0
            return int(self.connection.execute("SELECT version FROM credentials WHERE id = 1").fetchone()[0])

    def set_username(self, username: str) -> int:
        """Rename the existing account. Returns the new version (0: no account)."""
        with self._lock, self._txn():
            cursor = self.connection.execute(
                "UPDATE credentials SET username=?, updated_at=?, version = version + 1 WHERE id = 1",
                (username, time.time()),
            )
            if cursor.rowcount == 0:
                return 0
            return int(self.connection.execute("SELECT version FROM credentials WHERE id = 1").fetchone()[0])

    # ------------------------------------------------- sign-in sessions
    # Only a SHA-256 of the session id is stored, never the id (the cookie value) itself.
    def load_sessions(self, now: float) -> list[tuple[str, float, int]]:
        """Prune expired rows, then return (sid_hash, expires_at, version), oldest first."""
        with self._lock:
            self.connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            rows = self.connection.execute(
                "SELECT sid_hash, expires_at, version FROM sessions ORDER BY created_at, expires_at"
            ).fetchall()
        return [(r["sid_hash"], float(r["expires_at"]), int(r["version"])) for r in rows]

    def put_session(self, sid_hash: str, expires_at: float, version: int, now: float) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO sessions (sid_hash, expires_at, version, created_at) VALUES (?,?,?,?)",
                (sid_hash, float(expires_at), int(version), float(now)),
            )

    def delete_sessions(self, sid_hashes) -> None:
        with self._lock, self._txn():
            for h in sid_hashes:
                self.connection.execute("DELETE FROM sessions WHERE sid_hash = ?", (h,))

    def keep_only_session(self, sid_hash: str, version: int) -> None:
        """A credential change: every row goes except ``sid_hash``, which moves to ``version``
        (an empty hash keeps nothing)."""
        with self._lock, self._txn():
            self.connection.execute("DELETE FROM sessions WHERE sid_hash != ?", (sid_hash,))
            self.connection.execute(
                "UPDATE sessions SET version = ? WHERE sid_hash = ?", (int(version), sid_hash)
            )

    def clear_sessions(self) -> None:
        with self._lock:
            self.connection.execute("DELETE FROM sessions")

    def heartbeat(self) -> None:
        self.set_meta("heartbeat", time.time())

    def heartbeat_age(self) -> float:
        beat = self.get_meta("heartbeat")
        return float("inf") if not beat else time.time() - beat

    def close(self) -> None:
        self.connection.close()

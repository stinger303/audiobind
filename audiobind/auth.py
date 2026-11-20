"""Sign-in for the web UI and API: the single account (username + password) and the
legacy shared AUTH_TOKEN.

The account lives in the state DB as a salted PBKDF2-SHA256 hash - never the password.
Sessions are saved in the state DB (as a SHA-256 of the id, never the id itself), so a restart
or redeploy keeps people signed in. Each session is stamped with the
credential version it was created under; changing (or resetting) the credentials bumps
that version, which ends every other session - even after a reset from another process.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

log = logging.getLogger("audiobind")

COOKIE_NAME = "audiobind_session"
SESSION_TTL = 7 * 24 * 3600
#: "Keep me signed in": a per-session lifetime, chosen at the login form
REMEMBER_TTL = 30 * 24 * 3600
#: the remembered-username cookie. Prefills the login field; never an authenticator
USER_COOKIE_NAME = "ab_user"
USER_COOKIE_TTL = 30 * 24 * 3600
#: failed logins (or wrong Bearer tokens) allowed per source IP inside FAIL_WINDOW
FAIL_LIMIT = 5
FAIL_WINDOW = 10 * 60
#: failures from ALL sources together inside FAIL_WINDOW above which each further attempt is
#: logged as a warning (SEC-02: logged, never refused - see FailLimiter.attempt)
GLOBAL_FAIL_LIMIT = 30
#: hard caps so a flood of requests cannot grow these tables without bound
MAX_SESSIONS = 256
MAX_TRACKED_IPS = 4096

#: password hashing: stdlib PBKDF2-HMAC-SHA256, a fresh 16-byte random salt per hash
ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
#: account rules, shared by /setup, Settings and the reset-password CLI
USERNAME_MAX = 64
PASSWORD_MIN = 8
#: an upper bound so a megabyte "password" cannot be used to burn CPU
PASSWORD_MAX = 1024


def token_matches(given: str, expected: str) -> bool:
    """Constant-time comparison; an empty configured token never matches."""
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


# ---------------------------------------------------------------- passwords


@dataclass(frozen=True)
class PasswordHash:
    """What the credentials table stores. repr() hides the secret parts on purpose, so a
    stray log line or traceback can never print them."""

    algo: str
    iterations: int
    salt: str = field(repr=False)  # hex
    digest: str = field(repr=False)  # hex


#: at most this many PBKDF2 runs at once, so a burst of sign-in attempts cannot burn every core
PBKDF2_SLOTS = threading.BoundedSemaphore(2)


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    with PBKDF2_SLOTS:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "replace"), salt, iterations)


def hash_password(password: str, iterations: int | None = None) -> PasswordHash:
    """A new salted hash. Every call draws a fresh salt, so the same password never hashes
    the same way twice."""
    rounds = int(iterations or ITERATIONS)
    salt = secrets.token_bytes(SALT_BYTES)
    return PasswordHash(ALGO, rounds, salt.hex(), _pbkdf2(password, salt, rounds).hex())


def verify_password(password: str, stored) -> bool:
    """Constant-time check of ``password`` against a stored hash (a PasswordHash, or a
    credentials row with ``algo``/``iterations``/``salt``/``password_hash``)."""
    digest = getattr(stored, "digest", None) or getattr(stored, "password_hash", "")
    try:
        salt = bytes.fromhex(stored.salt)
        expected = bytes.fromhex(digest)
        rounds = int(stored.iterations)
    except (ValueError, TypeError, AttributeError):
        return False
    if stored.algo != ALGO or rounds < 1 or not salt or not expected:
        return False
    return hmac.compare_digest(_pbkdf2(password, salt, rounds), expected)


def check_login(creds, username: str, password: str) -> bool:
    """Username and password together, with the same work whichever one is wrong.

    The PBKDF2 always runs against the stored hash (the dominant cost), the usernames are
    compared as fixed-length digests, and the results are combined without short-circuit,
    so neither the answer nor the timing says which field failed.
    """
    user_ok = hmac.compare_digest(
        hashlib.sha256(username.encode("utf-8", "replace")).digest(),
        hashlib.sha256(str(creds.username).encode("utf-8", "replace")).digest(),
    )
    password_ok = verify_password(password, creds)
    return bool(user_ok) & bool(password_ok) & bool(username) & bool(password)


def username_problem(username: str) -> str:
    """'' when the (already stripped) username is acceptable, else what is wrong with it."""
    if not username:
        return "Enter a username."
    if len(username) > USERNAME_MAX:
        return f"The username can be at most {USERNAME_MAX} characters."
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in username):
        return "The username cannot contain control characters."
    return ""


def login_secret(given: str) -> str:
    """SEC-06: the one canonical form of a password (or legacy token) as typed in the browser
    or sent as Bearer: outer whitespace dropped. An HTTP header cannot carry it, so the
    browser must not count it either - and password_problem() refuses it in a new password."""
    return given.strip()


def password_problem(password: str, confirm: str | None = None) -> str:
    """'' when the new password is acceptable, else what is wrong with it."""
    if password != login_secret(password):
        return "The password cannot start or end with a space."
    if len(password) < PASSWORD_MIN:
        return f"The password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password can be at most {PASSWORD_MAX} characters."
    if confirm is not None and not hmac.compare_digest(
        password.encode("utf-8", "replace"), confirm.encode("utf-8", "replace")
    ):
        return "The two passwords do not match."
    return ""


# ---------------------------------------------------------------- first-run setup code

#: SEC-01: /setup needs this one-time code, so the first peer to reach a fresh install cannot
#: claim it. Kept in the state DB's meta table until the account is created.
SETUP_CODE_KEY = "setup_code"
SETUP_CODE_ENV = "AUDIOBIND_SETUP_CODE"
SETUP_CODE_LENGTH = 8
#: no 0/O or 1/I: the code is read off a log line and typed by hand
SETUP_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def setup_code(state) -> str:
    """The code /setup asks for: AUDIOBIND_SETUP_CODE when set (never stored), else the one
    saved in the state DB, made on first use."""
    forced = os.environ.get(SETUP_CODE_ENV, "").strip()
    if forced:
        return forced
    code = state.get_meta(SETUP_CODE_KEY) or ""
    if not code:
        code = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
        state.set_meta(SETUP_CODE_KEY, code)
    return code


def setup_code_matches(state, given: str) -> bool:
    """Constant-time; case and surrounding spaces do not matter (it is typed off a log line)."""
    given = (given or "").strip().upper()
    return bool(given) and hmac.compare_digest(
        given.encode("utf-8", "replace"), setup_code(state).upper().encode("utf-8", "replace")
    )


def consume_setup_code(state) -> None:
    """The account exists: the stored code is spent."""
    state.set_meta(SETUP_CODE_KEY, "")


def announce_setup_code(state) -> None:
    """At start, while setup is pending: put the code in the log, where only someone with
    access to the container can read it. An AUDIOBIND_SETUP_CODE is not repeated."""
    if os.environ.get(SETUP_CODE_ENV, "").strip():
        log.warning("setup code: set by %s  (required to create the first account)", SETUP_CODE_ENV)
        return
    log.warning("setup code: %s  (required to create the first account)", setup_code(state))


class BearerCache:
    """Remembers recently accepted API passwords, so a polling script does not pay (and make
    the server pay) a 600k-round PBKDF2 on every request.

    Entries are HMACs under a key that exists only in this process's memory, tied to the
    credential version, so a password change makes every entry useless.
    """

    def __init__(self, size: int = 16):
        self.size = size
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._seen: dict[bytes, None] = {}

    def _tag(self, given: str, version: int) -> bytes:
        return hmac.new(self._key, f"{version}\0{given}".encode("utf-8", "replace"), "sha256").digest()

    def hit(self, given: str, version: int) -> bool:
        tag = self._tag(given, version)
        with self._lock:
            return any(hmac.compare_digest(tag, known) for known in self._seen)

    def add(self, given: str, version: int) -> None:
        tag = self._tag(given, version)
        with self._lock:
            self._seen[tag] = None
            while len(self._seen) > self.size:
                self._seen.pop(next(iter(self._seen)))

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


# ---------------------------------------------------------------- sessions


class Sessions:
    """Random session ids -> (expiry time, credential version).

    A session is only valid under the credential version it was created with: bumping the
    version (a password or username change, or a CLI reset) ends it.

    With a ``store`` attached (the State DB) sessions survive a restart. Only a SHA-256 of
    each id is kept - in memory and on disk - so a copy of the database holds nothing that
    can be replayed as a cookie.
    """

    def __init__(self, ttl: float = SESSION_TTL, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}  # insertion order = age, oldest first
        self._version: dict[str, int] = {}
        self._store = None

    @staticmethod
    def _key(sid: str) -> str:
        return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()

    def attach(self, store) -> None:
        """Back the sessions with ``store`` and load what it holds (expired rows are pruned)."""
        now = self.clock()
        with self._lock:
            self._store = store
            self._expiry.clear()
            self._version.clear()
            try:
                rows = store.load_sessions(now)
            except sqlite3.Error:
                log.warning("could not load saved sessions", exc_info=True)
                rows = []
            for key, expiry, version in rows[-MAX_SESSIONS:]:
                self._expiry[key] = expiry
                self._version[key] = version

    def detach(self) -> None:
        """Stop saving (the store is going away); sessions stay in memory."""
        with self._lock:
            self._store = None

    def _persist(self, action: Callable[[], None]) -> None:
        if self._store is None:
            return
        try:
            action()
        except sqlite3.Error:
            log.warning("could not save sessions", exc_info=True)

    def create(self, version: int = 0, ttl: float | None = None) -> str:
        sid = secrets.token_urlsafe(32)
        key = self._key(sid)
        now = self.clock()
        lifetime = self.ttl if ttl is None else float(ttl)
        with self._lock:
            self._prune(now)
            while len(self._expiry) >= MAX_SESSIONS:  # drop the oldest
                self._drop(next(iter(self._expiry)))
            self._expiry[key] = now + lifetime
            self._version[key] = int(version)
            if self._store is not None:
                self._persist(lambda: self._store.put_session(key, now + lifetime, int(version), now))
        return sid

    def valid(self, sid: str, version: int | None = None) -> bool:
        """True for a live session; with ``version``, only one created under that version."""
        if not sid:
            return False
        key = self._key(sid)
        now = self.clock()
        with self._lock:
            expiry = self._expiry.get(key)
            if expiry is None:
                return False
            if expiry <= now:
                self._prune(now)
                return False
            if version is not None and self._version.get(key, 0) != int(version):
                self._drop(key)  # from before a credential change: gone for good
                return False
            return True

    def bump(self, version: int, keep: str = "") -> None:
        """The credentials changed: every session ends except ``keep`` (the browser that made
        the change), which carries on under the new version."""
        now = self.clock()
        with self._lock:
            keep_key = self._key(keep) if keep else ""
            kept = keep_key if keep_key and self._expiry.get(keep_key, 0) > now else ""
            for key in [k for k in self._expiry if k != kept]:
                self._expiry.pop(key, None)
                self._version.pop(key, None)
            if kept:
                self._version[kept] = int(version)
            if self._store is not None:
                self._persist(lambda: self._store.keep_only_session(kept, int(version)))

    def revoke(self, sid: str) -> None:
        if not sid:
            return
        with self._lock:
            self._drop(self._key(sid))

    def clear(self) -> None:
        with self._lock:
            self._expiry.clear()
            self._version.clear()
            if self._store is not None:
                self._persist(self._store.clear_sessions)

    def _drop(self, key: str) -> None:
        self._expiry.pop(key, None)
        self._version.pop(key, None)
        if self._store is not None:
            self._persist(lambda: self._store.delete_sessions([key]))

    def __len__(self) -> int:
        with self._lock:
            return len(self._expiry)

    def _prune(self, now: float) -> None:
        dead = [k for k, exp in self._expiry.items() if exp <= now]
        for key in dead:
            self._expiry.pop(key, None)
            self._version.pop(key, None)
        if dead and self._store is not None:
            self._persist(lambda: self._store.delete_sessions(dead))


def limiter_key(ip: str) -> str:
    """The key failures are counted under: an IPv4 address as is, an IPv6 address by its /64
    (one host is usually given a whole /64, so per-address counting is no limit at all), an
    IPv4-mapped IPv6 address as its IPv4 address. Anything unparseable is used unchanged."""
    try:
        addr = ipaddress.ip_address((ip or "").strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


class FailLimiter:
    """At most ``limit`` failures per key inside a sliding ``window``; past ``global_limit``
    from every key together, attempts are logged. Keys are normalised by ``limiter_key``
    (IPv6 counted per /64)."""

    def __init__(
        self,
        limit: int = FAIL_LIMIT,
        window: float = FAIL_WINDOW,
        clock: Callable[[], float] = time.time,
        global_limit: int = GLOBAL_FAIL_LIMIT,
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.global_limit = global_limit
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        #: (time, key) for every counted failure, any key: the global budget
        self._all: list[tuple[float, str]] = []

    def _global_recent(self, now: float) -> list[tuple[float, str]]:
        self._all = [(t, k) for t, k in self._all if now - t < self.window]
        return self._all

    def _global_blocked(self, now: float) -> bool:
        return self.global_limit > 0 and len(self._global_recent(now)) >= self.global_limit

    def _recent(self, key: str, now: float) -> list[float]:
        stamps = [t for t in self._fails.get(key, []) if now - t < self.window]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps

    def blocked(self, key: str) -> bool:
        key = limiter_key(key)
        now = self.clock()
        with self._lock:
            return len(self._recent(key, now)) >= self.limit

    def attempt(self, key: str) -> bool:
        """Check the budget and count this attempt in one step, BEFORE the password is
        verified. False = locked out (nothing is counted). On success call ``reset``.
        Because the count is taken up front, N concurrent guesses are N failures.

        SEC-02: only the key's own limit refuses. Over the global budget the attempt is
        logged, not refused - otherwise junk from unrelated sources locks the owner out. (A
        pacing sleep here would hold one of the server's capped connection slots per attempt,
        which a flood could use to starve the UI, so there is none.)"""
        key = limiter_key(key)
        now = self.clock()
        with self._lock:
            if len(self._recent(key, now)) >= self.limit:
                return False
            if self._global_blocked(now):
                log.warning(
                    "sign-in failures from all sources are over the global budget (%d in %d min); "
                    "still checking %s, which is under its own limit",
                    len(self._all), int(self.window // 60), key,
                )
            self._record_locked(key, now)
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest counted failure ages out (0 when not blocked)."""
        key = limiter_key(key)
        now = self.clock()
        with self._lock:
            waits = [0]
            stamps = self._recent(key, now)
            if len(stamps) >= self.limit:
                waits.append(max(1, int(self.window - (now - stamps[0])) + 1))
            return max(waits)

    def _record_locked(self, key: str, now: float) -> None:
        if key not in self._fails and len(self._fails) >= MAX_TRACKED_IPS:
            for k in list(self._fails):
                self._recent(k, now)
            if len(self._fails) >= MAX_TRACKED_IPS:
                self._fails.pop(next(iter(self._fails)))
        self._fails.setdefault(key, []).append(now)
        self._all.append((now, key))
        if len(self._all) > MAX_TRACKED_IPS:  # record() is uncapped by the budget: bound the memory
            del self._all[: len(self._all) - MAX_TRACKED_IPS]

    def record(self, key: str) -> None:
        key = limiter_key(key)
        now = self.clock()
        with self._lock:
            self._record_locked(key, now)

    def reset(self, key: str) -> None:
        """A successful sign-in: this key's failures (and its share of the global budget) go."""
        key = limiter_key(key)
        with self._lock:
            self._fails.pop(key, None)
            self._all = [(t, k) for t, k in self._all if k != key]

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()
            self._all.clear()


SESSIONS = Sessions()
LIMITER = FailLimiter()
BEARERS = BearerCache()


def cookie_value(header: str | None, name: str = COOKIE_NAME) -> str:
    """One cookie's value from a Cookie header ('' when absent). Tolerant of junk."""
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"')
    return ""


def legacy_version(token: str) -> int:
    """The "credential version" a session carries while no account exists and the legacy
    AUTH_TOKEN signs people in: a number derived from a hash of the token, so changing the
    token (restart included) ends every session made under the old one. 0 without a token."""
    if not token:
        return 0
    digest = hashlib.sha256(b"audiobind-legacy-token\0" + token.encode("utf-8", "replace")).digest()
    return int.from_bytes(digest[:6], "big") | 1 << 47  # positive, never a small account version


def session_cookie(sid: str, ttl: int | float = SESSION_TTL, secure: bool = False) -> str:
    return f"{COOKIE_NAME}={sid}; Path=/; Max-Age={int(ttl)}; HttpOnly; SameSite=Strict" + (
        "; Secure" if secure else "")


def expired_cookie(secure: bool = False) -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")


def username_cookie(username: str, secure: bool = False) -> str:
    """The remembered username: only ever read back to prefill the login field."""
    value = quote(username, safe="")
    return f"{USER_COOKIE_NAME}={value}; Path=/; Max-Age={USER_COOKIE_TTL}; HttpOnly; SameSite=Strict" + (
        "; Secure" if secure else "")


def remembered_username(header: str | None) -> str:
    return unquote(cookie_value(header, USER_COOKIE_NAME))[:USERNAME_MAX]

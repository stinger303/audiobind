"""Sign-in for the web UI and API: the single account (username + password) and the
legacy shared AUTH_TOKEN.

The account lives in the state DB as a salted PBKDF2-SHA256 hash - never the password.
Sessions live in memory: a restart signs everyone out, which is fine for a single-user LAN
app and means no session secret ever touches the disk. Each session is stamped with the
credential version it was created under; changing (or resetting) the credentials bumps
that version, which ends every other session - even after a reset from another process.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

COOKIE_NAME = "audiobind_session"
SESSION_TTL = 7 * 24 * 3600
#: failed logins (or wrong Bearer tokens) allowed per source IP inside FAIL_WINDOW
FAIL_LIMIT = 5
FAIL_WINDOW = 10 * 60
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


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
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


def password_problem(password: str, confirm: str | None = None) -> str:
    """'' when the new password is acceptable, else what is wrong with it."""
    if len(password) < PASSWORD_MIN:
        return f"The password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password can be at most {PASSWORD_MAX} characters."
    if confirm is not None and not hmac.compare_digest(
        password.encode("utf-8", "replace"), confirm.encode("utf-8", "replace")
    ):
        return "The two passwords do not match."
    return ""


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
    """

    def __init__(self, ttl: float = SESSION_TTL, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}
        self._version: dict[str, int] = {}

    def create(self, version: int = 0) -> str:
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._prune(now)
            if len(self._expiry) >= MAX_SESSIONS:  # drop the oldest
                oldest = min(self._expiry, key=self._expiry.__getitem__)
                self._drop(oldest)
            self._expiry[sid] = now + self.ttl
            self._version[sid] = int(version)
        return sid

    def valid(self, sid: str, version: int | None = None) -> bool:
        """True for a live session; with ``version``, only one created under that version."""
        if not sid:
            return False
        now = self.clock()
        with self._lock:
            expiry = self._expiry.get(sid)
            if expiry is None:
                return False
            if expiry <= now:
                self._drop(sid)
                return False
            if version is not None and self._version.get(sid, 0) != int(version):
                self._drop(sid)  # from before a credential change: gone for good
                return False
            return True

    def bump(self, version: int, keep: str = "") -> None:
        """The credentials changed: every session ends except ``keep`` (the browser that made
        the change), which carries on under the new version."""
        now = self.clock()
        with self._lock:
            kept = keep if keep and self._expiry.get(keep, 0) > now else ""
            for sid in [s for s in self._expiry if s != kept]:
                self._drop(sid)
            if kept:
                self._version[kept] = int(version)

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._drop(sid)

    def clear(self) -> None:
        with self._lock:
            self._expiry.clear()
            self._version.clear()

    def _drop(self, sid: str) -> None:
        self._expiry.pop(sid, None)
        self._version.pop(sid, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._expiry)

    def _prune(self, now: float) -> None:
        for sid in [s for s, exp in self._expiry.items() if exp <= now]:
            self._drop(sid)


class FailLimiter:
    """At most ``limit`` failures per key inside a sliding ``window``."""

    def __init__(
        self, limit: int = FAIL_LIMIT, window: float = FAIL_WINDOW, clock: Callable[[], float] = time.time
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        stamps = [t for t in self._fails.get(key, []) if now - t < self.window]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, self.clock())) >= self.limit

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest counted failure ages out (0 when not blocked)."""
        now = self.clock()
        with self._lock:
            stamps = self._recent(key, now)
            if len(stamps) < self.limit:
                return 0
            return max(1, int(self.window - (now - stamps[0])) + 1)

    def record(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            if key not in self._fails and len(self._fails) >= MAX_TRACKED_IPS:
                for k in list(self._fails):
                    self._recent(k, now)
                if len(self._fails) >= MAX_TRACKED_IPS:
                    self._fails.pop(next(iter(self._fails)))
            self._fails.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()


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


def session_cookie(sid: str) -> str:
    return f"{COOKIE_NAME}={sid}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict"


def expired_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"

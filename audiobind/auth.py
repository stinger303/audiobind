"""Optional shared-token login for the web UI and API (AUTH_TOKEN).

Everything lives in memory: a restart signs everyone out, which is fine for a single-user
LAN app and means no session secret ever touches the disk.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable

COOKIE_NAME = "audiobind_session"
SESSION_TTL = 7 * 24 * 3600
#: failed logins (or wrong Bearer tokens) allowed per source IP inside FAIL_WINDOW
FAIL_LIMIT = 5
FAIL_WINDOW = 10 * 60
#: hard caps so a flood of requests cannot grow these tables without bound
MAX_SESSIONS = 256
MAX_TRACKED_IPS = 4096


def token_matches(given: str, expected: str) -> bool:
    """Constant-time comparison; an empty configured token never matches."""
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


class Sessions:
    """Random session ids -> expiry time."""

    def __init__(self, ttl: float = SESSION_TTL, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._prune(now)
            if len(self._expiry) >= MAX_SESSIONS:  # drop the oldest
                oldest = min(self._expiry, key=self._expiry.__getitem__)
                del self._expiry[oldest]
            self._expiry[sid] = now + self.ttl
        return sid

    def valid(self, sid: str) -> bool:
        if not sid:
            return False
        now = self.clock()
        with self._lock:
            expiry = self._expiry.get(sid)
            if expiry is None:
                return False
            if expiry <= now:
                del self._expiry[sid]
                return False
            return True

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._expiry.pop(sid, None)

    def clear(self) -> None:
        with self._lock:
            self._expiry.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._expiry)

    def _prune(self, now: float) -> None:
        for sid in [s for s, exp in self._expiry.items() if exp <= now]:
            del self._expiry[sid]


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

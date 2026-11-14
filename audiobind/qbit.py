"""qBittorrent WebUI: hand a .torrent to the download client (never write files ourselves).

Three calls, all POSTs to the WebUI API: ``/api/v2/auth/login``, ``/api/v2/torrents/add``
and ``/api/v2/auth/logout``. Quirks of the 5.x build this is written against:

* login answers 204 with an EMPTY body on success (older builds: 200 "Ok."); the proof is
  the session cookie in the jar, so that is what is checked. Older builds name it ``SID``;
  5.x names it after the WebUI port (``QBT_SID_8083``), so either is accepted;
* every POST needs a ``Referer`` matching the WebUI origin, or CSRF protection refuses it;
* ``paused`` was renamed ``stopped`` in 5.0 - both are sent as false.

The torrent goes in category ``mam-audiobooks`` (its save path is the ingest folder), with
no automatic-torrent-management override.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

CATEGORY = "mam-audiobooks"
TIMEOUT = 20.0
MAX_ANSWER = 64 * 1024


class QbitError(Exception):
    """qBittorrent refused or could not be reached. The message never carries the password."""


# ---------------------------------------------------------------------------- the info hash
def _skip(data: bytes, i: int) -> int:
    """Index just past the bencoded value that starts at ``i``."""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            if not data[i:i + 1]:
                raise ValueError("truncated")
            i = _skip(data, i)
        return i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        end = colon + 1 + int(data[i:colon])
        if end > len(data):
            raise ValueError("truncated")
        return end
    raise ValueError("not bencode")


def info_hash(torrent: bytes) -> str:
    """The v1 info hash (SHA-1 of the bencoded info dictionary), or "" when unreadable."""
    try:
        if torrent[:1] != b"d":
            return ""
        i = 1
        while torrent[i:i + 1] != b"e":
            key_end = _skip(torrent, i)
            colon = torrent.index(b":", i)
            key = torrent[colon + 1:key_end]
            value_end = _skip(torrent, key_end)
            if key == b"info":
                return hashlib.sha1(torrent[key_end:value_end]).hexdigest()  # noqa: S324 - BitTorrent v1
            i = value_end
    except (ValueError, IndexError, RecursionError):
        return ""
    return ""


# ---------------------------------------------------------------------------- multipart
def multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """(body, content type) for a multipart/form-data POST."""
    boundary = "----audiobind" + secrets.token_hex(12)
    out = bytearray()
    for name, value in fields:
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode() + b"\r\n"
    for name, filename, content, ctype in files:
        safe = "".join(ch for ch in filename if ch.isalnum() or ch in "._-") or "upload.torrent"
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------- the client
class QbitClient:
    """One WebUI session: login(), add_torrent(), logout(). Cookies live in this object's own
    jar only and die with it."""

    def __init__(self, url: str, username: str, password: str):
        self.url = str(url or "").rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise QbitError(f"QBIT_URL is not an http(s) URL: {self.url!r}")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self._username = str(username or "")
        self._password = str(password or "")
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False

    def __repr__(self) -> str:  # never the password
        return f"QbitClient({self.url!r}, logged_in={self.logged_in})"

    def _post(self, path: str, body: bytes, content_type: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.url + path,
            data=body,
            method="POST",
            headers={"Content-Type": content_type, "Referer": self.origin, "Origin": self.origin},
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                return response.status, response.read(MAX_ANSWER)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(MAX_ANSWER)
            finally:
                exc.close()
        except (urllib.error.URLError, OSError) as exc:
            raise QbitError(f"qBittorrent at {self.url} could not be reached ({type(exc).__name__})") from None

    def _has_sid(self) -> bool:
        """A non-empty session cookie: ``SID`` (older builds) or ``QBT_SID_<port>`` (5.x)."""
        return any(
            (cookie.name == "SID" or cookie.name.startswith("QBT_SID_")) and cookie.value
            for cookie in self.jar
        )

    def login(self) -> None:
        if not self._username or not self._password:
            raise QbitError("QBIT_USER / QBIT_PASS are not set")
        form = urllib.parse.urlencode({"username": self._username, "password": self._password}).encode()
        status, body = self._post("/api/v2/auth/login", form, "application/x-www-form-urlencoded")
        if status == 403:
            raise QbitError("qBittorrent refused the login (403: too many failed attempts, or a Referer mismatch)")
        if status not in (200, 204) or body.strip() == b"Fails." or not self._has_sid():
            raise QbitError(f"qBittorrent login failed (HTTP {status}) - check QBIT_USER / QBIT_PASS")
        self.logged_in = True

    def add_torrent(self, torrent: bytes, filename: str = "mam.torrent", category: str = CATEGORY) -> str:
        """Hand one .torrent over. Returns its info hash (from the answer when qBittorrent sends
        one, else computed from the file), "" if neither is available."""
        if not self.logged_in:
            self.login()
        body, ctype = multipart(
            [("category", category), ("paused", "false"), ("stopped", "false")],
            [("torrents", filename, torrent, "application/x-bittorrent")],
        )
        status, answer = self._post("/api/v2/torrents/add", body, ctype)
        if status == 403:  # the session expired between login and add: one fresh login, one retry
            self.logged_in = False
            self.login()
            status, answer = self._post("/api/v2/torrents/add", body, ctype)
        text = answer.strip()
        if status == 415:
            raise QbitError("qBittorrent says the torrent file is not valid")
        if status != 200 or text == b"Fails.":
            raise QbitError(f"qBittorrent refused the torrent (HTTP {status}"
                            f"{', ' + text[:80].decode('utf-8', 'replace') if text else ''}) - already added?")
        return _answer_hash(text) or info_hash(torrent)

    def logout(self) -> None:
        """Best effort: end the WebUI session and forget the cookie."""
        if self.logged_in:
            try:
                self._post("/api/v2/auth/logout", b"", "application/x-www-form-urlencoded")
            except QbitError:
                pass
        self.logged_in = False
        self.jar.clear()


def _answer_hash(text: bytes) -> str:
    """Newer WebAPIs may answer JSON naming the added torrent(s); older ones say "Ok."."""
    if not text.startswith((b"{", b"[")):
        return ""
    try:
        data = json.loads(text.decode("utf-8", "replace"))
    except ValueError:
        return ""
    candidates = []
    if isinstance(data, dict):
        for key in ("added_torrent_ids", "hashes", "hash", "infohash_v1"):
            value = data.get(key)
            candidates.extend(value if isinstance(value, list) else [value])
    for value in candidates:
        if isinstance(value, str) and len(value) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in value):
            return value.lower()
    return ""

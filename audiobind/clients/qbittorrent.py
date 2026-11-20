"""qBittorrent WebUI: hand a .torrent to the download client (never write files ourselves).

POSTs to the WebUI API: ``/api/v2/auth/login``, ``/api/v2/torrents/add`` and
``/api/v2/auth/logout``; GETs for ``/api/v2/torrents/info`` and, for the Settings page's
connection test only, ``/api/v2/app/version`` and ``/api/v2/torrents/categories`` (``check()``:
read-only, one login, no add). Quirks of the 5.x build this is written against:

* login answers 204 with an EMPTY body on success (older builds: 200 "Ok."); the proof is
  the session cookie in the jar, so that is what is checked. Older builds name it ``SID``;
  5.x names it after the WebUI port (``QBT_SID_8083``), so either is accepted;
* every POST needs a ``Referer`` matching the WebUI origin, or CSRF protection refuses it;
* ``paused`` was renamed ``stopped`` in 5.0 - both are sent as false.

The torrent goes in the category the caller names (``QBIT_CATEGORY``; its save path is the
ingest folder), with no automatic-torrent-management override.

Moved here from ``audiobind/qbit.py`` (which re-exports it) behind ``clients.base.DownloadClient``
with no behavioural change: same requests, same answers, same messages. The only additions are
the base classes and a ``kind`` on each error.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from .base import AUTH, ERROR, REFUSED, UNAVAILABLE, ClientError, DownloadClient
from .torrentfile import _skip, info_hash  # noqa: F401 - re-exported through audiobind.qbit

TIMEOUT = 20.0
MAX_ANSWER = 64 * 1024


class QbitError(ClientError):
    """qBittorrent refused or could not be reached. The message never carries the password."""


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
class QbitClient(DownloadClient):
    """One WebUI session: login(), add_torrent(), logout(). Cookies live in this object's own
    jar only and die with it."""

    name = "qBittorrent"

    def __init__(self, url: str, username: str, password: str):
        self.url = str(url or "").rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise QbitError(f"QBIT_URL is not an http(s) URL: {self.url!r}", kind=ERROR)
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
            raise QbitError(f"qBittorrent at {self.url} could not be reached ({type(exc).__name__})",
                            kind=UNAVAILABLE) from None

    def _get(self, path: str, query: dict) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.url + path + "?" + urllib.parse.urlencode(query),
            method="GET",
            headers={"Referer": self.origin, "Origin": self.origin},
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
            raise QbitError(f"qBittorrent at {self.url} could not be reached ({type(exc).__name__})",
                            kind=UNAVAILABLE) from None

    def _has_sid(self) -> bool:
        """A non-empty session cookie: ``SID`` (older builds) or ``QBT_SID_<port>`` (5.x)."""
        return any(
            (cookie.name == "SID" or cookie.name.startswith("QBT_SID_")) and cookie.value
            for cookie in self.jar
        )

    def login(self) -> None:
        if not self._username or not self._password:
            raise QbitError("QBIT_USER / QBIT_PASS are not set", kind=AUTH)
        form = urllib.parse.urlencode({"username": self._username, "password": self._password}).encode()
        status, body = self._post("/api/v2/auth/login", form, "application/x-www-form-urlencoded")
        if status == 403:
            raise QbitError("qBittorrent refused the login (403: too many failed attempts, or a Referer mismatch)",
                            kind=AUTH)
        if status not in (200, 204) or body.strip() == b"Fails." or not self._has_sid():
            raise QbitError(f"qBittorrent login failed (HTTP {status}) - check QBIT_USER / QBIT_PASS", kind=AUTH)
        self.logged_in = True

    def add_torrent(self, torrent: bytes, filename: str = "tracker.torrent", category: str = "audiobooks") -> str:
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
            raise QbitError("qBittorrent says the torrent file is not valid", kind=REFUSED)
        if status != 200 or text == b"Fails.":
            raise QbitError(f"qBittorrent refused the torrent (HTTP {status}"
                            f"{', ' + text[:80].decode('utf-8', 'replace') if text else ''}) - already added?",
                            kind=REFUSED)
        return _answer_hash(text) or info_hash(torrent)

    def _info(self, query: dict) -> list:
        """``/api/v2/torrents/info`` for ``query``: the decoded JSON list (one request, one re-login on 403)."""
        if not self.logged_in:
            self.login()
        status, answer = self._get("/api/v2/torrents/info", query)
        if status == 403:  # the session expired: one fresh login, one retry
            self.logged_in = False
            self.login()
            status, answer = self._get("/api/v2/torrents/info", query)
        if status != 200:
            raise QbitError(f"qBittorrent could not list torrents (HTTP {status})", kind=ERROR)
        try:
            data = json.loads(answer.decode("utf-8", "replace"))
        except ValueError:
            raise QbitError("qBittorrent answered the torrent list with something unreadable", kind=ERROR) from None
        return data if isinstance(data, list) else []

    def incomplete(self, category: str, done: float) -> int:
        """Read-only: how many torrents in ``category`` are below progress ``done``, in ONE request."""
        count = 0
        for item in self._info({"category": category}):
            if not isinstance(item, dict):
                continue
            try:
                if float(item.get("progress")) < done:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def snatch_state(self, category: str) -> dict[str, tuple[float, float]]:
        """Read-only: ``{info hash: (progress, ratio)}`` for every torrent in ``category``, in ONE request."""
        out: dict[str, tuple[float, float]] = {}
        for item in self._info({"category": category}):
            if not isinstance(item, dict):
                continue
            digest = str(item.get("hash") or "").lower()
            try:
                out[digest] = (float(item.get("progress")), float(item.get("ratio")))
            except (TypeError, ValueError):
                continue
        return out

    def progress(self, hashes) -> dict[str, float]:
        """Read-only: ``{info hash: progress 0..1}`` for the given hashes, in ONE request. A
        torrent qBittorrent no longer has is simply absent from the result."""
        wanted = [str(h).lower() for h in hashes if h]
        if not wanted:
            return {}
        data = self._info({"hashes": "|".join(wanted)})
        out: dict[str, float] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            digest = str(item.get("hash") or "").lower()
            try:
                value = float(item.get("progress"))
            except (TypeError, ValueError):
                continue
            if digest in wanted:
                out[digest] = value
        return out

    def version(self) -> str:
        """The connection test: log in, ``GET /api/v2/app/version`` (e.g. "v5.0.4" -> "5.0.4"),
        log out. Raises QbitError with the same plain messages as every other call."""
        self.login()
        try:
            status, answer = self._get("/api/v2/app/version", {})
        finally:
            self.logout()
        return _version_text(status, answer)

    def check(self, category: str) -> dict:
        """The Settings page's fuller connection test. ONE login, then GETs only - nothing is added,
        changed or deleted: the version, the categories (``{name: save path}``) and, when ``category``
        exists, the torrents in it (name, save path, content path, progress).

        Raises QbitError exactly like ``version()`` when the login or the version check fails. A
        failure of the later reads does not lose the version: it is reported as ``categories_error``
        / ``torrents_error`` and the matching value stays None."""
        self.login()
        out: dict = {"version": "", "categories": None, "categories_error": "", "torrents": None, "torrents_error": ""}
        try:
            status, answer = self._get("/api/v2/app/version", {})
            out["version"] = _version_text(status, answer)
            try:
                out["categories"] = _category_paths(self._read_json("/api/v2/torrents/categories", {}, "categories"))
            except QbitError as exc:
                out["categories_error"] = str(exc)
            if out["categories"] is not None and category in out["categories"]:
                try:
                    out["torrents"] = _torrent_places(
                        self._read_json("/api/v2/torrents/info", {"category": category}, "torrents"))
                except QbitError as exc:
                    out["torrents_error"] = str(exc)
        finally:
            self.logout()
        return out

    def _read_json(self, path: str, query: dict, what: str):
        """One GET inside an open session, decoded; QbitError with a plain message if it is not JSON."""
        status, answer = self._get(path, query)
        if status != 200:
            raise QbitError(f"qBittorrent could not list its {what} (HTTP {status})", kind=ERROR)
        try:
            return json.loads(answer.decode("utf-8", "replace"))
        except ValueError:
            raise QbitError(f"qBittorrent answered its {what} list with something unreadable", kind=ERROR) from None

    def logout(self) -> None:
        """Best effort: end the WebUI session and forget the cookie."""
        if self.logged_in:
            try:
                self._post("/api/v2/auth/logout", b"", "application/x-www-form-urlencoded")
            except QbitError:
                pass
        self.logged_in = False
        self.jar.clear()


def _version_text(status: int, answer: bytes) -> str:
    """"v5.0.4" -> "5.0.4"; QbitError when the answer is not a version."""
    text = answer.decode("utf-8", "replace").strip()
    if status != 200 or not text or len(text) > 40:
        raise QbitError(f"qBittorrent answered HTTP {status} to a version check", kind=ERROR)
    return text[1:] if text[:1] in ("v", "V") else text


def _category_paths(data) -> dict[str, str]:
    """``/api/v2/torrents/categories`` -> ``{category: save path}`` ("" = qBittorrent's default folder)."""
    if not isinstance(data, dict):
        raise QbitError("qBittorrent answered its category list with something unexpected", kind=ERROR)
    out: dict[str, str] = {}
    for name, entry in data.items():
        path = entry.get("savePath") if isinstance(entry, dict) else ""
        out[str(name)] = path.strip() if isinstance(path, str) else ""
    return out


def _torrent_places(data) -> list[dict]:
    """The few fields of ``/api/v2/torrents/info`` the folder check reads, defensively."""
    if not isinstance(data, list):
        raise QbitError("qBittorrent answered its torrent list with something unexpected", kind=ERROR)
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            progress = float(item.get("progress"))
        except (TypeError, ValueError):
            progress = 0.0
        out.append({"name": str(item.get("name") or ""), "save_path": str(item.get("save_path") or ""),
                    "content_path": str(item.get("content_path") or ""), "progress": progress})
    return out


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

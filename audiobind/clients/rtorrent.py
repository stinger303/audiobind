"""rTorrent, through XML-RPC - straight over SCGI (``scgi://host:port``) or through a web server's
XML-RPC mount (``http(s)://host/RPC2``, optionally with basic auth).

Protocol (rTorrent 0.9.x command names):

* the payload is a standard XML-RPC call, marshalled with the standard library's
  ``xmlrpc.client.dumps`` / ``loads``. Over SCGI it is framed as a netstring of NUL-separated
  headers (``CONTENT_LENGTH`` first, ``SCGI 1``) followed by the body; the answer starts with
  CGI-style headers (``Status: 200 OK``) and a blank line;
* ``system.client_version`` is the login proof and the version - SCGI has no authentication at
  all, so the socket must only be reachable from trusted containers;
* ``load.raw_start ["", <torrent bytes>, 'd.custom1.set="<category>"']`` adds and starts.
  rTorrent answers 0 whether or not it took the file, so the adapter computes the info hash
  itself, refuses a file whose hash it cannot read, checks with ``d.hash`` that the torrent is
  NOT already there before the add (a duplicate is a refusal, as in qBittorrent) and IS there
  after it;
* the category is ``custom1`` - the field ruTorrent shows as the label (ruTorrent URL-encodes
  it, so both forms are matched when reading);
* ``d.multicall2 ["", "main", "d.hash=", "d.custom1=", "d.completed_bytes=", "d.size_bytes=",
  "d.ratio="]`` is the one listing; progress is completed/size and ``d.ratio`` is in thousandths.

Exercised against a mocked transport only (tests/test_client_rtorrent.py), never a live rTorrent.
"""

from __future__ import annotations

import re
import socket
import urllib.parse
import urllib.request
import xmlrpc.client

from .base import AUTH, ERROR, REFUSED, UNAVAILABLE, ClientError, DownloadClient
from .torrentfile import info_hash
from .transport import MAX_ANSWER, TIMEOUT, basic_auth, http_post

#: what a category may hold when it is written into an rTorrent command (quoted, so spaces are fine)
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9 _.\-]*$")
LISTING = ("d.hash=", "d.custom1=", "d.completed_bytes=", "d.size_bytes=", "d.ratio=")


class _Fault(ClientError):
    """rTorrent answered with an XML-RPC fault (it was reached; it said no)."""


class RTorrentClient(DownloadClient):
    name = "rTorrent"

    def __init__(self, url: str, username: str = "", password: str = ""):
        url = str(url or "").strip().rstrip("/")
        if not url:
            raise ClientError("RTORRENT_URL is not set", kind=ERROR)
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.username is not None or parts.scheme not in ("scgi", "http", "https") or not parts.hostname:
            raise ClientError("RTORRENT_URL is not valid: use scgi://host:port or http(s)://host/RPC2 "
                              f"(with no login in it): {url!r}", kind=ERROR)
        if parts.scheme == "scgi" and not port:
            raise ClientError(f"RTORRENT_URL needs a port for SCGI: {url!r}", kind=ERROR)
        self.url = url
        self._scgi = (parts.hostname, port) if parts.scheme == "scgi" else None
        self._auth = basic_auth(str(username or ""), str(password or ""))
        self._opener = urllib.request.build_opener()
        self._version = ""
        self.logged_in = False

    def __repr__(self) -> str:  # never the password
        return f"RTorrentClient({self.url!r}, logged_in={self.logged_in})"

    # ------------------------------------------------------------------ transport
    def _send(self, body: bytes) -> bytes:
        """The one network call: an XML-RPC request in, the XML answer out. Tests replace this."""
        if self._scgi is None:
            status, _headers, answer = http_post(
                self._opener, self.url, body, {"Content-Type": "text/xml", **self._auth}, self.name)
            if status in (401, 403):
                raise ClientError(f"rTorrent's XML-RPC endpoint refused the login (HTTP {status}) - check "
                                  "RTORRENT_USER / RTORRENT_PASS", kind=AUTH)
            if status != 200:
                raise ClientError(f"rTorrent's XML-RPC endpoint answered HTTP {status}", kind=ERROR)
            return answer
        return self._scgi_send(body)

    def _scgi_send(self, body: bytes) -> bytes:
        headers = (b"CONTENT_LENGTH\x00" + str(len(body)).encode() + b"\x00SCGI\x001\x00"
                   b"REQUEST_METHOD\x00POST\x00REQUEST_URI\x00/RPC2\x00")
        request = str(len(headers)).encode() + b":" + headers + b"," + body
        host, port = self._scgi
        chunks, size = [], 0
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
                sock.sendall(request)
                while size <= MAX_ANSWER:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise ClientError(f"rTorrent at {host}:{port} could not be reached ({type(exc).__name__})",
                              kind=UNAVAILABLE) from None
        answer = b"".join(chunks)
        head, sep, rest = answer.partition(b"\r\n\r\n")
        if sep and not head.lstrip().startswith(b"<"):
            status = re.search(rb"(?im)^status:\s*(\d+)", head)
            if status and status.group(1) != b"200":
                raise ClientError(f"rTorrent answered SCGI status {status.group(1).decode()}", kind=ERROR)
            return rest
        return answer

    def _call(self, method: str, *params):
        body = xmlrpc.client.dumps(tuple(params), method, allow_none=False).encode("utf-8")
        answer = self._send(body)
        try:
            result, _method = xmlrpc.client.loads(answer)
        except xmlrpc.client.Fault as fault:
            raise _Fault(f"rTorrent refused {method}: {str(fault.faultString)[:160]}", kind=ERROR) from None
        except Exception:  # noqa: BLE001 - expat / value errors: whatever it is, it is unreadable
            raise ClientError(f"rTorrent answered {method} with something unreadable", kind=ERROR) from None
        return result[0] if result else None

    # ------------------------------------------------------------------ the contract
    def login(self) -> None:
        self._version = str(self._call("system.client_version") or "")
        self.logged_in = True

    def _ensure(self) -> None:
        if not self.logged_in:
            self.login()

    def _has(self, digest: str) -> bool:
        try:
            self._call("d.hash", digest.upper())
        except _Fault:
            return False  # "Could not find info-hash."
        return True

    def add_torrent(self, torrent: bytes, filename: str = "tracker.torrent", category: str = "audiobooks") -> str:
        self._ensure()
        label = str(category or "").strip()
        if not _CATEGORY_RE.match(label):
            raise ClientError("the category can only hold letters, digits, spaces and _ . - for rTorrent",
                              kind=ERROR)
        digest = info_hash(torrent)
        if not digest:
            raise ClientError("rTorrent was not sent the torrent: the file is not a valid torrent", kind=REFUSED)
        if self._has(digest):
            raise ClientError("rTorrent refused the torrent - already added", kind=REFUSED)
        commands = [f'd.custom1.set="{label}"'] if label else []
        try:
            self._call("load.raw_start", "", xmlrpc.client.Binary(torrent), *commands)
        except _Fault as exc:
            raise ClientError(f"rTorrent refused the torrent ({str(exc).split(': ', 1)[-1]})", kind=REFUSED) from None
        if not self._has(digest):
            raise ClientError("rTorrent did not take the torrent", kind=REFUSED)
        return digest

    def _listing(self) -> list[tuple[str, str, float, float]]:
        """(hash, custom1, progress, ratio) for every torrent; rows that cannot be read are left out."""
        self._ensure()
        rows = self._call("d.multicall2", "", "main", *LISTING)
        out = []
        for row in rows if isinstance(rows, list) else []:
            try:
                digest, custom1, done, size, ratio = row
                progress = (float(done) / float(size)) if float(size) > 0 else 0.0
                out.append((str(digest).lower(), str(custom1), progress, max(0.0, float(ratio) / 1000.0)))
            except (TypeError, ValueError):
                continue
        return out

    def progress(self, hashes) -> dict[str, float]:
        wanted = [str(h).lower() for h in hashes if h]
        if not wanted:
            return {}
        return {digest: progress for digest, _l, progress, _r in self._listing() if digest in wanted}

    def _category(self, category: str):
        label = str(category or "").strip()
        return [r for r in self._listing() if r[1] == label or urllib.parse.unquote(r[1]) == label]

    def incomplete(self, category: str, done: float) -> int:
        return sum(1 for _d, _l, progress, _r in self._category(category) if progress < done)

    def snatch_state(self, category: str) -> dict[str, tuple[float, float]]:
        return {digest: (progress, ratio) for digest, _l, progress, ratio in self._category(category)}

    def version(self) -> str:
        self.login()
        self.logout()
        text = self._version.strip()
        if not text or len(text) > 40:
            raise ClientError("rTorrent did not say which version it is", kind=ERROR)
        return text[1:] if text[:1] in ("v", "V") else text

    def logout(self) -> None:
        """XML-RPC has no session: nothing to end."""
        self.logged_in = False

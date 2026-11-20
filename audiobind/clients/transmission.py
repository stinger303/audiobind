"""Transmission, through its RPC (``POST <TRANSMISSION_URL>``, normally ``.../transmission/rpc``).

Protocol as the RPC spec describes it:

* every call is ``{"method": ..., "arguments": {...}, "tag": n}`` and answers
  ``{"result": "success" | <error text>, "arguments": {...}}``;
* CSRF protection: a call without the current ``X-Transmission-Session-Id`` answers **409** with
  that header; the call is repeated once with it;
* authentication is optional HTTP basic auth (401 = wrong user/password); 403 means the RPC
  whitelist does not include this host;
* ``torrent-add {metainfo: base64, paused: false, labels: [category]}`` answers
  ``torrent-added`` or ``torrent-duplicate`` (a duplicate is a refusal here, as it is in
  qBittorrent). ``labels`` on add needs RPC 17 (4.0); on RPC 16 (3.0) the label is set right
  after with ``torrent-set``. Below RPC 16 there are no labels at all, so login() refuses;
* ``torrent-get`` gives ``percentDone`` (0..1) and ``uploadRatio`` (-1 = none, -2 = infinite;
  both are read as 0.0 - the cautious side for the budget).

Exercised against a mocked transport only (tests/test_client_transmission.py), never a live
Transmission.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import urllib.parse

from .base import AUTH, ERROR, REFUSED, ClientError, DownloadClient
from .torrentfile import info_hash, is_hex_hash
from .transport import basic_auth, guarded_opener, http_post, http_url

log = logging.getLogger("audiobind")

SESSION_HEADER = "x-transmission-session-id"
LABELS_ON_ADD_RPC = 17  # Transmission 4.0
LABELS_RPC = 16  # Transmission 3.0


class TransmissionClient(DownloadClient):
    name = "Transmission"

    def __init__(self, url: str, username: str = "", password: str = ""):
        url, origin = http_url(url, "TRANSMISSION_URL")
        if urllib.parse.urlsplit(url).path in ("", "/"):
            url += "/transmission/rpc"  # the address of the web UI was given: the RPC lives here
        self.url, self.origin = url, origin
        self._auth = basic_auth(str(username or ""), str(password or ""))
        self._opener = guarded_opener()  # SEC-04: Basic auth never follows a redirect off-origin
        self._session_id = ""
        self._tags = itertools.count(1)
        self.rpc_version = 0
        self._version = ""
        self.logged_in = False

    def __repr__(self) -> str:  # never the password
        return f"TransmissionClient({self.url!r}, logged_in={self.logged_in})"

    # ------------------------------------------------------------------ transport
    def _send(self, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        """The one network call. Tests replace this."""
        return http_post(self._opener, self.url, body, headers, self.name)

    def _rpc(self, method: str, arguments: dict | None = None) -> dict:
        body = json.dumps({"method": method, "arguments": arguments or {}, "tag": next(self._tags)}).encode()
        for _attempt in range(2):
            headers = {"Content-Type": "application/json", **self._auth}
            if self._session_id:
                headers["X-Transmission-Session-Id"] = self._session_id
            status, answer_headers, answer = self._send(body, headers)
            if status == 409 and answer_headers.get(SESSION_HEADER):
                self._session_id = answer_headers[SESSION_HEADER]
                continue
            break
        if status == 401:
            raise ClientError("Transmission refused the login (HTTP 401) - check TRANSMISSION_USER / "
                              "TRANSMISSION_PASS", kind=AUTH)
        if status == 403:
            raise ClientError("Transmission refused the connection (HTTP 403: its RPC whitelist does not "
                              "include this host)", kind=AUTH)
        if status != 200:
            raise ClientError(f"Transmission answered HTTP {status} to {method}", kind=ERROR)
        try:
            data = json.loads(answer.decode("utf-8", "replace"))
        except ValueError:
            raise ClientError(f"Transmission answered {method} with something unreadable", kind=ERROR) from None
        if not isinstance(data, dict):
            raise ClientError(f"Transmission answered {method} with something unreadable", kind=ERROR)
        if data.get("result") != "success":
            raise ClientError(f"Transmission refused {method}: {str(data.get('result'))[:160]}", kind=ERROR)
        arguments = data.get("arguments")
        return arguments if isinstance(arguments, dict) else {}

    # ------------------------------------------------------------------ the contract
    def login(self) -> None:
        session = self._rpc("session-get", {"fields": ["version", "rpc-version"]})
        try:
            self.rpc_version = int(session.get("rpc-version") or 0)
        except (TypeError, ValueError):
            self.rpc_version = 0
        if self.rpc_version and self.rpc_version < LABELS_RPC:
            raise ClientError("Transmission 3.0 or newer is needed - older versions have no labels, so the "
                              "category could not be followed", kind=ERROR)
        self._version = str(session.get("version") or "")
        self.logged_in = True

    def _ensure(self) -> None:
        if not self.logged_in:
            self.login()

    def add_torrent(self, torrent: bytes, filename: str = "tracker.torrent", category: str = "audiobooks") -> str:
        self._ensure()
        label = str(category or "").strip()
        arguments = {"metainfo": base64.b64encode(torrent).decode(), "paused": False}
        if label and self.rpc_version >= LABELS_ON_ADD_RPC:
            arguments["labels"] = [label]
        try:
            answer = self._rpc("torrent-add", arguments)
        except ClientError as exc:
            if exc.kind != ERROR:
                raise
            raise ClientError(f"Transmission refused the torrent ({str(exc).split(': ', 1)[-1]})",
                              kind=REFUSED) from None
        if "torrent-duplicate" in answer:
            raise ClientError("Transmission refused the torrent (torrent-duplicate) - already added?", kind=REFUSED)
        added = answer.get("torrent-added")
        if not isinstance(added, dict):
            raise ClientError("Transmission did not say it took the torrent", kind=REFUSED)
        reported = added.get("hashString")
        digest = reported.lower() if is_hex_hash(reported) else info_hash(torrent)
        if label and self.rpc_version < LABELS_ON_ADD_RPC and digest:
            try:
                self._rpc("torrent-set", {"ids": [digest], "labels": [label]})
            except ClientError as exc:  # the torrent is in: say so, do not pretend it failed
                log.warning("Transmission took the torrent but could not label it %r (%s)", label, exc)
        return digest

    def _get(self, fields: list, ids: list | None = None) -> list:
        self._ensure()
        arguments: dict = {"fields": fields}
        if ids is not None:
            arguments["ids"] = ids
        torrents = self._rpc("torrent-get", arguments).get("torrents")
        return [t for t in torrents if isinstance(t, dict)] if isinstance(torrents, list) else []

    def progress(self, hashes) -> dict[str, float]:
        wanted = [str(h).lower() for h in hashes if h]
        if not wanted:
            return {}
        out: dict[str, float] = {}
        for item in self._get(["hashString", "percentDone"], wanted):
            digest = str(item.get("hashString") or "").lower()
            try:
                value = float(item.get("percentDone"))
            except (TypeError, ValueError):
                continue
            if digest in wanted:
                out[digest] = value
        return out

    def _category(self, category: str) -> list[dict]:
        label = str(category or "").strip()
        items = self._get(["hashString", "percentDone", "uploadRatio", "labels"])
        return [t for t in items if isinstance(t.get("labels"), list) and label in t["labels"]]

    def incomplete(self, category: str, done: float) -> int:
        count = 0
        for item in self._category(category):
            try:
                if float(item.get("percentDone")) < done:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def snatch_state(self, category: str) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for item in self._category(category):
            digest = str(item.get("hashString") or "").lower()
            try:
                ratio = float(item.get("uploadRatio"))
                out[digest] = (float(item.get("percentDone")), ratio if ratio > 0 else 0.0)
            except (TypeError, ValueError):
                continue
        return out

    def version(self) -> str:
        self.login()
        self.logout()
        text = self._version.split(" ")[0].strip()  # "4.0.5 (a6fe2a64aa)" -> "4.0.5"
        if not text or len(text) > 40:
            raise ClientError("Transmission did not say which version it is", kind=ERROR)
        return text

    def logout(self) -> None:
        """Transmission's RPC has no session to end; forget the CSRF token."""
        self.logged_in = False
        self._session_id = ""

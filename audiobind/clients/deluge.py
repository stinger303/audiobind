"""Deluge, through its Web UI's JSON-RPC (``POST <DELUGE_URL>/json``).

Protocol as the Deluge 2.x Web UI serves it:

* every call is ``{"method": ..., "params": [...], "id": n}`` and answers HTTP 200 with
  ``{"result": ..., "error": null | {"message", "code"}, "id": n}`` - an error is in the body, not
  the status. Code 1 means "not authenticated";
* ``auth.login [password]`` answers true/false and sets a ``_session_id`` cookie (the Web UI has
  a password only, no user name);
* the Web UI must be connected to a daemon: ``web.connected``, and if not, ``web.get_hosts`` and
  ``web.connect [host id]`` with the first host it knows;
* ``core.add_torrent_file [filename, base64, options]`` answers the info hash, or an error such as
  "Torrent already in session";
* the category is a **label** from the Label plugin (``label.add``, ``label.set_torrent``). Deluge
  stores labels lower-case. login() checks that the plugin is enabled - without it a torrent
  cannot be followed by category, and the watcher's budget would be blind - so a missing plugin is
  reported before anything is downloaded at the tracker;
* ``core.get_torrents_status [filter, keys]`` answers ``{hash: {key: value}}``; ``progress`` is a
  PERCENTAGE (0-100) and is turned into 0..1 here.

Exercised against a mocked transport only (tests/test_client_deluge.py), never a live Deluge.
"""

from __future__ import annotations

import base64
import http.cookiejar
import itertools
import json
import logging
import urllib.request

from .base import AUTH, ERROR, REFUSED, UNAVAILABLE, ClientError, DownloadClient
from .torrentfile import info_hash, is_hex_hash
from .transport import http_post, http_url

log = logging.getLogger("audiobind")

NOT_AUTHENTICATED = 1


class DelugeClient(DownloadClient):
    name = "Deluge"

    def __init__(self, url: str, password: str):
        self.url, self.origin = http_url(url, "DELUGE_URL")
        self._password = str(password or "")
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._ids = itertools.count(1)
        self.logged_in = False

    def __repr__(self) -> str:  # never the password
        return f"DelugeClient({self.url!r}, logged_in={self.logged_in})"

    # ------------------------------------------------------------------ transport
    def _send(self, body: bytes) -> tuple[int, bytes]:
        """The one network call: POST to /json. Tests replace this."""
        status, _headers, answer = http_post(
            self._opener, self.url + "/json", body,
            {"Content-Type": "application/json", "Accept": "application/json"}, self.name)
        return status, answer

    def _call(self, method: str, *params):
        body = json.dumps({"method": method, "params": list(params), "id": next(self._ids)}).encode()
        status, answer = self._send(body)
        if status in (401, 403):
            raise ClientError(f"Deluge refused the request (HTTP {status})", kind=AUTH)
        if status != 200:
            raise ClientError(f"Deluge answered HTTP {status} to {method}", kind=ERROR)
        try:
            data = json.loads(answer.decode("utf-8", "replace"))
        except ValueError:
            raise ClientError(f"Deluge answered {method} with something unreadable", kind=ERROR) from None
        if not isinstance(data, dict):
            raise ClientError(f"Deluge answered {method} with something unreadable", kind=ERROR)
        error = data.get("error")
        if error:
            message = str(error.get("message") if isinstance(error, dict) else error)[:160]
            code = error.get("code") if isinstance(error, dict) else None
            if code == NOT_AUTHENTICATED:
                raise ClientError(f"Deluge says the session is not logged in ({method})", kind=AUTH)
            raise ClientError(f"Deluge refused {method}: {message}", kind=ERROR)
        return data.get("result")

    # ------------------------------------------------------------------ the contract
    def login(self) -> None:
        if not self._password:
            raise ClientError("DELUGE_PASS is not set", kind=AUTH)
        if self._call("auth.login", self._password) is not True:
            raise ClientError("Deluge refused the password - check DELUGE_PASS", kind=AUTH)
        if not self._call("web.connected"):
            hosts = self._call("web.get_hosts") or []
            if not hosts or not isinstance(hosts[0], (list, tuple)) or not hosts[0]:
                raise ClientError("Deluge's web UI knows no daemon to connect to", kind=UNAVAILABLE)
            self._call("web.connect", hosts[0][0])
            if not self._call("web.connected"):
                raise ClientError("Deluge's web UI could not connect to its daemon", kind=UNAVAILABLE)
        plugins = self._call("core.get_enabled_plugins") or []
        if "Label" not in plugins:
            raise ClientError("Deluge's Label plugin is not enabled - Audiobind needs it to put torrents in "
                              "the category and follow them", kind=ERROR)
        self.logged_in = True

    def _ensure(self) -> None:
        if not self.logged_in:
            self.login()

    def add_torrent(self, torrent: bytes, filename: str = "tracker.torrent", category: str = "audiobooks") -> str:
        self._ensure()
        label = str(category or "").strip().lower()
        try:
            labels = self._call("label.get_labels") or []
            if label and label not in labels:
                self._call("label.add", label)
        except ClientError as exc:
            if exc.kind == AUTH:
                raise
            raise ClientError(f"Deluge could not prepare the label {label!r} ({exc})", kind=ERROR) from None
        options = {"add_paused": False}
        try:
            answer = self._call("core.add_torrent_file", filename, base64.b64encode(torrent).decode(), options)
        except ClientError as exc:
            if exc.kind != ERROR:
                raise
            raise ClientError(f"Deluge refused the torrent ({str(exc).split(': ', 1)[-1]}) - already added?",
                              kind=REFUSED) from None
        if not answer:
            raise ClientError("Deluge did not take the torrent - already added?", kind=REFUSED)
        digest = answer.lower() if is_hex_hash(answer) else info_hash(torrent)
        if label and digest:
            try:
                self._call("label.set_torrent", digest, label)
            except ClientError as exc:  # the torrent is in: say so, do not pretend it failed
                log.warning("Deluge took the torrent but could not label it %r (%s)", label, exc)
        return digest

    def _status(self, filter_dict: dict, keys: list) -> dict:
        self._ensure()
        data = self._call("core.get_torrents_status", filter_dict, keys)
        return data if isinstance(data, dict) else {}

    def progress(self, hashes) -> dict[str, float]:
        wanted = [str(h).lower() for h in hashes if h]
        if not wanted:
            return {}
        out: dict[str, float] = {}
        for digest, item in self._status({"id": wanted}, ["hash", "progress"]).items():
            if not isinstance(item, dict):
                continue
            try:
                value = float(item.get("progress")) / 100.0
            except (TypeError, ValueError):
                continue
            if str(digest).lower() in wanted:
                out[str(digest).lower()] = value
        return out

    def _category(self, category: str) -> list[tuple[str, dict]]:
        label = str(category or "").strip().lower()
        data = self._status({"label": label}, ["hash", "progress", "ratio", "label"])
        return [(str(h).lower(), item) for h, item in data.items() if isinstance(item, dict)]

    def incomplete(self, category: str, done: float) -> int:
        count = 0
        for _digest, item in self._category(category):
            try:
                if float(item.get("progress")) / 100.0 < done:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def snatch_state(self, category: str) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for digest, item in self._category(category):
            try:
                ratio = float(item.get("ratio"))
                out[digest] = (float(item.get("progress")) / 100.0, ratio if ratio > 0 else 0.0)
            except (TypeError, ValueError):
                continue
        return out

    def version(self) -> str:
        self.login()
        try:
            answer = self._call("daemon.info")
        finally:
            self.logout()
        text = str(answer or "").strip()
        if not text or len(text) > 40:
            raise ClientError("Deluge did not say which version it is", kind=ERROR)
        return text[1:] if text[:1] in ("v", "V") else text

    def logout(self) -> None:
        if self.logged_in:
            try:
                self._call("auth.delete_session")
            except Exception:  # noqa: BLE001 - best effort, and logout never raises
                pass
        self.logged_in = False
        self.jar.clear()

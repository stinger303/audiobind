"""The download-client contract: what the wishlist watcher relies on, written down.

Derived from what ``audiobind/qbit.py`` does and what ``audiobind/watcher.py`` calls on it as of
0.21.0 - nothing here is speculative. Every call the app makes, and how much it depends on it:

=====================  ==========  ==============================================================
call                   kind        who calls it, and what a failure does there
=====================  ==========  ==============================================================
``login()``            mandatory   ``Watcher._qbit``: once per cycle, BEFORE the first tracker
                                   download, so an unreachable client never costs a freeleech
                                   credit. A ClientError ends the cycle ("... is not available").
``add_torrent()``      mandatory   ``Watcher._one``: hands over the .torrent bytes the tracker
                                   client fetched. A ClientError marks the row ``error`` and ends
                                   the cycle; the returned hash is stored to follow the download.
``logout()``           mandatory   every caller, in ``finally`` - often WITHOUT a try around it,
                                   so it must never raise.
``progress()``         best effort ``Watcher._mark_downloaded``: a ClientError is logged once and
                                   every row is left as it is.
``incomplete()``       best effort ``Watcher._holding`` (WATCH_MAX_INCOMPLETE): a ClientError is
                                   logged once and no limit applies that cycle.
``snatch_state()``     best effort ``Watcher._budget``: any failure marks the budget unknown, which
                                   FAILS CLOSED - no freeleech credit is spent that cycle.
``version()``          health      ``watcher.probe_qbit`` (the Settings page's connection test)
                                   only; never called by a cycle.
``url`` attribute      -           the address the client was built with (shown, compared in
                                   tests); never carries a password.
=====================  ==========  ==============================================================

Two things the app deliberately does NOT do, so they are not in the contract:

* add by magnet or by .torrent URL. The tracker download goes through the tracker client (its
  session cookie, pacing and egress guard); handing the URL to the download client instead would
  send it to the tracker from a different address with no guard. ``add_torrent`` takes bytes.
* list torrents for a dedupe check. A duplicate is the client refusing the add (qBittorrent 5
  answers "Fails."), which the watcher already turns into a row error. ``snatch_state`` is the one
  listing, and it is for the budget. Per-torrent state names and seeder counts are not read by
  anything, so they are not asked for.

The failure rule, for every method except ``logout``: any failure - unreachable, refused, bad
credentials, an unreadable answer - is raised as ``ClientError`` (or a subclass) with a plain
message that never contains a password. No other exception type may escape: the watcher catches
``ClientError`` and nothing else in several places, and a stray ``OSError`` or ``ValueError``
would end a cycle with a traceback instead of the "client unavailable" state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

#: ``ClientError.kind`` values: what went wrong, for tests and messages (the message is for people)
UNAVAILABLE = "unavailable"  # could not be reached at all (connection refused, timeout, DNS)
AUTH = "auth"  # reached, but the credentials were refused
REFUSED = "refused"  # reached and logged in, but this torrent was not taken (duplicate, invalid)
ERROR = "error"  # anything else: an unexpected status, an unreadable answer, a bad address


class ClientError(Exception):
    """The download client refused or could not be reached. ``str()`` is the plain message, which
    never carries a password; ``kind`` is one of UNAVAILABLE / AUTH / REFUSED / ERROR."""

    def __init__(self, message: str = "", kind: str = ERROR):
        super().__init__(message)
        self.kind = kind


class DownloadClient(ABC):
    """One session with one download client. Built per use (the watcher makes a fresh one for each
    read and one per cycle for adds) and thrown away; nothing is shared between instances.

    The constructor may raise ClientError(kind=ERROR) when the address is unusable (not http(s),
    not set). It must not touch the network."""

    #: the client program's name as people know it, used in messages ("qBittorrent", "Deluge", ...)
    name: str = "download client"
    #: the address the client was built with; never a password
    url: str = ""
    #: True between a successful login() and logout()
    logged_in: bool = False

    @abstractmethod
    def login(self) -> None:
        """Prove the client is reachable and the credentials work, and open a session if the
        protocol has one. Must make at least one real request: the watcher calls it to make sure a
        torrent can be handed over BEFORE spending anything at the tracker.

        Failure: ClientError - kind AUTH for refused or missing credentials, UNAVAILABLE when it
        cannot be reached."""

    @abstractmethod
    def add_torrent(self, torrent: bytes, filename: str = "tracker.torrent", category: str = "audiobooks") -> str:
        """Hand one .torrent file (its bytes) to the client, started (not paused), in ``category``
        (the client's own word for it: category, label, custom1). Logs in first when needed.

        Returns the torrent's v1 info hash, lower-case hex - from the client's answer when it gives
        one, else computed from the file - or "" when neither is available.

        Failure: ClientError - kind REFUSED when the client was reached but did not take this
        torrent (already present, not a valid torrent), AUTH / UNAVAILABLE as for login()."""

    @abstractmethod
    def progress(self, hashes) -> dict[str, float]:
        """``{info hash: progress 0..1}`` for the given hashes, in as few requests as the protocol
        allows. Hashes are matched lower-case; one the client does not have is simply absent.
        Empty/falsy hashes are ignored, and with none left nothing is sent at all.

        Failure: ClientError (the caller logs it and leaves every row as it is)."""

    @abstractmethod
    def incomplete(self, category: str, done: float) -> int:
        """How many torrents in ``category`` are below progress ``done`` (0..1). An entry whose
        progress cannot be read is not counted.

        Failure: ClientError (the caller logs it and applies no limit this cycle)."""

    @abstractmethod
    def snatch_state(self, category: str) -> dict[str, tuple[float, float]]:
        """``{info hash: (progress 0..1, share ratio)}`` for every torrent in ``category``. A
        client that has no ratio for a torrent reports 0.0. Entries that cannot be read are left out.

        Failure: ClientError (the caller fails closed: no freeleech credit is spent this cycle)."""

    @abstractmethod
    def version(self) -> str:
        """The connection test: log in, ask the client's version, log out again. Returns the
        version text without a leading "v" (e.g. "5.0.4").

        Failure: ClientError with the same plain messages as every other call."""

    @abstractmethod
    def logout(self) -> None:
        """End the session (best effort) and forget it. Must NEVER raise - callers use it in
        ``finally`` without a try - and is safe to call when not logged in."""

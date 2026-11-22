"""Phase 3: the wishlist watcher - wanted books from a private tracker, through qBittorrent, into ingest.

Off unless the tracker settings are complete (TRACKER_URL, TRACKER_SEARCH_PATH,
TRACKER_SEARCH_BODY, TRACKER_DOWNLOAD_PATH, TRACKER_COOKIE_NAME, TRACKER_SESSION): otherwise no thread starts and
nothing touches the network.

One cycle, every WATCH_INTERVAL_MIN (or on "Run a cycle now"):

1. Wishlist rows already in the library are marked ``in_library`` (no network).
2. Unless WATCH_MAX_INCOMPLETE (0 = off) torrents in the qBittorrent category are still
   downloading - then the cycle holds: nothing is searched or added, one line is logged. Steps 1
   and the ``downloaded`` flip never wait for it.
   Up to WATCH_MAX_PER_CYCLE ``wanted`` rows - never-checked first, oldest added first, then
   the least recently checked - are each searched on the tracker (tracker.TrackerClient: one request at a
   time, >= 3 s apart, backoff on any failure).
3. No result good enough (tracker.select_candidate): the row stays ``wanted`` with a "not found
   on the tracker (checked <date>)" note and is searched again in a later cycle; after MISS_LIMIT
   misses in a row it becomes ``not_found`` until you press Retry. Nothing is ever forced.
4. A match: the freeleech decision (tracker.freeleech_decision) is written into the row's note. In dry
   run that is all - nothing is downloaded. Otherwise qBittorrent is logged into FIRST (a
   qBittorrent that is down must never cost a freeleech credit), the .torrent is downloaded and
   handed to qBittorrent, and the row becomes ``downloading``. The ingest conveyor does the
   rest; the row becomes ``in_library`` when the book shows up converted.

Failure rules - a freeleech credit is never refunded, so nothing after a download request is retried
on its own: a failed download or a qBittorrent refusal turns the row into ``error`` (with the
reason) and only a Retry click puts it back. Any tracker failure ends the cycle at once.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import uuid4

from . import egress, ipupdate, qbit, tracker
from .clients.base import ClientError, DownloadClient
from . import config as _config
from .config import (
    DOWNLOAD_CLIENTS,
    WATCHER_REQUIRED,
    effective_val,
    effective_values,
    set_effective,
    watch_interval_min,
    watch_max_incomplete,
    watch_max_per_cycle,
    watch_max_unsatisfied,
    wedge_per_cycle,
    wedge_per_day,
    wedge_reserve,
)
from .control import CONTROL
from .state import (
    WISHLIST_DOWNLOADED,
    WISHLIST_DOWNLOADING,
    WISHLIST_ERROR,
    WISHLIST_IN_LIBRARY,
    WISHLIST_NOT_FOUND,
    WISHLIST_WANTED,
)

log = logging.getLogger("audiobind")

#: consecutive "not found" searches before a row stops being searched (Retry resets it)
MISS_LIMIT = 3
#: the first timed cycle after a start with no cycle on record waits this long
FIRST_CYCLE_DELAY = 120.0
#: a torrent at or above this progress counts as finished
DONE_PROGRESS = 0.999
DOWNLOADED_NOTE = "downloaded - waiting for the converted book"
#: unit statuses that mean the book is in the library: converted here, or found already
#: published ("ready" with an output file - a dry-run "ready" has none)
PUBLISHED = ("converted", "ready")

META_PAUSED = "watcher_paused"
META_LAST_CYCLE = "watcher_last_cycle"
META_LAST_ERROR = "watcher_last_error"
META_BACKOFF = "watcher_backoff"
META_EGRESS = "watcher_egress"
#: the allowed-IP updater's record: last change (when, old, new) and the drift already reported
META_IPUPDATE = "watcher_ipupdate"


def _stamp(at: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() if at is None else at))


def _setting(name: str) -> str:
    """A watcher setting through the effective-config getter (env default unless overridden)."""
    return str(effective_val(name) or "")


def missing_settings() -> list[str]:
    """The tracker variables that are still empty (the watcher needs all of them)."""
    return [var for var, attr in WATCHER_REQUIRED if not _setting(attr)]


def configured() -> bool:
    """Every tracker setting the watcher needs is set: the owner wants the watcher."""
    return not missing_settings()


def tracker_host() -> str:
    """The configured tracker's host (and port) - never the full URL."""
    from urllib.parse import urlsplit

    return urlsplit(_setting("tracker_url")).netloc


def disabled_reason() -> str:
    """Why the watcher will not run ("" = it can)."""
    missing = missing_settings()
    if missing:
        return f"not set: {', '.join(missing)}"
    kind = client_kind()
    if kind == "qbittorrent":
        # an empty address is "not filled in yet", never an error; a malformed one still fails loudly
        if not qbit_url().strip():
            return "QBIT_URL is not set - the watcher has nowhere to send a torrent"
        if not _setting("qbit_user") or not _setting("qbit_pass"):
            return "QBIT_USER / QBIT_PASS are not set - the watcher has nowhere to send a torrent"
        return ""
    # the other clients: the address always, and the password where the protocol has no anonymous login
    needed = [f"{kind.upper()}_URL"] if not _setting(f"{kind}_url") else []
    if kind == "deluge" and not _setting("deluge_pass"):
        needed.append("DELUGE_PASS")
    if needed:
        return f"{' / '.join(needed)} {'is' if len(needed) == 1 else 'are'} not set - the watcher has nowhere to send a torrent"
    return ""


def qbit_category() -> str:
    return _setting("qbit_category") or "audiobooks"


def qbit_url() -> str:
    return _setting("qbit_url")


# the network, behind two factories tests replace
def make_tracker(route: TrackerRoute | None = None) -> tracker.TrackerClient:
    settings = (route or capture_route()).settings
    endpoints = tracker.Endpoints.build(
        settings["tracker_url"],
        settings["tracker_search_path"],
        settings["tracker_download_path"],
        search_method=settings["tracker_search_method"],
        download_param=settings["tracker_download_param"],
        freeleech_param=settings["tracker_freeleech_param"],
        search_category=settings["tracker_search_category"],
        search_body=settings["tracker_search_body"],
        search_encoding=settings["tracker_search_encoding"],
    )
    return tracker.TrackerClient(settings["tracker_session"], endpoints, settings["tracker_cookie_name"],
                                 proxy=settings["tracker_proxy"], user_agent=settings["tracker_user_agent"])


def make_ip_transport(route: TrackerRoute):
    """The MAM IP transport, bound to the route used for the probe."""
    settings = route.settings
    return ipupdate.urllib_transport(settings["tracker_proxy"],
                                     tracker.resolve_user_agent(settings["tracker_user_agent"]))


def make_qbit(url: str | None = None) -> qbit.QbitClient:
    """The qBittorrent client for the saved settings; ``url`` tries the same login at another address
    (the setup guide's one retry on this container's own loopback) without saving anything."""
    return qbit.QbitClient(qbit_url() if url is None else url, _setting("qbit_user"), _setting("qbit_pass"))


def client_kind() -> str:
    """DOWNLOAD_CLIENT through the effective getter: qbittorrent unless another client is chosen."""
    kind = _setting("download_client").strip().lower()
    return kind if kind in DOWNLOAD_CLIENTS else "qbittorrent"


def client_label() -> str:
    """The selected client's name for messages: "qBittorrent" unless another client is chosen."""
    return DOWNLOAD_CLIENTS[client_kind()]


def client_url() -> str:
    """The selected client's address (never a password)."""
    kind = client_kind()
    return qbit_url() if kind == "qbittorrent" else _setting(f"{kind}_url")


class _Unusable(DownloadClient):
    """A non-qBittorrent client whose settings could not even build a client (no URL, a bad URL):
    every call raises that same ClientError, so the watcher sees "not available" and never a raw
    exception from a constructor. logout() is a no-op, as the contract requires."""

    def __init__(self, name: str, error: ClientError):
        self.name, self._error, self.url, self.logged_in = name, error, "", False

    def _fail(self, *_a, **_k):
        raise ClientError(str(self._error), kind=self._error.kind)

    login = add_torrent = progress = incomplete = snatch_state = version = _fail

    def logout(self) -> None:
        self.logged_in = False


def make_client() -> DownloadClient:
    """The download client the watcher hands torrents to. qBittorrent (the default) is make_qbit(),
    exactly as before; another client is built from its own settings, read per use."""
    kind = client_kind()
    if kind == "qbittorrent":
        return make_qbit()
    from .clients.deluge import DelugeClient
    from .clients.rtorrent import RTorrentClient
    from .clients.transmission import TransmissionClient

    try:
        if kind == "deluge":
            return DelugeClient(_setting("deluge_url"), _setting("deluge_pass"))
        if kind == "transmission":
            return TransmissionClient(_setting("transmission_url"), _setting("transmission_user"),
                                      _setting("transmission_pass"))
        return RTorrentClient(_setting("rtorrent_url"), _setting("rtorrent_user"), _setting("rtorrent_pass"))
    except ClientError as exc:
        return _Unusable(DOWNLOAD_CLIENTS[kind], exc)


#: every setting make_tracker() reads: a change to any of them means a new client
TRACKER_CLIENT_SETTINGS = (
    "tracker_url", "tracker_search_path", "tracker_download_path", "tracker_search_method",
    "tracker_download_param", "tracker_freeleech_param", "tracker_search_category", "tracker_search_body",
    "tracker_search_encoding", "tracker_session", "tracker_cookie_name", "tracker_proxy", "tracker_user_agent",
)


ROUTE_SETTINGS = (*TRACKER_CLIENT_SETTINGS, "tracker_home_ip", "tracker_expected_egress_ip")


def _route_settings():
    return {name: str(value or "") for name, value in effective_values(ROUTE_SETTINGS).items()}


@dataclass(frozen=True)
class TrackerRoute:
    settings: Mapping[str, str] = field(repr=False)
    id: str

    def check(self):
        if self.settings != _route_settings():
            raise tracker.TrackerRouteChanged("tracker routing settings changed during the check; try again")

    @contextmanager
    def checked(self):
        self.check()
        token = tracker.route_check.set(self.check)
        try:
            yield
        finally:
            tracker.route_check.reset(token)


def capture_route() -> TrackerRoute:
    return TrackerRoute(MappingProxyType(_route_settings()), uuid4().hex)


def _client_settings_digest(route: TrackerRoute) -> str:
    """Identify the client settings without retaining a second cleartext session."""
    import hashlib

    joined = repr(tuple(route.settings[name] for name in TRACKER_CLIENT_SETTINGS))
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------- connection tests
#: the title the tracker test searches for until the person types another: a common book, so "found
#: nothing" points at the search settings and not at an unlucky title
DEFAULT_TEST_QUERY = "Dune"
MAX_TEST_QUERY = 200

#: how the folder check decides two container paths are one folder (shown under the qBittorrent test)
CHAIN_RULE = (
    "How this is checked: Audiobind and qBittorrent each call the downloads folder by their own container path "
    "(Audiobind's Ingest path, qBittorrent's category save path). They are the same folder when both Docker pages "
    "map one host folder to those paths. Audiobind cannot read your host folders from inside its container, so it "
    "compares what both can see: a finished torrent that qBittorrent reports under its save path must show up, "
    "by name, inside Audiobind's ingest folder. No finished torrent yet means nothing can be compared.")
#: the folder check looks at no more than this many finished torrents of the category
CHAIN_SAMPLE = 20


def _first_component(content_path: str, save_path: str) -> str:
    """The name qBittorrent gave the download inside its save folder: the first path part under
    ``save_path`` (the folder of a multi-file torrent, the file of a single-file one). "" when there is
    no usable name - it is only ever joined to the ingest folder, so it must be one plain name."""
    from pathlib import PurePosixPath

    content = PurePosixPath(content_path.replace("\\", "/"))
    try:
        parts = content.relative_to(PurePosixPath(save_path.replace("\\", "/"))).parts
    except ValueError:
        parts = (content.name,) if content.name else ()
    name = parts[0] if parts else ""
    return "" if name in ("", ".", "..") or "/" in name else name


def folder_chain(category: str, check: dict, ingest_dir: str, exists=None) -> dict:
    """Does what qBittorrent reports match what Audiobind watches? ``{"level", "lines"}``: level is ``ok``
    (proved: a finished torrent of the category is in the ingest folder), ``problem`` (the category is
    missing or has no save path, or finished torrents are not in the ingest folder) or ``unknown`` (cannot
    tell; the lines say exactly what is missing). Nothing is guessed: ``ok`` needs the proof.
    ``check`` is ``QbitClient.check()``'s answer; ``exists`` (default Path.exists) is the one filesystem read."""
    from pathlib import Path

    exists = exists or (lambda path: Path(path).exists())
    categories = check.get("categories")
    if categories is None:
        why = check.get("categories_error") or "no answer"
        return {"level": "unknown", "lines": [
            f"Could not read qBittorrent's categories ({why}).",
            f"So it is not known whether category '{category}' exists or where it saves."]}
    lines = ["qBittorrent's categories: " + (", ".join(sorted(categories, key=str.lower)) or "none yet") + "."]
    if category not in categories:
        lines.append(f"Category '{category}' isn't in qBittorrent. Create it there (Categories → Add category) "
                     "with its save path set to the folder Audiobind's Ingest path points at.")
        return {"level": "problem", "lines": lines}
    save_path = categories[category]
    if not save_path:
        lines.append(f"Category '{category}' exists but has no save path of its own, so qBittorrent puts its books in "
                     "its default download folder. Give the category a save path: the folder that your qBittorrent "
                     "Docker page maps to the same host folder as Audiobind's Ingest path.")
        return {"level": "problem", "lines": lines}
    lines.append(f"Category '{category}' exists; qBittorrent saves it to {save_path}.")
    if not ingest_dir:
        lines.append("Audiobind's ingest is off (Ingest dir is empty), so there is no folder to compare that with. "
                     "Docker → Audiobind → Edit → Show more settings → Ingest dir.")
        return {"level": "unknown", "lines": lines}
    if not exists(ingest_dir):
        lines.append(f"Audiobind's ingest folder {ingest_dir} is not there from Audiobind's side (the Ingest path "
                     "mount is missing), so nothing can be compared.")
        return {"level": "unknown", "lines": lines}
    torrents = check.get("torrents")
    if torrents is None:
        lines.append(f"Could not read the torrents in '{category}' ({check.get('torrents_error') or 'no answer'}), "
                     "so the folders could not be compared.")
        return {"level": "unknown", "lines": lines}
    done = [t for t in torrents if t.get("progress", 0) >= DONE_PROGRESS][:CHAIN_SAMPLE]
    if not done:
        lines.append(f"Nothing has finished in '{category}' yet, so there is nothing to compare: qBittorrent's "
                     f"{save_path} and Audiobind's {ingest_dir} are not yet shown to be the same folder.")
        if save_path.rstrip("/") == ingest_dir.rstrip("/"):
            lines.append("The two paths are spelled the same. That is right only if your qBittorrent container really "
                         "has the same host folder at that path.")
        return {"level": "unknown", "lines": lines}
    seen = [(t, _first_component(t.get("content_path", ""), t.get("save_path") or save_path)) for t in done]
    for _torrent, name in seen:
        found = str(Path(ingest_dir) / name) if name else ""
        if found and exists(found):
            lines.append(f"Verified: qBittorrent reports '{name}' under {save_path}, and Audiobind finds it at {found}. "
                         f"So {save_path} (qBittorrent) and {ingest_dir} (Audiobind) are the same folder.")
            return {"level": "ok", "lines": lines}
    example = next((name for _t, name in seen if name), "")
    lines.append(f"qBittorrent has {len(seen)} finished torrent(s) in '{category}'"
                 + (f" (for example '{example}' under {save_path})" if example else "")
                 + f", but none of them is in Audiobind's ingest folder {ingest_dir}. Either the two are not the same "
                 "folder, or the books were moved or deleted after they finished. Check that the category's save path "
                 "is the folder your qBittorrent Docker page maps to the same host folder as Audiobind's Ingest path.")
    return {"level": "problem", "lines": lines}


def probe_qbit_report(url: str | None = None) -> dict:
    """Settings > Watcher "Test qBittorrent" / Download client "Test connection": log in to the selected
    client with the saved values and ask the version. For qBittorrent it also reads - in the same session,
    GET requests only, nothing added - its categories and the torrents of the configured category, and says
    whether the category's save path is the folder Audiobind watches. ``{"ok", "message", "chain", "details",
    "rule", "kind"}``; ``chain`` is "" for the other clients, ``kind`` is the ClientError kind of a failure
    (unavailable | auth | refused | error; "" on success). ``url`` (qBittorrent only) tests that address instead
    of the saved one. Nothing is stored; the session is logged out again."""
    label = client_label()
    report = {"ok": False, "message": "", "chain": "", "details": [], "rule": "", "kind": ""}
    qbit_only = url is not None and client_kind() == "qbittorrent"
    if client_kind() == "qbittorrent" and not (url if qbit_only else qbit_url()).strip():
        report["message"] = "QBIT_URL is not set - fill in the qBittorrent URL above and test again"
        return report
    try:
        client = make_qbit(url) if qbit_only else make_client()
        if client_kind() != "qbittorrent":
            report.update(ok=True, message=f"Connected - {label} {client.version()}")
            return report
        category = qbit_category()
        check = client.check(category)
    except ClientError as exc:
        report["message"], report["kind"] = str(exc), exc.kind
        return report
    except Exception as exc:  # noqa: BLE001 - whatever it is, the answer is "not connected"
        report["message"], report["kind"] = f"{label} could not be reached ({type(exc).__name__})", "unavailable"
        return report
    from . import ingest

    chain = folder_chain(category, check, ingest.configured_dir())
    report.update(ok=True, message=f"Connected - {label} {check['version']}", chain=chain["level"],
                  details=chain["lines"], rule=CHAIN_RULE)
    return report


def probe_qbit() -> tuple[bool, str]:
    """``probe_qbit_report()`` as (ok, message). The name stays: the route and the default client are qBittorrent."""
    report = probe_qbit_report()
    return report["ok"], report["message"]


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_test_query(raw) -> str:
    """The title typed into the tracker test: one line, at most MAX_TEST_QUERY characters; empty = the default."""
    return " ".join(str(raw or "").split())[:MAX_TEST_QUERY] or DEFAULT_TEST_QUERY


def probe_tracker(state, query: str = "") -> tuple[bool, str]:
    """Settings > Watcher "Test tracker": ONE search for ``query`` (a common title by default), through the
    same client (so the same pacing and backoff) and behind the same egress guard as a cycle. It proves a
    search works, not only that the site answered. (ok, message). The message is built from the results
    only, never from a setting, so the session cannot be in it."""
    missing = missing_settings()
    if missing:
        return False, f"not set yet: {', '.join(missing)}"
    route = capture_route()
    verdict, reason, _observed = WATCHER._egress_guard(state, route)
    if verdict in ("blocked", "unknown", "changed"):
        return False, reason
    title = clean_test_query(query)
    try:
        with route.checked():
            found = WATCHER.client(route).search(title)
    except tracker.TrackerError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"the tracker could not be reached ({type(exc).__name__})"
    if not found:
        return False, f"The site answered but found nothing for '{_short(title, 80)}'. Check the search body and category."
    n, first = len(found), found[0]
    who = ", ".join(a for a in first.authors[:2] if a)
    return True, (f"Found {n} {'result' if n == 1 else 'results'}; first: {_short(first.name) or '(no title)'}"
                  + (f" — {_short(who)}" if who else ""))


def check_ip() -> dict:
    """Settings > Watcher "Check IP": which address would the tracker see right now, and what would the egress
    guard decide? Asks only the address lookup (through TRACKER_PROXY when set - the very call the guard makes);
    the tracker is never contacted, whether the guard is off or armed. Nothing is stored.
    ``{"ok", "ip", "guard", "message"}``: ``ok`` = the lookup worked; ``guard`` = off | ok | blocked | unknown."""
    proxy = _setting("tracker_proxy")
    home, expected = _setting("tracker_home_ip"), _setting("tracker_expected_egress_ip")
    try:
        ip = egress.probe(proxy)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the answer is "could not check"
        why = str(exc) or type(exc).__name__
        return {"ok": False, "ip": "", "guard": "unknown", "message": (
            f"Could not check — no request was made to the site. The address lookup failed ({why}). "
            + ("Check that TRACKER_PROXY is right and the proxy is running." if proxy else
               "Check this container's internet connection."))}
    via = "through TRACKER_PROXY" if proxy else "directly from this container, with no proxy"
    verdict, reason = egress.judge(home, ip, expected)
    if verdict == "off":
        decision = ("The guard is off, so nothing is checked or blocked. To lock the watcher to this address, enter it "
                    "under \"Only reach the tracker from one IP\" and save: from then on the watcher stops, instead of "
                    "contacting the site, whenever the address is anything else.")
    elif verdict == "ok":
        decision = f"The guard is armed and would let the watcher run: {reason}."
    else:
        decision = f"The guard is armed and would stop the watcher: {reason}."
    return {"ok": True, "ip": ip, "guard": verdict,
            "message": f"The tracker would see {ip} ({via}). {decision} No request was made to the site."}


# ---------------------------------------------------------------------------- allowed-IP updater
def ip_updater_mode() -> str:
    """IP_UPDATER through the effective getter (a page value wins over the env): ``off`` (default),
    ``notify`` or ``auto``."""
    return ipupdate.mode(_setting("ip_updater"))


def _ipupdate_record(state) -> dict:
    rec = state.get_meta(META_IPUPDATE) or {}
    return dict(rec) if isinstance(rec, dict) else {}


def _mam_session(route: TrackerRoute | None = None) -> str:
    """The session is sent only for a configured MyAnonamouse tracker."""
    settings = (route or capture_route()).settings
    return settings["tracker_session"] if ipupdate.is_mam(settings["tracker_url"]) else ""


def _recovery_blocked(route: TrackerRoute, probed: str) -> str:
    """The drift exception relaxes the pin for recovery only. It still requires a verified non-home IP."""
    try:
        route.check()
    except tracker.TrackerRouteChanged as exc:
        return str(exc)
    home = route.settings["tracker_home_ip"]
    if not home:
        return "TRACKER_HOME_IP is not set, so the updater cannot rule out your home connection"
    if not probed:
        return "the egress probe did not verify an address"
    if egress.judge(home, probed)[0] != "ok":
        return "the address check saw your home connection"
    return ""


def _recovery_transport(route: TrackerRoute, probed: str, report: dict):
    """Recheck containment before every read, write and verification request."""
    transport = make_ip_transport(route)

    def send(*args):
        blocked = _recovery_blocked(route, probed)
        if blocked:
            raise ipupdate.UpdateError(ipupdate.REFUSED, blocked)
        report["updater_attempted"] = True
        return transport(*args)

    return send


def _judge_drift(rec: dict, probed: str, probe_error: str, now: float, route: TrackerRoute,
                 transport) -> tuple[str, str, str, str]:
    """Decide from the probe first, then consult MAM only for a contained drift."""
    expected = route.settings["tracker_expected_egress_ip"]
    home = route.settings["tracker_home_ip"]
    last = rec.get("last_updated_at")
    outcome, why = ipupdate.decide(probed, expected, home, last, now, probe_error=probe_error)
    session = _mam_session(route)
    if outcome != ipupdate.UPDATE or not session:
        return outcome, why, probed, "egress probe"
    blocked = _recovery_blocked(route, probed)
    if blocked:
        return ipupdate.REFUSE, blocked, probed, "egress probe"
    log.info("allowed-IP updater: route %s approved for recovery via %s, egress %s, home %s, pinned %s",
             route.id, "proxy" if route.settings["tracker_proxy"] else "direct", probed, home, expected)
    try:
        seen, err = ipupdate.read_ip(session, transport), ""
    except ipupdate.UpdateError as exc:
        seen, err = "", f"MAM's jsonIp.php - {exc}"
    outcome, why = ipupdate.decide(seen, expected, home, last, now, probe_error=err)
    return outcome, why, seen, "MAM"


#: one MAM write at a time (the cycle's auto write and the Settings button)
_WRITE_LOCK = threading.Lock()


def _write_and_pin(state, probed: str, observed: str, now: float, via: str, route: TrackerRoute,
                   transport) -> tuple[bool, str]:
    """Set MAM's dynamic seedbox IP to ``observed``, verify it, then re-pin the allowlist to it.
    Only called after the egress guard and ``decide()`` said ``update``. Records the attempt, the
    result and (on success) the timestamp the hourly limit uses. (ok, message)."""
    blocked = _recovery_blocked(route, probed) or _recovery_blocked(route, observed)
    if blocked:
        return False, blocked
    old = route.settings["tracker_expected_egress_ip"]
    with _WRITE_LOCK:
        rec = _ipupdate_record(state)
        last = rec.get("last_updated_at")
        if last and now - float(last) < ipupdate.MIN_INTERVAL:
            return False, "the allowed IP was changed under an hour ago - at most one change an hour"
        rec["last_attempt_at"] = now
        try:
            new = ipupdate.write(_mam_session(route), observed, transport)
            if route.settings != _route_settings():
                raise ipupdate.UpdateError(
                    ipupdate.REFUSED, "MAM verified the update but the saved route changed")
        except ipupdate.UpdateError as exc:
            rec["last_result"] = {"at": now, "ok": False, "kind": exc.kind, "ip": observed, "message": str(exc), "via": via}
            rec["notified_ip"] = observed
            state.set_meta(META_IPUPDATE, rec)
            log.warning("allowed-IP updater: MAM's dynamic seedbox IP was not set to %s (%s: %s)", observed, exc.kind, exc)
            return False, f"MAM did not take {observed} ({exc.kind}): {exc}"
        state.set_meta("setting_expected_egress", new)
        set_effective(state)
        rec.update({"last_updated_at": now, "old": old, "new": new, "notified_ip": "",
                    "last_result": {"at": now, "ok": True, "kind": "", "ip": new, "message": "", "via": via}})
        state.set_meta(META_IPUPDATE, rec)
    log.info("allowed-IP updater: allowed IP %s -> %s (set on MAM by dynamicSeedbox.php, verified by jsonIp.php; %s)",
             old, new, via)
    return True, f"MAM's seedbox IP is now {new} (was {old}), verified with MAM. The next cycle checks against it."


def use_current_ip(state) -> tuple[bool, str]:
    """Settings > Watcher "Use the current IP". One fresh probe, then ``_judge_drift`` (MAM's
    ``jsonIp.php`` when a MAM session is configured). With a MAM session it sets the seedbox IP on
    MAM, verifies it and re-pins (``_write_and_pin``). Without one, the owner has changed the allowed
    IP on the tracker's website and this only makes the observed IP Audiobind's allowlist pin too;
    nothing is sent to the tracker. Never a home IP, an unverified IP, or twice within an hour.
    (ok, message)."""
    if ip_updater_mode() == "off":
        return False, "the allowed-IP updater is off (IP_UPDATER=off)"
    route = capture_route()
    observed, why = "", ""
    try:
        observed = egress.probe(route.settings["tracker_proxy"])
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the address is unknown
        why = str(exc) or type(exc).__name__
    try:
        route.check()
    except tracker.TrackerRouteChanged as exc:
        return False, str(exc)
    old = route.settings["tracker_expected_egress_ip"]
    rec = _ipupdate_record(state)
    now = time.time()
    probed = observed
    transport = _recovery_transport(route, probed, {}) if _mam_session(route) else None
    outcome, reason, observed, source = _judge_drift(rec, observed, why, now, route, transport)
    if outcome != ipupdate.UPDATE:
        return False, reason
    if source == "MAM":  # a MAM session: set it on MAM, verify, then re-pin
        return _write_and_pin(state, probed, observed, now, "Use the current IP on Settings > Watcher",
                              route, transport)
    state.set_meta("setting_expected_egress", observed)
    set_effective(state)
    rec.update({"last_updated_at": now, "old": old, "new": observed, "notified_ip": ""})
    state.set_meta(META_IPUPDATE, rec)
    log.info("allowed-IP updater: allowed IP %s -> %s (%s; confirmed by hand on Settings > Watcher)", old, observed, reason)
    return True, f"Allowed IP is now {observed} (was {old}). The next cycle checks against it."


# ---------------------------------------------------------------------------- library match
def resolve_library(state) -> list[str]:
    """Every wishlist row whose book is now in the library is taken off the wishlist (a want-list
    holds nothing you already have): a converted (or already-published) book with the same ASIN,
    or the same title and first author after normalisation. Rows that do not match are never
    touched (wanted / not_found / error still need attention). No network, idempotent; returns
    the ASINs it removed."""
    from .discover import LibraryIndex  # discover pulls in the catalog; only needed here

    moved = []
    pending = []
    for row in state.wishlist_all():
        if row["status"] == WISHLIST_IN_LIBRARY:  # left over from before 0.18.0-dev10
            if state.wishlist_remove(row["asin"]):
                moved.append(row["asin"])
                log.info("wishlist: %s is in the library - removed from the wishlist", row["title"])
        else:
            pending.append(row)
    if not pending:
        return moved
    units = [
        u for u in state.series_rows()
        if u.get("status") == "converted" or (u.get("status") == "ready" and u.get("output"))
    ]
    if not units:
        return moved
    index = LibraryIndex(units, set())
    for row in pending:
        owner = index.owner({"asin": row["asin"], "title": row["title"], "authors": row["authors"]})
        if not owner:
            continue
        if state.wishlist_remove(row["asin"]):
            moved.append(row["asin"])
            log.info("wishlist: %s is in the library - removed from the wishlist", row["title"])
    return moved


# ---------------------------------------------------------------------------- the watcher
class _QbitDown(Exception):
    """qBittorrent cannot take a torrent: end the cycle before anything is downloaded."""


class Budget:
    """What the account can afford this cycle: read from ONE qBittorrent request plus the ledger, never
    the tracker. Fails closed - when qBittorrent cannot be read, no freeleech credit is spent."""

    def __init__(self, unsatisfied: int = 0, spent_day: int = 0, unknown: bool = False) -> None:
        self.unsatisfied = unsatisfied
        self.spent_day = spent_day
        self.spent_cycle = 0
        self.unknown = unknown

    @property
    def at_limit(self) -> bool:
        limit = watch_max_unsatisfied()
        return limit > 0 and not self.unknown and self.unsatisfied >= limit

    def may_spend(self) -> tuple[bool, str]:
        """(allowed, why not). The reserve is enforced against the ledger only: the site's own wedge
        count is not read."""
        if self.unknown:
            return False, f"{client_label()} is unreachable, so the freeleech credit budget cannot be checked"
        reserve, per_day, per_cycle = wedge_reserve(), wedge_per_day(), wedge_per_cycle()
        if reserve > 0 and per_day > 0 and self.spent_day >= per_day - reserve:
            return False, "keeping %d freeleech %s in reserve" % (reserve, "credit" if reserve == 1 else "credits")
        if per_cycle > 0 and self.spent_cycle >= per_cycle:
            return False, "%d freeleech %s already spent this cycle" % (self.spent_cycle, "credit" if self.spent_cycle == 1 else "credits")
        if per_day > 0 and self.spent_day >= per_day:
            return False, "%d freeleech %s spent in the last 24 hours" % (self.spent_day, "credit" if self.spent_day == 1 else "credits")
        return True, ""


class Watcher:
    def __init__(self) -> None:
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._client_lock = threading.Lock()
        self._client: tracker.TrackerClient | None = None
        # the client client() built itself, and a digest of the settings it was built from: a saved
        # change to any of them builds a fresh client on next use (a client handed in from outside,
        # as tests do, is never replaced)
        self._built: tracker.TrackerClient | None = None
        self._built_from = ""
        self._paused: bool | None = None
        self.busy = False
        self.next_at = 0.0
        #: set by the daemon only: a settings save that completes the configuration may start the
        #: timed loop (the web page never restarts the daemon, and tests never start a thread this way)
        self.autostart = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, state) -> bool:
        """Start the timed loop - only when the tracker settings are complete and qBittorrent credentials exist.
        Returns False (and starts nothing) otherwise."""
        if not configured():
            log.info("wishlist watcher: off (%s)", disabled_reason())
            return False
        reason = disabled_reason()
        if reason:
            log.warning("wishlist watcher: DISABLED - %s", reason)
            return False
        if self.alive:
            return True
        self._paused = bool(state.get_meta(META_PAUSED, False))  # a pause survives restarts
        saved = state.get_meta(META_BACKOFF) or {}
        if isinstance(saved, dict) and saved.get("until"):
            self.client().backoff.restore(float(saved.get("until") or 0), int(saved.get("failures") or 1))
        last = (state.get_meta(META_LAST_CYCLE) or {}).get("at") or 0
        now = time.time()
        self.next_at = max(now + FIRST_CYCLE_DELAY, float(last) + watch_interval_min() * 60) if last \
            else now + FIRST_CYCLE_DELAY
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(state,), name="wishlist-watcher", daemon=True)
        self._thread.start()
        log.info(
            "wishlist watcher: on - every %d min, at most %d book(s) per cycle, %s %s%s",
            watch_interval_min(), watch_max_per_cycle(), client_label(), client_url(),
            " (paused)" if self._paused else "",
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in (self._thread, self._worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
        self._thread = None

    def client(self, route: TrackerRoute | None = None) -> tracker.TrackerClient:
        """One client for every cycle: one rate limiter and one backoff, whoever triggers.

        The client holds the tracker settings it was built with, so a value saved on the Settings
        page (a new session, URL, cookie name...) builds a fresh one on next use. The rate limiter and
        the backoff carry over: changing a setting never buys an extra request or ends a backoff.
        """
        route = route or capture_route()
        digest = _client_settings_digest(route)
        with self._client_lock:
            if self._client is None:
                self._client = self._built = make_tracker(route)
                self._built_from = digest
            elif self._client is self._built and digest != self._built_from:
                fresh = make_tracker(route)
                fresh.limiter, fresh.backoff = self._client.limiter, self._client.backoff
                self._client = self._built = fresh
                self._built_from = digest
                log.info("wishlist watcher: tracker settings changed - using them from this request on")
            return self._client

    def settings_changed(self, state) -> bool:
        """Called after the Settings page saves. In the daemon, a save that completes the watcher's
        configuration starts the timed loop (no restart needed). Returns True when it started it."""
        if not self.autostart or self.alive or disabled_reason():
            return False
        return self.start(state)

    def _loop(self, state) -> None:
        try:
            with self._cycle_lock:
                self._reconcile_handoffs(state)
        except Exception:
            log.exception("wishlist watcher: startup handoff reconciliation failed")
        while not self._stop.is_set():
            remaining = self.next_at - time.time()
            if remaining > 0:
                self._stop.wait(min(30.0, remaining))
                continue
            if self.paused(state):
                self.next_at = time.time() + watch_interval_min() * 60
                continue
            try:
                self.run_cycle(state, trigger="timer")
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("wishlist watcher: cycle failed")
                self.next_at = time.time() + watch_interval_min() * 60

    # ------------------------------------------------------------------ pause
    def paused(self, state=None) -> bool:
        """The persisted switch (read fresh when a state is given; cached for the cycle loop)."""
        if state is not None:
            self._paused = bool(state.get_meta(META_PAUSED, False))
        return bool(self._paused)

    def set_paused(self, state, value: bool) -> None:
        self._paused = bool(value)
        state.set_meta(META_PAUSED, bool(value))
        log.info("wishlist watcher: %s", "paused" if value else "resumed")

    # ------------------------------------------------------------------ one cycle
    def request_cycle(self, state) -> tuple[bool, str]:
        """"Run a cycle now": one cycle in a worker thread. (started, why not)."""
        reason = disabled_reason()
        if reason:
            return False, f"the watcher is disabled: {reason}"
        if self.paused(state):
            return False, "the watcher is paused - resume it first"
        if self.busy or self._cycle_lock.locked():
            return False, "a cycle is already running"
        self._worker = threading.Thread(target=self._run_quietly, args=(state,), name="watcher-cycle", daemon=True)
        self._worker.start()
        return True, ""

    def _run_quietly(self, state) -> None:
        try:
            self.run_cycle(state, trigger="manual")
        except Exception:  # noqa: BLE001 - a worker thread has nobody to raise to
            log.exception("wishlist watcher: manual cycle failed")

    def run_cycle(self, state, trigger: str = "timer") -> dict:
        """One cycle, start to end, on the calling thread. Returns its summary."""
        if not self._cycle_lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.busy = True
        try:
            return self._cycle(state, trigger)
        finally:
            self.busy = False
            self._cycle_lock.release()

    @staticmethod
    def _handoff_client_key() -> str:
        return hashlib.sha256(f"{client_kind()}\n{client_url()}".encode()).hexdigest()

    def _reconcile_handoffs(self, state) -> None:
        for asin, intent in state.tracker_handoffs().items():
            digest = intent.get("hash", "")
            reason = "the tracker download may have completed and spent a freeleech credit"
            if digest and intent["client"] == self._handoff_client_key():
                client = make_client()
                try:
                    client.login()
                    if digest in client.progress([digest]):
                        state.settle_tracker_handoff(
                            asin, WISHLIST_DOWNLOADING,
                            "Recovered handoff: torrent found in download client.", digest,
                        )
                        continue
                    reason = "the torrent was not found in the download client"
                except ClientError as exc:
                    reason = f"the download client could not confirm the torrent ({exc})"
                finally:
                    client.logout()
            elif digest:
                reason = "the download client settings changed since the handoff"
            note = f"Tracker torrent #{intent['tid']} needs owner review before retrying: {reason}."
            state.settle_tracker_handoff(asin, WISHLIST_ERROR, note)

    def _cycle(self, state, trigger: str) -> dict:
        reason = disabled_reason()
        if reason:
            return {"skipped": reason}
        self._reconcile_handoffs(state)
        if self.paused(state):
            return {"skipped": "paused"}
        route = capture_route()
        started = time.time()
        summary = {"at": started, "trigger": trigger, "watcher_dry_run": CONTROL.watcher_dry_run, "searched": 0, "sent": 0,
                   "would_send": 0, "not_found": 0, "skipped_budget": 0, "errors": 0, "resolved": 0, "error": ""}
        try:
            summary["resolved"] = len(resolve_library(state))
        except Exception:  # noqa: BLE001 - a library-match problem must not stop the search
            log.exception("wishlist watcher: library match failed")
        # Local bookkeeping first: this talks to qBittorrent only, never the tracker, so the egress guard
        # (which protects tracker requests) has no say. A VPN that is down is exactly when someone
        # is looking at the page and wants finished torrents to read "downloaded".
        try:
            summary["downloaded"] = self._mark_downloaded(state)
        except Exception:  # noqa: BLE001 - a progress check must never end the cycle
            log.exception("wishlist watcher: download progress check failed")
        verdict, reason, observed = self._egress_guard(state, route)
        summary["egress_guard"] = verdict
        if observed:
            summary["egress_ip"] = observed
        mode = ip_updater_mode()
        summary.update(updater_attempted=False, updater_result="updater is off")
        if mode != "off":
            summary["updater_result"] = "no verified drift"
            if verdict == "changed":
                summary["updater_result"] = reason
            elif verdict != "off":
                try:
                    summary["updater_result"] = self._ip_notice(
                        state, verdict, observed, route, summary, mode)
                except Exception:  # noqa: BLE001 - the updater must never break a cycle
                    summary["updater_result"] = "drift check failed"
                    log.exception("allowed-IP updater: drift check failed")
        if verdict in ("blocked", "unknown", "changed"):
            return self._refuse(state, summary, reason)
        summary["normal_tracker_blocked"] = False
        client = self.client(route)
        holder: dict = {}
        budget = self._budget(state, holder)
        try:
            wait = client.backoff.remaining()
            if wait > 0:
                summary["error"] = f"tracker backoff - next request allowed in {int(wait) + 1}s; nothing searched"
            else:
                rows = [] if self._holding(summary) or self._unsatisfied_hold(summary, budget) \
                    else state.wishlist_due(watch_max_per_cycle())
                for row in rows:
                    if self._stop.is_set() or self.paused():
                        break
                    try:
                        with route.checked():
                            outcome = self._one(
                                state, client, row, summary["watcher_dry_run"], holder, budget)
                    except tracker.TrackerRouteChanged as exc:
                        summary["egress_guard"] = "changed"
                        summary["normal_tracker_blocked"] = True
                        summary["error"] = str(exc)
                        break
                    except tracker.TrackerError as exc:
                        summary["error"] = str(exc)
                        break
                    except _QbitDown as exc:
                        summary["error"] = str(exc)
                        break
                    finally:
                        summary["searched"] += 1 if holder.pop("searched", False) else 0
                        summary["errors"] += 1 if holder.pop("row_error", False) else 0
                    summary[outcome] = summary.get(outcome, 0) + 1
        finally:
            qb = holder.get("qbit")
            if qb is not None:
                qb.logout()
            state.set_meta(META_BACKOFF, {"until": client.backoff.until, "failures": client.backoff.failures})
        summary["seconds"] = round(time.time() - started, 1)
        state.set_meta(META_LAST_CYCLE, summary)
        if summary["error"]:
            state.set_meta(META_LAST_ERROR, {"at": time.time(), "message": summary["error"]})
            log.warning("wishlist watcher: %s", summary["error"])
        self.next_at = time.time() + watch_interval_min() * 60
        skipped = summary["skipped_budget"]
        log.info(
            "wishlist watcher: %s cycle%s - %d searched, %d sent to %s, %d would be sent (dry run), "
            "%d not found%s, %d error(s), %d now in the library",
            trigger, " [dry run]" if summary["watcher_dry_run"] else "", summary["searched"], summary["sent"],
            client_label(),
            summary["would_send"], summary["not_found"],
            ", %d skipped for the wedge budget" % skipped if skipped else "", summary["errors"], summary["resolved"],
        )
        return summary

    def _holding(self, summary: dict) -> bool:
        """True when WATCH_MAX_INCOMPLETE torrents of the category are still below DONE_PROGRESS: the
        cycle then searches and adds nothing (one log line). A dry run adds nothing anyway, 0 turns
        the check off, and a qBittorrent failure is logged once and the cycle goes on as before."""
        limit = watch_max_incomplete()
        if limit <= 0 or summary.get("watcher_dry_run"):
            return False
        client = make_client()
        try:
            count = client.incomplete(qbit_category(), DONE_PROGRESS)
        except ClientError as exc:
            log.warning("wishlist watcher: could not count incomplete torrents in %s (%s) - no limit applied this cycle",
                        client_label(), exc)
            return False
        finally:
            client.logout()
        if count < limit:
            return False
        summary["held"] = count
        log.info("wishlist watcher: %d torrent(s) in category %r still incomplete, limit %d - holding this cycle, nothing added",
                 count, qbit_category(), limit)
        return True

    def _budget(self, state, holder: dict) -> Budget:
        """The account budget for this cycle: one qBittorrent read plus the ledger. A qBittorrent that
        cannot be read marks the budget unknown (no credit is spent; rows that need none go on)."""
        spent_day = state.wedges_spent_since(time.time() - 86400)
        hashes = set(state.wishlist_snatch_hashes())
        client = make_client()
        try:
            snatches = client.snatch_state(qbit_category())
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, the budget cannot be trusted
            log.warning("wishlist watcher: could not read snatch state from %s (%s) - no freeleech "
                        "credit will be spent this cycle", client_label(), exc)
            return Budget(spent_day=spent_day, unknown=True)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass
        unsatisfied = sum(
            1 for h in hashes
            if h in snatches and (snatches[h][0] < 1.0 or snatches[h][1] < 1.0)
        )
        return Budget(unsatisfied=unsatisfied, spent_day=spent_day)

    def _unsatisfied_hold(self, summary: dict, budget: Budget) -> bool:
        """True when WATCH_MAX_UNSATISFIED snatches are still unsatisfied (progress or ratio below 1):
        nothing is searched or added this cycle. Independent of _holding()."""
        if not budget.at_limit or summary.get("watcher_dry_run"):
            return False
        summary["held_unsatisfied"] = budget.unsatisfied
        log.info("wishlist watcher: %d snatch(es) still unsatisfied, limit %d - holding this cycle, nothing added",
                 budget.unsatisfied, watch_max_unsatisfied())
        return True

    def _mark_downloaded(self, state) -> int:
        """After the search phase: ``downloading`` rows whose torrent is finished become
        ``downloaded``. Read-only against qBittorrent; one request for the whole batch. A
        qBittorrent failure is logged once and every row is left as it is."""
        rows = [r for r in state.wishlist_all()
                if r["status"] == WISHLIST_DOWNLOADING and (r.get("torrent_hash") or "").strip()]
        if not rows:
            return 0
        client = make_client()
        try:
            progress = client.progress([r["torrent_hash"] for r in rows])
        except ClientError as exc:
            log.warning("wishlist watcher: could not read download progress from %s (%s) - rows left as they are",
                        client_label(), exc)
            return 0
        finally:
            client.logout()
        moved = 0
        for row in rows:
            done = progress.get(row["torrent_hash"].strip().lower())
            if done is not None and done >= DONE_PROGRESS and state.wishlist_set_status(
                row["asin"], WISHLIST_DOWNLOADED, expect=WISHLIST_DOWNLOADING, note=DOWNLOADED_NOTE,
            ):
                moved += 1
                log.info("wishlist watcher: %s (%s) finished downloading", row["asin"], row["title"])
        return moved

    def _egress_guard(self, state, route: TrackerRoute | None = None) -> tuple[str, str, str]:
        """(verdict, reason, observed ip), before any tracker request. Not armed = no probe at all."""
        route = route or capture_route()
        home = route.settings["tracker_home_ip"]
        expected = route.settings["tracker_expected_egress_ip"]
        proxy = route.settings["tracker_proxy"]
        try:
            route.check()
        except tracker.TrackerRouteChanged as exc:
            return "changed", str(exc), ""
        if not home and not expected:
            state.set_meta(META_EGRESS, {})
            log.info("tracker route %s: %s, IP guard off", route.id, "proxy" if proxy else "direct")
            return "off", egress.judge("", "")[1], ""
        observed, why = "", ""
        try:
            observed = egress.probe(proxy)
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, the tracker stays untouched
            why = str(exc) or type(exc).__name__
        try:
            route.check()
        except tracker.TrackerRouteChanged as exc:
            return "changed", str(exc), observed
        verdict, reason = egress.judge(home, observed, expected)
        if verdict == "unknown":
            reason = f"the egress IP could not be verified ({why})"
        elif verdict == "blocked":
            reason += ". Update the allowed address in Settings, or check your VPN or tracker proxy"
        else:
            log.info("tracker egress: %s (route %s approved via %s, home %s, pinned %s)",
                     observed, route.id, "proxy" if proxy else "direct", home or "unset", expected or "unset")
        state.set_meta(META_EGRESS, {"ip": observed, "at": time.time(), "reason": reason if verdict == "unknown" else ""})
        return verdict, reason, observed

    def _ip_notice(self, state, verdict: str, observed: str, route: TrackerRoute,
                   report: dict, mode: str) -> str:
        """Report a drift or try recovery without changing the normal tracker verdict."""
        probed = observed
        rec = _ipupdate_record(state)
        now = time.time()
        transport = _recovery_transport(route, probed, report) if _mam_session(route) else None
        outcome, why, observed, source = _judge_drift(
            rec, observed, "the egress probe failed" if verdict == "unknown" else "", now, route, transport)
        if outcome == ipupdate.UPDATE:
            auto = mode == "auto"
            attempted = float(rec.get("last_attempt_at") or 0)
            if auto and source == "MAM" and now - attempted >= ipupdate.MIN_INTERVAL:
                _, result = _write_and_pin(state, probed, observed, now, "IP_UPDATER=auto", route, transport)
                return result

            suffix = ""
            if auto and source != "MAM":
                suffix = " (IP_UPDATER=auto: no MyAnonamouse session is configured, so nothing was changed automatically)"
            elif auto:
                suffix = " (IP_UPDATER=auto: the last attempt failed under an hour ago, so it is not retried yet)"
            if rec.get("notified_ip") != observed:
                log.warning(
                    "allowed-IP updater: %s. Set the tracker account's allowed IP to %s "
                    "on the tracker's website, "
                    "then press Use the current IP on Settings > Watcher%s", why, observed, suffix)
                rec["notified_ip"] = observed
                state.set_meta(META_IPUPDATE, rec)
            return why + suffix
        if outcome == ipupdate.NOOP and rec.get("notified_ip"):
            rec["notified_ip"] = ""  # back in step: a later drift is reported again
            state.set_meta(META_IPUPDATE, rec)
        return why

    def _refuse(self, state, summary: dict, reason: str) -> dict:
        """Keep the refusal and any recovery attempt visible as separate outcomes."""
        summary["error"] = reason
        summary["normal_tracker_blocked"] = True
        summary["seconds"] = round(time.time() - summary["at"], 1)
        state.set_meta(META_LAST_CYCLE, summary)
        state.set_meta(META_LAST_ERROR, {"at": time.time(), "message": reason})
        log.error("wishlist watcher: egress guard (%s) blocked tracker searches and downloads. "
                  "%s. Recovery %s - %s",
                  summary["egress_guard"], reason,
                  "attempted" if summary["updater_attempted"] else "skipped", summary["updater_result"])
        self.next_at = time.time() + watch_interval_min() * 60
        return summary

    def _qbit(self, holder: dict) -> DownloadClient:
        """The download client (qBittorrent by default), logged in once per cycle - before the first
        tracker download."""
        if "qbit" not in holder:
            holder["handoff_client"] = self._handoff_client_key()
            client = make_client()
            try:
                client.login()
            except ClientError as exc:
                raise _QbitDown(f"{client_label()} is not available ({exc}) - nothing downloaded") from None
            holder["qbit"] = client
        return holder["qbit"]

    def _one(self, state, client: tracker.TrackerClient, row: dict, dry_run: bool, holder: dict,
             budget: Budget | None = None) -> str:
        """Search one row and act on it. Returns the summary key of what happened."""
        asin, title = row["asin"], row["title"]
        author = row["authors"][0] if row.get("authors") else ""
        holder["searched"] = True
        try:
            torrents = client.search(row.get("search_title") or title, author)
        except tracker.TrackerRouteChanged:
            holder["searched"] = False
            raise
        pick, why = tracker.select_candidate(row, torrents, vip=bool(_setting("tracker_vip")), can_spend=client.can_spend)
        when = _stamp()
        if pick is None:
            note = f"not found on the tracker (checked {when}): {why}"
            misses = state.wishlist_record_check(asin, note, miss=True)
            if misses is not None and misses >= MISS_LIMIT:
                state.wishlist_set_status(
                    asin, WISHLIST_NOT_FOUND, expect=WISHLIST_WANTED, checked=True,
                    note=f"not found on the tracker in {misses} searches (last checked {when}): {why} - press Retry to search again",
                )
            log.info("wishlist watcher: %s (%s) not found on the tracker - %s", asin, title, why)
            return "not_found"

        use_credit, credit_why = tracker.freeleech_decision(pick.raw, client.can_spend)
        decision = f"{why}; {credit_why}"
        # only a torrent that would cost a credit consults the budget; one that is already free never does
        allowed, refusal = (budget or Budget()).may_spend() if use_credit else (True, "")
        if dry_run:
            if not allowed:
                state.wishlist_record_check(asin, f"dry run ({when}): would NOT spend a credit - {refusal}", miss=False)
                log.info("wishlist watcher [dry run]: %s (%s) - would not spend a credit: %s", asin, title, refusal)
                return "skipped_budget"
            state.wishlist_record_check(asin, f"dry run ({when}): would download {decision}", miss=False)
            log.info("wishlist watcher [dry run]: %s (%s) - would download %s%s", asin, title, decision,
                     " (would spend 1 wedge)" if use_credit else "")
            return "would_send"
        if not allowed:
            state.wishlist_record_check(asin, f"not downloaded ({when}): {refusal}", miss=False)
            log.info("wishlist watcher: %s (%s) - no credit spent: %s", asin, title, refusal)
            return "skipped_budget"

        current = state.wishlist_get(asin)
        if not current or current["status"] != WISHLIST_WANTED:
            return "skipped"  # removed or changed by a click while this cycle ran
        qb = self._qbit(holder)  # before the download: a dead qBittorrent costs no freeleech credit
        filename, category = f"tracker-{pick.tid}.torrent", qbit_category()
        intent = {"tid": pick.tid, "use_credit": use_credit, "client": holder["handoff_client"]}
        if not state.begin_tracker_handoff(asin, intent):
            return "skipped"
        log.info("wishlist watcher: %s (%s) - downloading %s", asin, title, decision)
        try:
            torrent = client.download(pick.tid, use_credit)
        except tracker.TrackerRouteChanged:
            state.settle_tracker_handoff(asin, None)
            raise
        except tracker.TrackerError as exc:
            state.settle_tracker_handoff(
                asin, WISHLIST_ERROR,
                f"Download of tracker torrent #{pick.tid} failed ({when}): {exc}. "
                "Review before retrying: a freeleech credit may have been spent.",
            )
            holder["row_error"] = True
            raise
        intent.update(hash=qbit.info_hash(torrent), filename=filename, category=category,
                      spent_at=time.time() if use_credit else 0)
        state.prepare_tracker_add(asin, intent)
        if use_credit and budget is not None:
            budget.spent_cycle += 1
        try:
            digest = qb.add_torrent(torrent, filename, category)
        except ClientError as exc:
            digest = intent["hash"]
            try:
                present = digest and digest in qb.progress([digest])
            except ClientError:
                present = False
            if not present:
                state.settle_tracker_handoff(
                    asin, WISHLIST_ERROR,
                    f"Fetched tracker torrent #{pick.tid} ({credit_why}) but "
                    f"{client_label()} did not take it ({when}): {exc}. "
                    "Review the download client before retrying.",
                )
                holder["row_error"] = True
                raise _QbitDown(f"{client_label()} did not take a torrent ({exc})") from None
        state.settle_tracker_handoff(
            asin, WISHLIST_DOWNLOADING, f"sent to {client_label()} {when}: {decision}",
            digest or intent["hash"],
        )
        log.info("wishlist watcher: %s (%s) sent to %s (hash %s)", asin, title, client_label(), digest or "unknown")
        return "sent"

    # ------------------------------------------------------------------ status
    @staticmethod
    def wedge_status(state) -> dict:
        """The wedge line's facts: ledger only, no network."""
        per_day = wedge_per_day()
        spent = state.wedges_spent_since(time.time() - 86400)
        return {
            "can_spend": bool(_setting("tracker_freeleech_param")),
            "spent_day": spent,
            "per_day": per_day,
            "left": max(0, per_day - spent) if per_day else None,
            "reserve": wedge_reserve(),
            "per_cycle": wedge_per_cycle(),
            "max_unsatisfied": watch_max_unsatisfied(),
        }

    def status(self, state) -> dict:
        """What the Settings page and GET /api/watcher show. Never a secret."""
        reason = disabled_reason()
        paused = self.paused(state)
        last = state.get_meta(META_LAST_CYCLE) or {}
        error = state.get_meta(META_LAST_ERROR) or {}
        backoff = self._client.backoff.remaining() if self._client is not None else 0.0
        if not backoff:
            saved = state.get_meta(META_BACKOFF) or {}
            until = float(saved.get("until") or 0) if isinstance(saved, dict) else 0.0
            backoff = max(0.0, until - time.time())
        home = _setting("tracker_home_ip")
        expected = _setting("tracker_expected_egress_ip")
        armed = bool(home or expected)
        rec = state.get_meta(META_EGRESS) or {}
        rec = rec if isinstance(rec, dict) else {}
        seen = str(rec.get("ip") or "")
        if not armed:
            guard, guard_reason = "off", egress.judge("", "")[1]
        elif seen:
            guard, guard_reason = egress.judge(home, seen, expected)
        else:
            guard, guard_reason = "unknown", str(rec.get("reason") or "not checked yet")
        extra = {}
        if ip_updater_mode() != "off":  # off: the answer is exactly what it was before the updater
            upd = _ipupdate_record(state)
            outcome, why = ipupdate.decide(seen, expected, home, upd.get("last_updated_at"), time.time(),
                                           probe_error="" if seen else str(rec.get("reason") or "not checked yet"))
            extra["ip_updater"] = {
                "mode": ip_updater_mode(),
                "can_write": bool(_mam_session()),  # a MAM session: the button and auto write to MAM
                "last_result": dict(upd.get("last_result") or {}),
                "decision": outcome,
                "reason": why,
                "last_updated_at": upd.get("last_updated_at"),
                "last_old": str(upd.get("old") or ""),
                "last_new": str(upd.get("new") or ""),
            }
        return {
            **extra,
            "egress_ip": seen if armed else "",
            "egress_checked_at": rec.get("at") if armed else None,
            "egress_guard": guard,
            "egress_reason": guard_reason,
            "egress_home_ip": home,
            "egress_expected": expected,
            "configured": configured(),
            "missing": missing_settings(),
            "tracker_host": tracker_host(),
            "enabled": not reason,
            "reason": reason,
            "running": self.alive,
            "paused": paused,
            "watcher_dry_run": CONTROL.watcher_dry_run,
            "busy": self.busy,
            "interval_min": watch_interval_min(),
            "max_per_cycle": watch_max_per_cycle(),
            "qbit_url": qbit_url(),
            "qbit_category": qbit_category(),
            "max_incomplete": watch_max_incomplete(),
            "wedges": self.wedge_status(state),
            "qbit_credentials": bool(_setting("qbit_user") and _setting("qbit_pass")),
            "download_client": client_kind(),
            "client_label": client_label(),
            "client_url": client_url(),
            "last_cycle": last if isinstance(last, dict) else {},
            "next_cycle_at": self.next_at if self.alive and not paused else None,
            "last_error": error if isinstance(error, dict) else {},
            "backoff_seconds": round(backoff),
            "counts": state.wishlist_status_counts(),
        }


WATCHER = Watcher()

"""Phase 3: the wishlist watcher - wanted books from MAM, through qBittorrent, into ingest.

Off unless MAM_ID is set: with it unset no thread starts and nothing touches the network.

One cycle, every WATCH_INTERVAL_MIN (or on "Run a cycle now"):

1. Wishlist rows already in the library are marked ``in_library`` (no network).
2. Up to WATCH_MAX_PER_CYCLE ``wanted`` rows - never-checked first, oldest added first, then
   the least recently checked - are each searched on MAM (mam.MamClient: one request at a
   time, >= 3 s apart, backoff on any failure).
3. No result good enough (mam.select_candidate): the row stays ``wanted`` with a "not found
   on MAM (checked <date>)" note and is searched again in a later cycle; after MISS_LIMIT
   misses in a row it becomes ``not_found`` until you press Retry. Nothing is ever forced.
4. A match: the wedge decision (mam.wedge_decision) is written into the row's note. In dry
   run that is all - nothing is downloaded. Otherwise qBittorrent is logged into FIRST (a
   qBittorrent that is down must never cost a wedge), the .torrent is downloaded from MAM and
   handed to qBittorrent, and the row becomes ``downloading``. The ingest conveyor does the
   rest; the row becomes ``in_library`` when the book shows up converted.

Failure rules - a wedge is never refunded, so nothing after a download request is retried
on its own: a failed download or a qBittorrent refusal turns the row into ``error`` (with the
reason) and only a Retry click puts it back. Any MAM failure ends the cycle at once.
"""

from __future__ import annotations

import logging
import threading
import time

from . import egress, mam, qbit
from .config import effective_val, watch_interval_min, watch_max_per_cycle
from .control import CONTROL
from .state import (
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
#: unit statuses that mean the book is in the library: converted here, or found already
#: published ("ready" with an output file - a dry-run "ready" has none)
PUBLISHED = ("converted", "ready")

META_PAUSED = "watcher_paused"
META_LAST_CYCLE = "watcher_last_cycle"
META_LAST_ERROR = "watcher_last_error"
META_BACKOFF = "watcher_backoff"
META_EGRESS = "watcher_egress"


def _stamp(at: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() if at is None else at))


def _setting(name: str) -> str:
    """A watcher setting through the effective-config getter (env default unless overridden)."""
    return str(effective_val(name) or "")


def configured() -> bool:
    """MAM_ID set: the owner wants the watcher."""
    return bool(_setting("mam_id"))


def disabled_reason() -> str:
    """Why the watcher will not run ("" = it can)."""
    if not _setting("mam_id"):
        return "MAM_ID is not set"
    if not _setting("qbit_user") or not _setting("qbit_pass"):
        return "QBIT_USER / QBIT_PASS are not set - the watcher has nowhere to send a torrent"
    return ""


def qbit_url() -> str:
    return _setting("qbit_url") or "http://localhost:8083"


# the network, behind two factories tests replace
def make_mam() -> mam.MamClient:
    return mam.MamClient(_setting("mam_id"), proxy=_setting("mam_proxy"),
                         user_agent=_setting("mam_user_agent"))


def make_qbit() -> qbit.QbitClient:
    return qbit.QbitClient(qbit_url(), _setting("qbit_user"), _setting("qbit_pass"))


# ---------------------------------------------------------------------------- library match
def resolve_library(state) -> list[str]:
    """Every wishlist row whose book is now in the library becomes ``in_library``: a converted
    (or already-published) book with the same ASIN, or the same title and first author after
    normalisation. No network, idempotent; returns the ASINs it moved."""
    from .discover import LibraryIndex  # discover pulls in the catalog; only needed here

    pending = [r for r in state.wishlist_all() if r["status"] != WISHLIST_IN_LIBRARY]
    if not pending:
        return []
    units = [
        u for u in state.series_rows()
        if u.get("status") == "converted" or (u.get("status") == "ready" and u.get("output"))
    ]
    if not units:
        return []
    index = LibraryIndex(units, set())
    moved = []
    for row in pending:
        owner = index.owner({"asin": row["asin"], "title": row["title"], "authors": row["authors"]})
        if not owner:
            continue
        how = "same ASIN" if owner.get("asin") == row["asin"] or owner.get("asin_override") == row["asin"] \
            else "same title and author"
        note = f"in your library since {_stamp()} ({how})"
        if state.wishlist_set_status(row["asin"], WISHLIST_IN_LIBRARY, note=note):
            moved.append(row["asin"])
            log.info("wishlist: %s (%s) is in the library now", row["asin"], row["title"])
    return moved


# ---------------------------------------------------------------------------- the watcher
class _QbitDown(Exception):
    """qBittorrent cannot take a torrent: end the cycle before anything is downloaded."""


class Watcher:
    def __init__(self) -> None:
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._client: mam.MamClient | None = None
        self._paused: bool | None = None
        self.busy = False
        self.next_at = 0.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, state) -> bool:
        """Start the timed loop - only when MAM_ID is set and qBittorrent credentials exist.
        Returns False (and starts nothing) otherwise."""
        if not configured():
            log.info("wishlist watcher: off (MAM_ID is not set)")
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
            "wishlist watcher: on - every %d min, at most %d book(s) per cycle, qBittorrent %s%s",
            watch_interval_min(), watch_max_per_cycle(), qbit_url(), " (paused)" if self._paused else "",
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in (self._thread, self._worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)
        self._thread = None

    def client(self) -> mam.MamClient:
        """One client for every cycle: one rate limiter and one backoff, whoever triggers."""
        if self._client is None:
            self._client = make_mam()
        return self._client

    def _loop(self, state) -> None:
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

    def _cycle(self, state, trigger: str) -> dict:
        reason = disabled_reason()
        if reason:
            return {"skipped": reason}
        if self.paused(state):
            return {"skipped": "paused"}
        started = time.time()
        summary = {"at": started, "trigger": trigger, "watcher_dry_run": CONTROL.watcher_dry_run, "searched": 0, "sent": 0,
                   "would_send": 0, "not_found": 0, "errors": 0, "resolved": 0, "error": ""}
        try:
            summary["resolved"] = len(resolve_library(state))
        except Exception:  # noqa: BLE001 - a library-match problem must not stop the search
            log.exception("wishlist watcher: library match failed")
        verdict, reason, observed = self._egress_guard(state)
        summary["egress_guard"] = verdict
        if observed:
            summary["egress_ip"] = observed
        if verdict in ("blocked", "unknown"):
            return self._refuse(state, summary, reason, trigger)
        client = self.client()
        holder: dict = {}
        try:
            wait = client.backoff.remaining()
            if wait > 0:
                summary["error"] = f"MAM backoff - next request allowed in {int(wait) + 1}s; nothing searched"
            else:
                rows = state.wishlist_due(watch_max_per_cycle())
                for row in rows:
                    if self._stop.is_set() or self.paused():
                        break
                    try:
                        outcome = self._one(state, client, row, summary["watcher_dry_run"], holder)
                    except mam.MamError as exc:
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
        log.info(
            "wishlist watcher: %s cycle%s - %d searched, %d sent to qBittorrent, %d would be sent (dry run), "
            "%d not found, %d error(s), %d now in the library",
            trigger, " [dry run]" if summary["watcher_dry_run"] else "", summary["searched"], summary["sent"],
            summary["would_send"], summary["not_found"], summary["errors"], summary["resolved"],
        )
        return summary

    def _egress_guard(self, state) -> tuple[str, str, str]:
        """(verdict, reason, observed ip), before any MAM request. Not armed = no probe at all."""
        home = _setting("mam_home_wan_ip")
        expected = _setting("mam_expected_egress_ip")
        if not home and not expected:
            state.set_meta(META_EGRESS, {})
            return "off", egress.judge("", "")[1], ""
        observed, why = "", ""
        try:
            observed = egress.probe(_setting("mam_proxy"))
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, MAM stays untouched
            why = str(exc) or type(exc).__name__
        verdict, reason = egress.judge(home, observed, expected)
        if verdict == "unknown":
            reason = f"the egress IP could not be verified ({why}) - the watcher will not contact MAM until it can"
        elif verdict == "blocked":
            reason += " - the watcher will not contact MAM from here. Update the allowed address in Settings, or start the VPN container / fix MAM_PROXY"
        else:
            log.info("MAM egress: %s", observed)
        state.set_meta(META_EGRESS, {"ip": observed, "at": time.time(), "reason": reason if verdict == "unknown" else ""})
        return verdict, reason, observed

    def _refuse(self, state, summary: dict, reason: str, trigger: str) -> dict:
        """End the cycle at the guard: nothing was requested from MAM."""
        summary["error"] = reason
        summary["seconds"] = round(time.time() - summary["at"], 1)
        state.set_meta(META_LAST_CYCLE, summary)
        state.set_meta(META_LAST_ERROR, {"at": time.time(), "message": reason})
        log.error("wishlist watcher: egress guard (%s) - %s; no request was sent to MAM", summary["egress_guard"], reason)
        self.next_at = time.time() + watch_interval_min() * 60
        return summary

    def _qbit(self, holder: dict) -> qbit.QbitClient:
        """qBittorrent, logged in once per cycle - before the first MAM download."""
        if "qbit" not in holder:
            client = make_qbit()
            try:
                client.login()
            except qbit.QbitError as exc:
                raise _QbitDown(f"qBittorrent is not available ({exc}) - nothing downloaded") from None
            holder["qbit"] = client
        return holder["qbit"]

    def _one(self, state, client: mam.MamClient, row: dict, dry_run: bool, holder: dict) -> str:
        """Search one row and act on it. Returns the summary key of what happened."""
        asin, title = row["asin"], row["title"]
        author = row["authors"][0] if row.get("authors") else ""
        holder["searched"] = True
        torrents = client.search(title, author)  # a MamError ends the cycle; the row is untouched
        pick, why = mam.select_candidate(row, torrents, vip=bool(_setting("mam_vip")))
        when = _stamp()
        if pick is None:
            note = f"not found on MAM (checked {when}): {why}"
            misses = state.wishlist_record_check(asin, note, miss=True)
            if misses is not None and misses >= MISS_LIMIT:
                state.wishlist_set_status(
                    asin, WISHLIST_NOT_FOUND, expect=WISHLIST_WANTED, checked=True,
                    note=f"not found on MAM in {misses} searches (last checked {when}): {why} - press Retry to search again",
                )
            log.info("wishlist watcher: %s (%s) not found on MAM - %s", asin, title, why)
            return "not_found"

        use_wedge, wedge_why = mam.wedge_decision(pick.raw)
        decision = f"{why}; {wedge_why}"
        if dry_run:
            state.wishlist_record_check(asin, f"dry run ({when}): would download {decision}", miss=False)
            log.info("wishlist watcher [dry run]: %s (%s) - would download %s", asin, title, decision)
            return "would_send"

        current = state.wishlist_get(asin)
        if not current or current["status"] != WISHLIST_WANTED:
            return "skipped"  # removed or changed by a click while this cycle ran
        qb = self._qbit(holder)  # before the download: a dead qBittorrent costs no wedge
        log.info("wishlist watcher: %s (%s) - downloading %s", asin, title, decision)
        try:
            torrent = client.download(pick.tid, use_wedge)
        except mam.MamError as exc:
            state.wishlist_set_status(
                asin, WISHLIST_ERROR, expect=WISHLIST_WANTED, checked=True, mam_tid=pick.tid,
                note=(f"download of MAM #{pick.tid} failed ({when}): {exc}. Decision was: {wedge_why}. "
                      "Not retried on its own - a wedge may have been spent; press Retry to search again."),
            )
            holder["row_error"] = True
            raise
        try:
            digest = qb.add_torrent(torrent, f"mam-{pick.tid}.torrent")
        except qbit.QbitError as exc:
            state.wishlist_set_status(
                asin, WISHLIST_ERROR, expect=WISHLIST_WANTED, checked=True, mam_tid=pick.tid,
                note=(f"fetched MAM #{pick.tid} ({wedge_why}) but qBittorrent did not take it ({when}): {exc}. "
                      "Press Retry to search again."),
            )
            holder["row_error"] = True
            raise _QbitDown(f"qBittorrent did not take a torrent ({exc})") from None
        state.wishlist_set_status(
            asin, WISHLIST_DOWNLOADING, expect=WISHLIST_WANTED, checked=True, mam_tid=pick.tid,
            torrent_hash=digest, note=f"sent to qBittorrent {when}: {decision}",
        )
        log.info("wishlist watcher: %s (%s) sent to qBittorrent (hash %s)", asin, title, digest or "unknown")
        return "sent"

    # ------------------------------------------------------------------ status
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
        home = _setting("mam_home_wan_ip")
        expected = _setting("mam_expected_egress_ip")
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
        return {
            "egress_ip": seen if armed else "",
            "egress_checked_at": rec.get("at") if armed else None,
            "egress_guard": guard,
            "egress_reason": guard_reason,
            "egress_home_ip": home,
            "egress_expected": expected,
            "configured": configured(),
            "enabled": not reason,
            "reason": reason,
            "running": self.alive,
            "paused": paused,
            "watcher_dry_run": CONTROL.watcher_dry_run,
            "busy": self.busy,
            "interval_min": watch_interval_min(),
            "max_per_cycle": watch_max_per_cycle(),
            "qbit_url": qbit_url(),
            "qbit_credentials": bool(_setting("qbit_user") and _setting("qbit_pass")),
            "last_cycle": last if isinstance(last, dict) else {},
            "next_cycle_at": self.next_at if self.alive and not paused else None,
            "last_error": error if isinstance(error, dict) else {},
            "backoff_seconds": round(backoff),
            "counts": state.wishlist_status_counts(),
        }


WATCHER = Watcher()

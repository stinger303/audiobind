"""Shared run controls, driven by the status page.

The daemon asks this object whether it may start another book, whether writes are allowed, and
the web handlers flip those switches at runtime. Nothing here touches the filesystem -
persistence lives in State.set_meta() ("paused", "dry_run", "watcher_dry_run"), so a restart keeps your choices.

Three ways to intervene, and they are not the same thing:

* **pause**   - finish the book being converted, then start nothing new.
* **stop**    - abort the current ffmpeg now. The partial file never leaves the work directory,
                so nothing corrupt reaches the library, and the book is retried on the next scan.
* **dry run** - decide everything, write nothing.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from .config import CONFIG


class Stopped(Exception):
    """Raised when the operator aborts the conversion that is currently running."""


#: at most one encode-progress heartbeat this often (each one is a small DB write)
HEARTBEAT_EVERY_S = 30.0


class Control:
    def __init__(self) -> None:
        self._scan = threading.Event()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        #: None = use the DRY_RUN env default; True/False = the user chose in the web UI
        self._dry_run: bool | None = None
        #: the acquisition watcher's own switch, never read by the pipeline; None = WATCH_DRY_RUN default
        self._watcher_dry_run: bool | None = None
        self.current_unit: str = ""
        #: the catalog title of the book being encoded ("" = name it from current_unit)
        self.current_title: str = ""
        #: live encode progress: {"percent", "elapsed", "eta", "speed"}
        self.progress: dict = {}
        #: live scan progress for the status page's busy indicator
        self._scanning = False
        self._scan_done = 0
        self._scan_total = 0
        self._scan_force = False
        #: manually requested conversions waiting for the one-at-a-time encoder
        self._queue: list[str] = []
        self._deleting: set[str] = set()
        self._auditing: set[str] = set()
        #: called (throttled) from encode progress so a long encode keeps the healthcheck fresh
        self._heartbeat = None
        self._heartbeat_at = 0.0

    # ---------------------------------------------------------------- pause
    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self._stop.clear()
        self.request_scan()  # resuming should not wait for the next interval

    # ---------------------------------------------------------------- stop
    def stop(self) -> None:
        """Abort the running conversion immediately, then hold."""
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.pause()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def clear_stop(self) -> None:
        self._stop.clear()

    # ---------------------------------------------------------------- current book
    @property
    def running(self) -> bool:
        return bool(self.current_unit)

    def begin_unit(self, name: str, title: str = "") -> None:
        self.current_unit = name
        self.current_title = title or ""
        self.progress = {}
        self._stop.clear()
        self.dequeue(name)

    def begin_delete(self, rel: str) -> str:
        """Acquire the per-unit delete claim, or return the reason it is busy."""
        with self._lock:
            if self.current_unit == rel:
                return "cannot delete a book that is currently encoding"
            if rel in self._queue or rel in self._auditing:
                return "this book is being checked or waiting for a manual action"
            if rel in self._deleting:
                return "this book is already being deleted"
            self._deleting.add(rel)
            return ""

    def end_delete(self, rel: str) -> None:
        with self._lock:
            self._deleting.remove(rel)

    def deleting(self, rel: str) -> bool:
        with self._lock:
            return rel in self._deleting

    def begin_audit(self, rel: str, *, manual: bool = False) -> bool:
        with self._lock:
            if rel in self._deleting or rel in self._auditing or (not manual and rel in self._queue):
                return False
            self._auditing.add(rel)
            return True

    def end_audit(self, rel: str) -> None:
        with self._lock:
            self._auditing.remove(rel)

    # ---------------------------------------------------------------- queue
    def enqueue(self, rel: str) -> None:
        """Mark a manually requested conversion as waiting for the encoder."""
        with self._lock:
            if rel not in self._queue:
                self._queue.append(rel)

    def try_enqueue(self, rel: str) -> bool:
        """Queue ``rel`` unless it is already waiting (atomic check-and-add)."""
        with self._lock:
            if rel in self._queue or rel in self._deleting or rel in self._auditing:
                return False
            self._queue.append(rel)
            return True

    def dequeue(self, rel: str) -> None:
        with self._lock:
            if rel in self._queue:
                self._queue.remove(rel)

    def queue_snapshot(self) -> dict:
        with self._lock:
            return {"units": list(self._queue), "count": len(self._queue)}

    def end_unit(self) -> None:
        self.current_unit = ""
        self.current_title = ""
        self.progress = {}

    def set_progress(self, **values) -> None:
        self.progress = values
        self._beat()

    def set_heartbeat(self, fn) -> None:
        """Install (or with None, remove) the callable an encode's progress keeps alive."""
        self._heartbeat = fn
        self._heartbeat_at = 0.0

    def _beat(self) -> None:
        fn = self._heartbeat
        if fn is None:
            return
        now = time.monotonic()
        if self._heartbeat_at and now - self._heartbeat_at < HEARTBEAT_EVERY_S:
            return
        self._heartbeat_at = now
        try:
            fn()
        except Exception:  # noqa: BLE001 - a heartbeat must never break an encode
            logging.getLogger("audiobind").debug("heartbeat failed", exc_info=True)

    def clear_progress(self) -> None:
        self.progress = {}

    def attach_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
        if self._stop.is_set():  # stop arrived between the check and the spawn
            proc.terminate()

    def detach_process(self) -> None:
        with self._lock:
            self._proc = None

    # ---------------------------------------------------------------- dry run
    @property
    def dry_run(self) -> bool:
        """Effective dry-run state: the UI override wins over the DRY_RUN environment."""
        return CONFIG.dry_run if self._dry_run is None else self._dry_run

    def set_dry_run(self, value: bool) -> None:
        self._dry_run = bool(value)

    @property
    def dry_run_source(self) -> str:
        return "default" if self._dry_run is None else "set in UI"

    # ------------------------------------------------- watcher dry run (separate switch)
    @property
    def watcher_dry_run(self) -> bool:
        """Acquisition only: search and decide, never download. Independent of dry_run."""
        return CONFIG.watch_dry_run if self._watcher_dry_run is None else self._watcher_dry_run

    def set_watcher_dry_run(self, value: bool) -> None:
        self._watcher_dry_run = bool(value)

    @property
    def watcher_dry_run_source(self) -> str:
        return "default" if self._watcher_dry_run is None else "set in UI"

    # ---------------------------------------------------------------- scan progress
    def begin_scan(self, total: int) -> None:
        with self._lock:
            self._scanning = True
            self._scan_done = 0
            self._scan_total = int(total)

    def scan_tick(self, done: int) -> None:
        with self._lock:
            self._scan_done = int(done)

    def end_scan(self) -> None:
        with self._lock:
            self._scanning = False
            self._scan_done = 0
            self._scan_total = 0

    @property
    def scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def scan_progress(self) -> dict:
        with self._lock:
            return {"scanning": self._scanning, "done": self._scan_done, "total": self._scan_total}

    # ---------------------------------------------------------------- scan
    def request_scan(self, force: bool = False) -> None:
        """Ask for a pass. ``force`` re-audits books that already have a verdict."""
        self._scan.set()
        with self._lock:
            self._scan_force = bool(force)

    def scan_requested(self) -> bool:
        return self._scan.is_set()

    @property
    def scan_force(self) -> bool:
        """One-shot: the next pass re-audits decided books, then the flag resets."""
        with self._lock:
            force = self._scan_force
            self._scan_force = False
            return force

    def clear_scan(self) -> None:
        self._scan.clear()

    def wait(self, timeout: float) -> bool:
        """Sleep until the timeout or until someone clicks 'Scan now'."""
        return self._scan.wait(timeout)

    # ---------------------------------------------------------------- helpers
    def describe(self) -> dict:
        scan = self.scan_progress()
        return {
            "paused": self.paused,
            "scan_pending": self.scan_requested(),
            "dry_run": self.dry_run,
            "dry_run_source": self.dry_run_source,
            "watcher_dry_run": self.watcher_dry_run,
            "watcher_dry_run_source": self.watcher_dry_run_source,
            "running": self.running,
            "current_unit": self.current_unit,
            "current_title": self.current_title,
            "progress": self.progress,
            "scan": scan,
            "scan_label": scan_label(scan, self.current_unit, self.progress, self.current_title),
            "queue": self.queue_snapshot(),
        }


def unit_display_name(current_unit: str, title: str = "") -> str:
    """What to call the book being encoded: its catalog title, else its folder name.

    ``current_unit`` is a state key: ``Author/Book``, ``ingest:Book`` or ``Parent#key``.
    """
    if (title or "").strip():
        return title.strip()
    key = current_unit or ""
    if key.startswith("ingest:"):
        key = key[len("ingest:"):]
    base, _, part = key.partition("#")
    name = base.rstrip("/").rsplit("/", 1)[-1]
    return f"{name} ({part})" if part else name


def scan_label(scan: dict, current_unit: str = "", progress: dict | None = None, title: str = "") -> str:
    """The status page's scan line while a scan is running ("" when it is not).

    A scan encodes eligible books inline and one encode can run for a quarter of an hour, so
    while a book is encoding the line names it and shows its progress instead of a frozen
    ``Scanning n/m...``; ``Scanning n/m...`` is only the fallback when nothing is encoding.
    """
    if not (scan or {}).get("scanning"):
        return ""
    if current_unit:
        name = unit_display_name(current_unit, title)
        percent = (progress or {}).get("percent")
        try:
            pct = " - {}%".format(int(float(percent))) if percent is not None else ""
        except (TypeError, ValueError):
            pct = ""
        return "Encoding " + name + pct + "…"
    return "Scanning {}/{}…".format(int(scan.get("done") or 0), int(scan.get("total") or 0))


CONTROL = Control()

"""Shared run controls, driven by the status page.

The daemon asks this object whether it may start another book, whether writes are allowed, and
the web handlers flip those switches at runtime. Nothing here touches the filesystem -
persistence lives in State.set_meta() ("paused", "dry_run"), so a restart keeps your choices.

Three ways to intervene, and they are not the same thing:

* **pause**   - finish the book being converted, then start nothing new.
* **stop**    - abort the current ffmpeg now. The partial file never leaves the work directory,
                so nothing corrupt reaches the library, and the book is retried on the next scan.
* **dry run** - decide everything, write nothing.
"""

from __future__ import annotations

import subprocess
import threading

from .config import CONFIG


class Stopped(Exception):
    """Raised when the operator aborts the conversion that is currently running."""


class Control:
    def __init__(self) -> None:
        self._scan = threading.Event()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        #: None = use the DRY_RUN env default; True/False = the user chose in the web UI
        self._dry_run: bool | None = None
        self.current_unit: str = ""
        #: live encode progress: {"percent", "elapsed", "eta", "speed"}
        self.progress: dict = {}
        #: live scan progress for the status page's busy indicator
        self._scanning = False
        self._scan_done = 0
        self._scan_total = 0
        self._scan_force = False
        #: manually requested conversions waiting for the one-at-a-time encoder
        self._queue: list[str] = []

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

    def begin_unit(self, name: str) -> None:
        self.current_unit = name
        self.progress = {}
        self._stop.clear()
        self.dequeue(name)

    # ---------------------------------------------------------------- queue
    def enqueue(self, rel: str) -> None:
        """Mark a manually requested conversion as waiting for the encoder."""
        with self._lock:
            if rel not in self._queue:
                self._queue.append(rel)

    def try_enqueue(self, rel: str) -> bool:
        """Queue ``rel`` unless it is already waiting (atomic check-and-add)."""
        with self._lock:
            if rel in self._queue:
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
        self.progress = {}

    def set_progress(self, **values) -> None:
        self.progress = values

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
        return {
            "paused": self.paused,
            "scan_pending": self.scan_requested(),
            "dry_run": self.dry_run,
            "dry_run_source": self.dry_run_source,
            "running": self.running,
            "current_unit": self.current_unit,
            "progress": self.progress,
            "scan": self.scan_progress(),
            "queue": self.queue_snapshot(),
        }


CONTROL = Control()
